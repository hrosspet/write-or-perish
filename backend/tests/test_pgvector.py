"""Tests for the optional pgvector ranking (#331).

Two groups:
* Always (sqlite, like the rest of the suite): vector literals, the SQL
  the ranking compiles to on Postgres, and rank_embeddings' dispatch:
  Python path when pgvector is off, merge of database and Python rows
  when it is on, fallback to Python when the SQL fails.
* Only with PGVECTOR_TEST_DATABASE_URL set (CI has no Postgres): the real
  thing against a Postgres with the vector extension created, through
  `flask pgvector-enable --apply`. The URL's role needs CREATE on its
  database (the tests create and drop the `vec` schema) and the extension
  must exist. The tests drop and recreate every table in that database.
  For example: docker run --rm -p 127.0.0.1:55431:5432
  -e POSTGRES_PASSWORD=test pgvector/pgvector:pg15, then CREATE EXTENSION
  vector in a database and point the URL at it.

OpenAI is never called.
"""
import contextlib
import os
import sys
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from flask import Flask  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    ExternalItem, ExternalItemEmbedding, Node, NodeEmbedding, User,
)
from backend.utils import pgvector  # noqa: E402
from backend.utils.embeddings import (  # noqa: E402
    EMBEDDING_DIM, EMBEDDING_MODEL, content_hash, pack_vector,
    rank_embeddings, top_k_similar,
)

# The sweep's helper, imported against stub glue (as test_semantic_search).
_GLUE = ("backend.celery_app", "backend.tasks.embeddings")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.embeddings", None)
import backend.tasks.embeddings as embedding_tasks  # noqa: E402
for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

PG_URL = os.environ.get("PGVECTOR_TEST_DATABASE_URL")
_RNG = np.random.default_rng(331)


@pytest.fixture(autouse=True)
def _fresh_detection():
    """The check is cached per process; no test may leak its answer."""
    pgvector.reset()
    yield
    pgvector.reset()


def _vec(seed_or_values=None):
    if seed_or_values is None:
        return _RNG.standard_normal(EMBEDDING_DIM).astype(np.float32).tolist()
    return list(seed_or_values)


def _basis(i, scale=1.0, tail=None):
    """A 1536-dim vector pointing mostly along axis i."""
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[i] = scale
    if tail is not None:
        v[EMBEDDING_DIM - 1] = tail
    return v.tolist()


def _make_app(url):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = url
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["OPENAI_API_KEY_CHAT"] = "fake-key"
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.search import search_bp
    from backend.pgvector_cli import (
        pgvector_compare_command, pgvector_enable_command,
    )
    app.register_blueprint(search_bp, url_prefix="/api")
    app.cli.add_command(pgvector_enable_command)
    app.cli.add_command(pgvector_compare_command)
    return app


def _mk_user(name):
    user = User(username=name)
    _db.session.add(user)
    _db.session.commit()
    return user


def _mk_embedded_node(uid, vector, text=None, model=EMBEDDING_MODEL):
    text = text or f"entry {_RNG.integers(1 << 30)}"
    node = Node(user_id=uid, human_owner_id=uid, node_type="text",
                ai_usage="chat")
    node.set_content(text)
    _db.session.add(node)
    _db.session.flush()
    emb = NodeEmbedding(node_id=node.id, user_id=uid, model=model,
                        content_hash=content_hash(text),
                        vector=pack_vector(vector))
    _db.session.add(emb)
    _db.session.commit()
    return node, emb


def _node_rows(uid):
    return _db.session.query(
        NodeEmbedding.node_id, NodeEmbedding.vector
    ).filter(NodeEmbedding.user_id == uid)


# ── Always: literals, SQL shape, dispatch ────────────────────────────────

def test_vector_literal_round_trips_float32_exactly():
    values = np.asarray(_vec(), dtype=np.float32)
    values[:4] = [1e-45, -3.4028235e38, 0.1, -0.0]
    literal = pgvector.vector_literal(values)
    assert literal.startswith("[") and literal.endswith("]")
    parsed = np.array([np.float32(x) for x in literal[1:-1].split(",")],
                      dtype=np.float32)
    assert np.array_equal(parsed, values)


