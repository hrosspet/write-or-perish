"""Tests for POST /drafts/streaming/<session_id>/save-as-node.

A recorded entry (streaming transcription) is saved through this route.
Text mode is agentic, so a recorded entry must be able to get the same
treatment a typed one gets from /textmode/start: a system node carrying
the textmode prompt (`agentic`) and an auto-generated reply
(`auto_generate`). Before these flags a recording on the Write page
became a bare node with no prompt and no reply while auto-generate
was ON — the user had to send a second message to get one.

Covers:
- default call (no flags): bare node, nothing else — unchanged contract.
- agentic + auto_generate: system node (prompt_key stamped), user node
  under it, LLM placeholder under the user node; response ids.
- auto_generate alone: placeholder under the plain root.
- agentic on a reply draft → 400; flags with ai_usage 'none' → 400.
- spend cap: entry saved, reply skipped, `spend_capped` flagged.
- unsupported model → 400, nothing saved.
"""

import os
import sys
import tempfile
import uuid
from unittest.mock import MagicMock, patch

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
# Keep the (best-effort, no-op here) draft→node audio move out of the repo.
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-test-")

# Mock optional heavy deps
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

# Pre-mock the LLM task module so create_llm_placeholder's lazy import
# picks up our mock instead of importing the full celery/ffmpeg chain.
_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    User, Node, Draft, NodeContextArtifact,
)
import backend.models as _real_backend_models    # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
        "claude-opus-4.7": {
            "provider": "anthropic", "api_model": "claude-opus-4.7",
        },
    }

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")

    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _make_user(username, **kwargs):
    u = User(username=username, approved=True, plan="alpha", **kwargs)
    _db.session.add(u)
    _db.session.flush()
    return u


