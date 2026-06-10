"""Tests for semantic search / embeddings (#155).

Covers: vector pack/unpack + cosine, top-k ranking, sweep candidate
selection (privacy + staleness), the /api/search/semantic endpoint
(ownership scoping, no-key 503), and the agentic retrieval helper.
OpenAI is never called — embed_texts is monkeypatched everywhere.
"""
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node, NodeEmbedding  # noqa: E402
from backend.utils.embeddings import (  # noqa: E402
    content_hash, cosine_similarity, pack_vector, unpack_vector,
    top_k_similar, retrieve_relevant_snippets,
)

# Import the sweep candidate selector against stub glue
_GLUE = ("backend.celery_app", "backend.tasks.embeddings")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.embeddings", None)
from backend.tasks.embeddings import _candidate_nodes  # noqa: E402
for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["OPENAI_API_KEY_CHAT"] = "fake-key"

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.search import search_bp
    app.register_blueprint(search_bp, url_prefix="/api")
    return app


@pytest.fixture
def app():
    app = _make_app()
    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _mk_node(uid, content, ai_usage="chat", node_type="text"):
    node = Node(user_id=uid, human_owner_id=uid, node_type=node_type,
                ai_usage=ai_usage)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _mk_embedding(node, vector):
    emb = NodeEmbedding(
        node_id=node.id,
        user_id=node.human_owner_id or node.user_id,
        model="text-embedding-3-small",
        content_hash=content_hash(node.get_content()),
        vector=pack_vector(vector),
    )
    _db.session.add(emb)
    _db.session.commit()
    return emb


# ── Pure vector math ─────────────────────────────────────────────────────

def test_pack_unpack_roundtrip():
    values = [0.1, -0.5, 0.0, 2.5]
    out = list(unpack_vector(pack_vector(values)))
    assert out == pytest.approx(values, abs=1e-6)


def test_cosine_similarity():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine_similarity([0, 0], [1, 0]) == 0.0


def test_top_k_similar_ranks_and_filters():
    rows = [
        (1, pack_vector([1.0, 0.0])),
        (2, pack_vector([0.9, 0.1])),
        (3, pack_vector([0.0, 1.0])),
    ]
    ranked = top_k_similar([1.0, 0.0], rows, k=2, min_score=0.5)
    assert [nid for nid, _ in ranked] == [1, 2]
    assert ranked[0][1] > ranked[1][1]


# ── Sweep candidate selection ────────────────────────────────────────────

def test_candidates_respect_privacy_and_staleness(app):
    with app.app_context():
        uid = User.query.first().id
        fresh = _mk_node(uid, "embed me")
        private = _mk_node(uid, "never send", ai_usage="none")
        stale = _mk_node(uid, "old content")
        _mk_embedding(stale, [1.0, 0.0])
        stale.set_content("edited content")  # hash now differs
        _db.session.commit()
        current = _mk_node(uid, "already embedded")
        _mk_embedding(current, [0.5, 0.5])

        out = _candidate_nodes(50)
        ids = {node.id for node, _, _, _ in out}
        assert fresh.id in ids       # missing embedding → candidate
        assert stale.id in ids       # hash mismatch → candidate
        assert private.id not in ids  # ai_usage none → never embedded
        assert current.id not in ids  # up to date → skipped


# ── Semantic endpoint ────────────────────────────────────────────────────

def _patch_query_embedding(monkeypatch, vector):
    import backend.routes.search as search_module  # noqa: F401
    import backend.utils.embeddings as emb_module
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, key, **kw: [vector for _ in texts])


def test_semantic_search_ranks_own_nodes(app, client, monkeypatch):
    with app.app_context():
        uid = User.query.first().id
        close = _mk_node(uid, "a note about gardening tomatoes")
        far = _mk_node(uid, "quarterly tax filing notes")
        _mk_embedding(close, [1.0, 0.0])
        _mk_embedding(far, [0.0, 1.0])
        close_id = close.id

    _patch_query_embedding(monkeypatch, [1.0, 0.05])
    resp = client.get("/api/search/semantic?q=growing vegetables")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mode"] == "semantic"
    assert [r["id"] for r in body["results"]] == [close_id]
    assert body["results"][0]["score"] > 0.9


def test_semantic_search_excludes_other_users(app, client, monkeypatch):
    with app.app_context():
        other = User(username="other")
        _db.session.add(other)
        _db.session.commit()
        foreign = _mk_node(other.id, "someone else's note")
        _mk_embedding(foreign, [1.0, 0.0])

    _patch_query_embedding(monkeypatch, [1.0, 0.0])
    resp = client.get("/api/search/semantic?q=note")
    assert resp.status_code == 200
    assert resp.get_json()["results"] == []


def test_semantic_search_requires_query(app, client):
    assert client.get("/api/search/semantic").status_code == 400


def test_semantic_search_503_without_key(app, client):
    app.config["OPENAI_API_KEY_CHAT"] = None
    resp = client.get("/api/search/semantic?q=x")
    assert resp.status_code == 503


# ── Agentic retrieval helper ─────────────────────────────────────────────

def test_retrieve_relevant_snippets_excludes_chain(app, monkeypatch):
    with app.app_context():
        uid = User.query.first().id
        in_chain = _mk_node(uid, "already in the conversation")
        archive = _mk_node(uid, "an older relevant entry " + "x" * 500)
        _mk_embedding(in_chain, [1.0, 0.0])
        _mk_embedding(archive, [0.95, 0.05])

        import backend.utils.embeddings as emb_module
        monkeypatch.setattr(
            emb_module, "embed_texts",
            lambda texts, key, **kw: [[1.0, 0.0] for _ in texts])

        results = retrieve_relevant_snippets(
            uid, "query", [in_chain.id], "fake-key",
            k=4, min_score=0.3, snippet_chars=100)
        assert [nid for nid, _, _, _ in results] == [archive.id]
        # Snippet is truncated with ellipsis
        assert results[0][2].endswith("…")
        assert len(results[0][2]) <= 101