def test_vector_literal_refuses_what_the_database_cannot_rank():
    assert pgvector.vector_literal([1.0, 0.0]) is None          # dimension
    assert pgvector.vector_literal([0.0] * EMBEDDING_DIM) is None  # no direction
    nan = _vec()
    nan[3] = float("nan")
    assert pgvector.vector_literal(nan) is None
    inf = _vec()
    inf[3] = float("inf")
    assert pgvector.vector_literal(inf) is None


def test_blob_literal_checks_the_blob_size():
    values = _vec()
    blob = pack_vector(values)
    assert pgvector.blob_literal(blob) == pgvector.vector_literal(values)
    assert pgvector.blob_literal(memoryview(blob)) == \
        pgvector.vector_literal(values)  # psycopg2 hands back memoryview
    assert pgvector.blob_literal(pack_vector([1.0, 2.0])) is None


def test_off_on_sqlite():
    app = _make_app("sqlite:///:memory:")
    with app.app_context():
        assert pgvector.enabled() is False
        assert pgvector._state["enabled"] is False  # cached


def test_a_failed_check_is_not_cached(monkeypatch):
    monkeypatch.setattr(pgvector, "_detect", lambda: None)
    assert pgvector.enabled() is False
    assert pgvector._state["enabled"] is None
    monkeypatch.setattr(pgvector, "_detect", lambda: True)
    assert pgvector.enabled() is True
    monkeypatch.setattr(pgvector, "_detect", lambda: False)
    assert pgvector.enabled() is True  # cached until restart


def _compile(query):
    return query.statement.compile(dialect=postgresql.dialect())


def test_ranked_in_db_sql_keeps_the_callers_filters():
    app = _make_app("sqlite:///:memory:")
    with app.app_context():
        base = _node_rows(7).filter(
            ~NodeEmbedding.node_id.in_([1, 2])
        ).join(Node, Node.id == NodeEmbedding.node_id).filter(
            Node.created_at >= sa.func.now())
        literal = pgvector.vector_literal(_vec())
        compiled = _compile(
            pgvector.ranked_in_db(base, NodeEmbedding, literal, 5))
    sql = " ".join(str(compiled).split())
    assert "SELECT node_embedding.node_id, " in sql
    assert ("- (pgv.embedding <=> CAST(%(query_vector)s AS vector)) AS score"
            in sql)
    assert "FROM node_embedding JOIN node ON node.id = node_embedding.node_id" \
        in sql
    assert ("JOIN vec.node_embedding AS pgv ON pgv.id = node_embedding.id"
            " AND pgv.content_hash = node_embedding.content_hash"
            " AND pgv.model = node_embedding.model") in sql
    assert "node_embedding.user_id = %(user_id_1)s" in sql
    assert "NOT IN" in sql and "node.created_at >= now()" in sql
    assert sql.endswith(
        "ORDER BY score DESC, node_embedding.node_id LIMIT %(param_2)s")
    assert compiled.params["query_vector"] == literal
    assert compiled.params["param_1"] == 1
    assert compiled.params["param_2"] == 5


def test_without_copy_sql_selects_the_rows_python_must_score():
    app = _make_app("sqlite:///:memory:")
    with app.app_context():
        base = _db.session.query(
            ExternalItemEmbedding.item_id, ExternalItemEmbedding.vector
        ).filter(ExternalItemEmbedding.user_id == 7)
        sql = " ".join(str(_compile(
            pgvector.without_copy(base, ExternalItemEmbedding))).split())
    assert sql.startswith(
        "SELECT external_item_embedding.item_id, external_item_embedding.vector"
        " FROM external_item_embedding LEFT OUTER JOIN"
        " vec.external_item_embedding AS pgv"
        " ON pgv.id = external_item_embedding.id"
        " AND pgv.content_hash = external_item_embedding.content_hash"
        " AND pgv.model = external_item_embedding.model")
    assert sql.endswith("AND pgv.id IS NULL")


def test_ddl_is_idempotent_and_cascades_deletes():
    ddl = pgvector.ddl_statements()
    assert ddl[0] == "SET LOCAL lock_timeout = '5s'"
    for table in ("node_embedding", "external_item_embedding"):
        create = next(s for s in ddl if f"vec.{table} (" in s)
        assert create.startswith("CREATE TABLE IF NOT EXISTS")
        assert f"REFERENCES {table} (id) ON DELETE CASCADE" in create
        assert f"embedding vector({EMBEDDING_DIM}) NOT NULL" in create


class _Rows:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows or [], error

    def all(self):
        if self.error is not None:
            raise self.error
        return self.rows


