"""has_own_entries: has the user written anything in Loore yet (#391, #392).

Typed, recorded and uploaded entries count. Imports (every importer,
including the pre-fills and imports from before Node.origin), LLM nodes,
the prompt node a session starts with, and soft-deleted nodes do not.
The check is one EXISTS query and never decrypts.

Nodes are built the way the real paths build them: the session prompt
roots through create_agentic_root (what /textmode/start, Voice and the
upload path call), chat and markdown imports through the importer's own
_add_imported_message_nodes.

Bare app (dashboard blueprint only), same module-swap pattern as
test_email_change.py.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from sqlalchemy import event  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils.own_entries import has_own_entries  # noqa: E402


def _make_app():
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    return app


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def app():
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    app = _make_app()
    with app.app_context():
        _db.create_all()
        _db.session.add_all([
            User(username="newbie", approved=True, plan="alpha"),
            User(username="other", approved=True, plan="alpha"),
            User(username="llm-test-model", twitter_id="llm-test-model"),
        ])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _user(username="newbie"):
    return User.query.filter_by(username=username).first()


def _node(user, parent=None, content="words", **kwargs):
    fields = dict(user_id=user.id, human_owner_id=user.id,
                  parent_id=parent.id if parent else None,
                  node_type="user", privacy_level="private",
                  ai_usage="chat")
    fields.update(kwargs)
    node = Node(**fields)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _session_root(user, prompt_key):
    """The empty prompt node Voice / Text / Read sessions start with."""
    from backend.utils.session_helpers import create_agentic_root
    root = create_agentic_root(user.id, prompt_key, "private", "chat")
    _db.session.commit()
    return root


def _import(user, origin, content="imported words", node_type="user",
            source_key="k"):
    from backend.routes.import_data import _add_imported_message_nodes
    llm = _user("llm-test-model")
    _add_imported_message_nodes(
        user_id=llm.id if node_type == "llm" else user.id,
        human_owner_id=user.id, parent_id=None, node_type=node_type,
        llm_model="claude-web" if node_type == "llm" else None,
        node_content=content, privacy_level="private", ai_usage="chat",
        source_key=f"{origin}:{source_key}", msg_created_at=None,
        origin=origin,
    )
    _db.session.commit()


# ── What counts ──────────────────────────────────────────────────────────

def test_newcomer_has_none(app):
    assert has_own_entries(_user().id) is False


def test_typed_entry_counts(app):
    # /textmode/start: the session's prompt root, the typed entry under it.
    user = _user()
    root = _session_root(user, "textmode")
    assert has_own_entries(user.id) is False
    _node(user, parent=root, content="What brought me here")
    assert has_own_entries(user.id) is True


def test_recorded_entry_counts(app):
    # Voice: the transcript lands on a user node under the voice root,
    # with or without the original audio kept.
    user = _user()
    root = _session_root(user, "voice")
    assert has_own_entries(user.id) is False
    _node(user, parent=root, streaming_transcription=True,
          transcription_status="completed")
    assert has_own_entries(user.id) is True


def test_uploaded_recording_counts(app):
    # POST /nodes/ with an audio file: a placeholder node until the
    # transcript arrives. It counts from the moment it exists.
    user = _user()
    _node(user, content="[Voice note – transcription pending]",
          transcription_status="pending",
          audio_original_url="/media/1/original.webm",
          audio_mime_type="audio/webm")
    assert has_own_entries(user.id) is True


# ── What doesn't ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin", ["chatgpt", "claude", "markdown"])
def test_chat_and_markdown_imports_dont_count(app, origin):
    user = _user()
    _import(user, origin)
    _import(user, origin, node_type="llm", source_key="reply")
    assert Node.query.count() == 2
    assert has_own_entries(user.id) is False


def test_split_import_doesnt_count(app):
    # Above the per-node cap an import becomes a chain; source_key sits on
    # the tip only, origin on every part.
    from backend.utils.node_split import NODE_CHAR_CAP
    user = _user()
    _import(user, "markdown", content="x" * (NODE_CHAR_CAP + 10))
    assert Node.query.filter(Node.source_key.is_(None)).count() == 1
    assert has_own_entries(user.id) is False


def test_tweets_dont_count(app):
    # Twitter archive upload, Community Archive and X API pre-fill all
    # stamp origin="twitter" (provenance tells them apart).
    user = _user()
    for i, provenance in enumerate(
            ["archive_upload", "prefill_ca", "prefill_x"]):
        _node(user, origin="twitter", source_key=f"twitter:{i}",
              provenance=provenance)
    assert has_own_entries(user.id) is False


def test_import_from_before_origin_column_doesnt_count(app):
    user = _user()
    _node(user, source_key="chatgpt:abc")
    assert has_own_entries(user.id) is False


def test_llm_reply_doesnt_count(app):
    user = _user()
    llm = _user("llm-test-model")
    _node(llm, human_owner_id=user.id, node_type="llm",
          llm_model="test-model")
    assert has_own_entries(user.id) is False


def test_only_user_nodes_count(app):
    # A craft-mode link node (POST /nodes/<id>/link) points at a node
    # that already exists; it is not an entry of its own.
    user = _user()
    _node(user, node_type="link", content="")
    assert has_own_entries(user.id) is False


def test_session_prompt_nodes_dont_count(app):
    user = _user()
    for key in ("voice", "textmode", "read"):
        _session_root(user, key)
    # A root from before the prompt_key stamp: only the prompt link.
    old_root = _session_root(user, "voice")
    old_root.prompt_key = None
    _db.session.commit()
    # A root whose per-thread edit detached the link but kept the stamp.
    edited = _node(user, prompt_key="textmode", content="my edited prompt")
    assert edited.context_artifacts == []
    assert has_own_entries(user.id) is False


def test_deleted_entry_doesnt_count(app):
    user = _user()
    _node(user, deleted_at=datetime.utcnow())
    assert has_own_entries(user.id) is False


def test_someone_elses_entry_doesnt_count(app):
    _node(_user("other"))
    assert has_own_entries(_user().id) is False


# ── Cost ─────────────────────────────────────────────────────────────────

def test_one_query_no_decryption(app):
    user = _user()
    for i in range(5):
        _node(user, origin="twitter", source_key=f"twitter:{i}")
    _node(user)
    uid = user.id  # loaded now: only the check's own query is counted
    statements = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = _db.engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        with patch("backend.models.decrypt_content",
                   side_effect=AssertionError("decrypted a node")):
            assert has_own_entries(uid) is True
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert len(statements) == 1
    # The user's rows first (MATERIALIZED CTE on PostgreSQL), then EXISTS.
    assert statements[0].lstrip().upper().startswith("WITH MINE AS")


def test_postgres_statement_selects_the_users_rows_first(app):
    # SQLite cannot show the plan, so pin the SQL PostgreSQL receives: the
    # user's rows in a MATERIALIZED CTE (without the keyword PostgreSQL 12+
    # folds it into the outer query and plans the old way, reading other
    # users' rows for a user whose rows are all imports), and the other
    # conditions applied to that CTE, not to node.
    from sqlalchemy.dialects import postgresql
    from backend.utils.own_entries import own_entries_query
    sql = str(own_entries_query(7).compile(dialect=postgresql.dialect()))
    assert "WITH mine AS MATERIALIZED" in sql
    cte, outer = sql.split("SELECT EXISTS", 1)
    assert "WHERE node.user_id = " in cte
    assert "node.origin" not in outer and "node.node_type" not in outer
    assert "mine.origin IS NULL" in outer


# ── Exposed on the current user ──────────────────────────────────────────

def _client(app, username="newbie"):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_user(username).id)
        sess["_fresh"] = True
    return client


def test_dashboard_user_carries_the_flag(app):
    client = _client(app)
    assert client.get(
        "/api/dashboard/").get_json()["user"]["has_own_entries"] is False
    _node(_user())
    assert client.get(
        "/api/dashboard/").get_json()["user"]["has_own_entries"] is True


def test_user_update_response_carries_the_flag(app):
    # The client swaps its whole user object for this response (the
    # tweets opt-in on /welcome does), so the flag must survive it.
    client = _client(app)
    res = client.put("/api/dashboard/user", json={"prefill_consent": "no"})
    assert res.status_code == 200
    assert res.get_json()["user"]["has_own_entries"] is False
    _node(_user())
    res = client.put("/api/dashboard/user", json={"prefill_consent": "no"})
    assert res.status_code == 200
    assert res.get_json()["user"]["has_own_entries"] is True