def _make_node(user, parent_id=None, content="hello", ai_usage="chat"):
    n = Node(
        user_id=user.id,
        human_owner_id=user.id,
        parent_id=parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


def _make_completed_draft(user, content="recorded words", parent_id=None,
                          ai_usage="chat"):
    d = Draft(
        user_id=user.id,
        node_id=None,
        parent_id=parent_id,
        session_id=str(uuid.uuid4()),
        streaming_status="completed",
        streaming_total_chunks=1,
        streaming_completed_chunks=1,
        privacy_level="private",
        ai_usage=ai_usage,
    )
    d.set_content(content)
    _db.session.add(d)
    _db.session.flush()
    return d


def _setup(app, **draft_kwargs):
    client = app.test_client()
    alice = _make_user("alice")
    _db.session.flush()
    draft = _make_completed_draft(alice, **draft_kwargs)
    _db.session.commit()
    _login(client, alice.id)
    return client, alice, draft


def _url(draft):
    return f"/api/drafts/streaming/{draft.session_id}/save-as-node"


class TestSaveAsNodeDefault:
    def test_no_flags_creates_bare_node_only(self, app):
        client, alice, draft = _setup(app)
        sid = draft.session_id

        resp = client.post(_url(draft), json={"content": "edited words"})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["id"] == data["user_node_id"]
        assert "conversation_id" not in data
        assert "llm_node_id" not in data

        node = Node.query.get(data["id"])
        assert node.parent_id is None
        assert node.get_content() == "edited words"
        assert node.streaming_transcription is True
        assert Node.query.count() == 1
        # Draft is consumed.
        assert Draft.query.filter_by(session_id=sid).first() is None


class TestSaveAsNodeAgentic:
    def test_agentic_and_auto_generate_mirror_textmode_start(self, app):
        client, alice, draft = _setup(app)

        resp = client.post(
            _url(draft),
            json={"agentic": True, "auto_generate": True, "model": "gpt-5"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["task_id"] == "fake-task-id"

        system = Node.query.get(data["conversation_id"])
        user_node = Node.query.get(data["user_node_id"])
        llm_node = Node.query.get(data["llm_node_id"])
        assert data["id"] == user_node.id

        # Same shape /textmode/start builds: system → user → llm.
        assert system.parent_id is None
        assert user_node.parent_id == system.id
        assert llm_node.parent_id == user_node.id
        assert llm_node.node_type == "llm"
        assert llm_node.llm_model == "gpt-5"
        assert user_node.get_content() == "recorded words"
        assert user_node.streaming_transcription is True

        # The thread is an agentic textmode session: prompt pinned + stamped.
        assert system.prompt_key == "textmode"
        assert system.get_prompt_key() == "textmode"
        pins = NodeContextArtifact.query.filter_by(
            node_id=system.id, artifact_type="prompt",
        ).count()
        assert pins == 1

        # Privacy / AI usage flow from the draft to every node.
        for n in (system, user_node, llm_node):
            assert n.privacy_level == "private"
            assert n.ai_usage == "chat"

    def test_auto_generate_without_agentic_replies_under_plain_root(self, app):
        client, alice, draft = _setup(app)

        resp = client.post(_url(draft), json={"auto_generate": True})
        assert resp.status_code == 201
        data = resp.get_json()
        assert "conversation_id" not in data

        user_node = Node.query.get(data["user_node_id"])
        llm_node = Node.query.get(data["llm_node_id"])
        assert user_node.parent_id is None
        assert llm_node.parent_id == user_node.id
        # Model resolved server-side (no `model` sent): config DEFAULT.
        assert llm_node.llm_model == "gpt-5"

    def test_auto_generate_off_creates_no_placeholder(self, app):
        client, alice, draft = _setup(app)

        resp = client.post(
            _url(draft), json={"agentic": True, "auto_generate": False},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "llm_node_id" not in data
        assert Node.query.filter_by(node_type="llm").count() == 0
        assert Node.query.get(data["user_node_id"]).parent_id == data["conversation_id"]

    def test_spend_cap_saves_entry_but_skips_reply(self, app):
        client, alice, draft = _setup(app)

        with patch("backend.utils.spend.user_is_capped", return_value=True):
            resp = client.post(
                _url(draft), json={"agentic": True, "auto_generate": True},
            )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["spend_capped"] is True
        assert "llm_node_id" not in data
        # System + user nodes exist; the writing is never blocked.
        assert Node.query.get(data["conversation_id"]) is not None
        assert Node.query.get(data["user_node_id"]) is not None
        assert Node.query.filter_by(node_type="llm").count() == 0


class TestSaveAsNodeValidation:
    def test_agentic_rejected_for_reply_drafts(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        root = _make_node(alice)
        draft = _make_completed_draft(alice, parent_id=root.id)
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(_url(draft), json={"agentic": True})
        assert resp.status_code == 400
        assert "agentic" in resp.get_json()["error"].lower()
        # Nothing was saved; the draft is still there for a retry.
        assert Node.query.count() == 1
        assert Draft.query.filter_by(session_id=draft.session_id).first() is not None

    def test_flags_rejected_when_ai_usage_none(self, app):
        client, alice, draft = _setup(app, ai_usage="none")

        for body in ({"agentic": True}, {"auto_generate": True}):
            resp = client.post(_url(draft), json=body)
            assert resp.status_code == 400
            assert "ai_usage" in resp.get_json()["error"].lower()
        assert Node.query.count() == 0

    def test_unsupported_model_rejected(self, app):
        client, alice, draft = _setup(app)

        resp = client.post(
            _url(draft), json={"auto_generate": True, "model": "nope-1"},
        )
        assert resp.status_code == 400
        assert "model" in resp.get_json()["error"].lower()
        assert Node.query.count() == 0

    def test_foreign_session_404(self, app):
        client, alice, draft = _setup(app)
        bob = _make_user("bob")
        _db.session.commit()
        _login(client, bob.id)

        resp = client.post(_url(draft), json={"agentic": True})
        assert resp.status_code == 404
        assert Node.query.count() == 0