@pytest.fixture
def sqlite_app():
    app = _make_app("sqlite:///:memory:")
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        _db.drop_all()


def test_rank_embeddings_merges_database_and_python_rows(
        sqlite_app, monkeypatch):
    """With pgvector on, the database's top k (stubbed here) and the rows
    without a copy (scored in Python, for real) merge by score."""
    uid = _mk_user("u").id
    a, _ = _mk_embedded_node(uid, _basis(0))
    b, _ = _mk_embedded_node(uid, _basis(1))
    fresh, _ = _mk_embedded_node(uid, _basis(0, tail=0.5))  # no copy yet
    monkeypatch.setattr(pgvector, "enabled", lambda: True)
    monkeypatch.setattr(_db.session, "begin_nested", contextlib.nullcontext)
    seen = {}

    def fake_ranked_in_db(query, source, literal, k):
        seen.update(source=source, literal=literal, k=k)
        return _Rows([(a.id, 0.99), (b.id, 0.1), (b.id + 100, float("nan"))])

    monkeypatch.setattr(pgvector, "ranked_in_db", fake_ranked_in_db)
    monkeypatch.setattr(
        pgvector, "without_copy",
        lambda query, source: query.filter(NodeEmbedding.node_id == fresh.id))

    ranked = rank_embeddings(_basis(0), _node_rows(uid), NodeEmbedding, k=2,
                             min_score=0.2)
    assert [nid for nid, _ in ranked] == [a.id, fresh.id]
    assert ranked[1][1] == pytest.approx(1 / np.sqrt(1.25), abs=1e-6)
    assert seen == {"source": NodeEmbedding,
                    "literal": pgvector.vector_literal(_basis(0)), "k": 2}


def test_rank_embeddings_falls_back_to_python_when_sql_fails(
        sqlite_app, monkeypatch, caplog):
    uid = _mk_user("u").id
    a, _ = _mk_embedded_node(uid, _basis(0))
    _mk_embedded_node(uid, _basis(1))
    pgvector._state["enabled"] = True
    monkeypatch.setattr(_db.session, "begin_nested", contextlib.nullcontext)
    monkeypatch.setattr(
        pgvector, "ranked_in_db",
        lambda *a, **kw: _Rows(error=OperationalError("SELECT", {}, None)))

    with caplog.at_level("ERROR", logger="backend.utils.embeddings"):
        ranked = rank_embeddings(_basis(0), _node_rows(uid), NodeEmbedding,
                                 k=1)
    assert [nid for nid, _ in ranked] == [a.id]
    assert pgvector._state["enabled"] is False  # Python from now on
    assert "pgvector ranking failed" in caplog.text


def test_rank_embeddings_stays_in_python_for_vectors_sql_cannot_rank(
        sqlite_app, monkeypatch):
    uid = _mk_user("u").id
    node, _ = _mk_embedded_node(uid, [1.0, 0.0])
    monkeypatch.setattr(pgvector, "enabled", lambda: True)

    def boom(*a, **kw):
        raise AssertionError("must not reach SQL")

    monkeypatch.setattr(pgvector, "ranked_in_db", boom)
    assert [nid for nid, _ in rank_embeddings(
        [1.0, 0.0], _node_rows(uid), NodeEmbedding)] == [node.id]
    assert rank_embeddings(_basis(0), _node_rows(uid), NodeEmbedding,
                           k=0) == []


def test_sweep_syncs_copies_only_when_on_and_never_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(pgvector, "enabled", lambda: False)
    monkeypatch.setattr(pgvector, "sync_copies",
                        lambda **kw: calls.append(kw))
    embedding_tasks._sync_pgvector_copies()
    assert calls == []

    monkeypatch.setattr(pgvector, "enabled", lambda: True)
    embedding_tasks._sync_pgvector_copies()
    assert calls == [{"limit": pgvector.SWEEP_SYNC_LIMIT}]

    rollback = MagicMock()
    monkeypatch.setattr(embedding_tasks.db.session, "rollback", rollback)

    def fail(**kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(pgvector, "sync_copies", fail)
    embedding_tasks._sync_pgvector_copies()  # logged, not raised
    rollback.assert_called_once()


# ── Postgres + pgvector (opt-in) ─────────────────────────────────────────

pg_only = pytest.mark.skipif(
    not PG_URL, reason="set PGVECTOR_TEST_DATABASE_URL to run")


def _drop_copies():
    _db.session.execute(sa.text("DROP SCHEMA IF EXISTS vec CASCADE"))
    _db.session.commit()


@pytest.fixture
def pg_app():
    app = _make_app(PG_URL)
    with app.app_context():
        _drop_copies()
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.rollback()
        _drop_copies()
        _db.drop_all()


def _enable(app):
    result = app.test_cli_runner().invoke(args=["pgvector-enable", "--apply"])
    assert result.exit_code == 0, result.output
    pgvector.reset()
    return result.output


def _python_ranking(query_vector, query, k, min_score):
    return top_k_similar(query_vector, query.all(), k=k, min_score=min_score)


def _assert_same_ranking(got, want):
    assert [i for i, _ in got] == [i for i, _ in want]
    for (_, a), (_, b) in zip(got, want):
        assert a == pytest.approx(b, abs=1e-5)


@pg_only
def test_pg_dry_run_changes_nothing(pg_app):
    uid = _mk_user("u").id
    _mk_embedded_node(uid, _vec())
    result = pg_app.test_cli_runner().invoke(args=["pgvector-enable"])
    assert result.exit_code == 0, result.output
    assert "node_embedding: 1 rows, 0 with a current copy, 1 to copy" \
        in result.output
    assert "Report only: nothing changed" in result.output
    assert _db.session.execute(sa.text(
        "SELECT to_regclass('vec.node_embedding')")).scalar() is None
    assert pgvector.enabled() is False


@pg_only
def test_pg_ranks_like_python_and_only_the_users_rows(pg_app):
    uid = _mk_user("u").id
    other = _mk_user("other").id
    for _ in range(120):
        _mk_embedded_node(uid, _vec())
    zero, _ = _mk_embedded_node(uid, [0.0] * EMBEDDING_DIM)
    _mk_embedded_node(uid, [1.0] * 8)                 # another dimension
    for _ in range(30):
        _mk_embedded_node(other, _vec())
    output = _enable(pg_app)
    assert "node_embedding: 150 copied, 2 left to the Python path" in output
    assert pgvector.enabled() is True

    others = {nid for (nid,) in _db.session.query(NodeEmbedding.node_id)
              .filter(NodeEmbedding.user_id == other)}
    for min_score in (-1.0, 0.0, 0.05):
        q = _vec()
        got = rank_embeddings(q, _node_rows(uid), NodeEmbedding, k=10,
                              min_score=min_score)
        _assert_same_ranking(
            got, _python_ranking(q, _node_rows(uid), 10, min_score))
        assert not others & {i for i, _ in got}
    # Everything at once: the zero vector (Python path) scores 0.0 there.
    q = _vec()
    got = rank_embeddings(q, _node_rows(uid), NodeEmbedding, k=500,
                          min_score=-1.0)
    _assert_same_ranking(got, _python_ranking(q, _node_rows(uid), 500, -1.0))
    assert dict(got)[zero.id] == 0.0
    assert len(got) == 121  # the other dimension's row is skipped


@pg_only
def test_pg_a_reembedded_row_is_ranked_from_its_new_vector(pg_app):
    uid = _mk_user("u").id
    for _ in range(20):
        _mk_embedded_node(uid, _vec())
    node, emb = _mk_embedded_node(uid, _vec())
    _enable(pg_app)

    new_vector = _vec()
    emb.vector = pack_vector(new_vector)
    emb.content_hash = content_hash("edited")
    _db.session.commit()
    assert pgvector.without_copy(_node_rows(uid), NodeEmbedding).count() == 1
    top = rank_embeddings(new_vector, _node_rows(uid), NodeEmbedding, k=1)
    assert top[0][0] == node.id and top[0][1] == pytest.approx(1.0, abs=1e-6)

    assert pgvector.sync_copies() == {
        "node_embedding": (1, 1), "external_item_embedding": (0, 0)}
    assert pgvector.without_copy(_node_rows(uid), NodeEmbedding).count() == 0
    top = rank_embeddings(new_vector, _node_rows(uid), NodeEmbedding, k=1)
    assert top[0][0] == node.id and top[0][1] == pytest.approx(1.0, abs=1e-6)


@pg_only
def test_pg_copies_are_exact_and_go_with_their_row(pg_app):
    uid = _mk_user("u").id
    node, emb = _mk_embedded_node(uid, _vec())
    _enable(pg_app)
    text = _db.session.execute(sa.text(
        "SELECT embedding::text FROM vec.node_embedding WHERE id = :id"),
        {"id": emb.id}).scalar()
    stored = np.array([np.float32(x) for x in text[1:-1].split(",")],
                      dtype=np.float32)
    assert np.array_equal(stored, np.frombuffer(emb.vector, dtype=np.float32))

    _db.session.delete(emb)
    _db.session.commit()
    assert _db.session.execute(sa.text(
        "SELECT count(*) FROM vec.node_embedding")).scalar() == 0


@pg_only
def test_pg_references_rank_like_python(pg_app):
    uid = _mk_user("u").id
    for i in range(40):
        item = ExternalItem(user_id=uid, source="web_clip",
                            external_id=f"{i:064d}", content=f"clip {i}")
        _db.session.add(item)
        _db.session.flush()
        _db.session.add(ExternalItemEmbedding(
            item_id=item.id, user_id=uid, model=EMBEDDING_MODEL,
            content_hash=content_hash(item.content),
            vector=pack_vector(_vec())))
    _db.session.commit()
    _enable(pg_app)
    rows = _db.session.query(
        ExternalItemEmbedding.item_id, ExternalItemEmbedding.vector
    ).filter(ExternalItemEmbedding.user_id == uid)
    q = _vec()
    _assert_same_ranking(
        rank_embeddings(q, rows, ExternalItemEmbedding, k=5, min_score=-1.0),
        _python_ranking(q, rows, 5, -1.0))


@pg_only
def test_pg_semantic_search_route_gives_the_same_results(pg_app, monkeypatch):
    user = _mk_user("u")
    for _ in range(60):
        _mk_embedded_node(user.id, _vec())
    _enable(pg_app)
    q = _vec()
    import backend.utils.embeddings as emb_module
    monkeypatch.setattr(emb_module, "embed_texts",
                        lambda texts, key, **kw: [q for _ in texts])
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    def search():
        body = client.get(
            "/api/search/semantic?q=x&limit=8&min_score=-1").get_json()
        return [(r["id"], r["score"]) for r in body["results"]]

    in_sql = search()
    assert pgvector._state["enabled"] is True
    pgvector.turn_off()
    in_python = search()
    assert [i for i, _ in in_sql] == [i for i, _ in in_python]
    assert len(in_sql) == 8


@pg_only
def test_pg_dropping_the_copies_falls_back_without_breaking_the_session(
        pg_app):
    uid = _mk_user("u").id
    node, _ = _mk_embedded_node(uid, _basis(0))
    _mk_embedded_node(uid, _basis(1))
    _enable(pg_app)
    assert pgvector.enabled() is True
    _drop_copies()  # the documented off switch, while processes run
    # Pending work of the caller (e.g. the query's cost log) must survive
    # the rollback to the savepoint.
    _db.session.add(User(username="pending"))

    ranked = rank_embeddings(_basis(0), _node_rows(uid), NodeEmbedding, k=1)
    assert [nid for nid, _ in ranked] == [node.id]
    assert pgvector._state["enabled"] is False
    _db.session.commit()  # transaction still usable
    assert User.query.filter_by(username="pending").count() == 1


@pg_only
def test_pg_autogenerate_never_sees_the_copies(pg_app):
    """The deploy runs `flask db migrate`: the copies must not show up as
    tables or columns to drop, with the same settings Flask-Migrate uses
    (compare_type on, include_schemas off)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def diff():
        with _db.engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True})
            return sorted(repr(d) for d in compare_metadata(
                ctx, _db.metadata))

    assert diff() == []  # models and a create_all'd schema agree
    uid = _mk_user("u").id
    _mk_embedded_node(uid, _vec())
    _enable(pg_app)
    assert diff() == []  # and still do with the copies in place


@pg_only
def test_pg_compare_command(pg_app):
    user = _mk_user("u")
    for _ in range(30):
        _mk_embedded_node(user.id, _vec())
    _enable(pg_app)
    result = pg_app.test_cli_runner().invoke(
        args=["pgvector-compare", "--username", "u", "--repeat", "1"])
    assert result.exit_code == 0, result.output
    assert "29 rows; 0 without a current copy." in result.output
    assert "Same top 10 in the same order: yes" in result.output
