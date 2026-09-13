"""Tests for POST /api/read/start and /api/read/from-node/<id> (the
Community Archive reading entry points, PoC 2026-09-13).

Both attach a prompt by reference (an empty node linked to the UserPrompt
row through NodeContextArtifact, prompt_key stamped) and hang the LLM
placeholder under it; both are admin-only while {ca_tweets} is.
"""
import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

# Mock optional heavy deps that may not be installed locally
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

# Pre-mock the LLM task module so the lazy import inside
# create_llm_placeholder_node gets our mock instead of triggering
# the full celery/ffmpeg import chain.
_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

# ── Force-import real modules ────────────────────────────────────────────
# Only evict specific mocks that other test files may have installed.
# Do NOT blanket-remove all backend.* mocks — this file legitimately mocks
# backend.tasks.llm_completion above and that must be preserved.
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node, NodeContextArtifact, UserPrompt  # noqa: E402
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
    }

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.read import read_bp
    app.register_blueprint(read_bp, url_prefix="/api/read")

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


def _make_node(user, parent_id=None, content="hello", node_type="user",
               llm_model=None, ai_usage="chat", human_owner=None):
    n = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent_id,
        node_type=node_type,
        llm_model=llm_model,
        privacy_level="private",
        ai_usage=ai_usage,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


def _make_prompt_node(user, prompt_key, parent_id=None):
    """Create a system prompt node linked via NodeContextArtifact."""
    from backend.models import NodeContextArtifact
    from backend.utils.prompts import get_user_prompt_record
    record = get_user_prompt_record(user.id, prompt_key)
    n = Node(
        user_id=user.id,
        human_owner_id=user.id,
        parent_id=parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    _db.session.add(n)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=n.id, artifact_type="prompt", artifact_id=record.id,
    ))
    _db.session.flush()
    return n


# ── Tests: Voice from-node ─────────────────────────────────────────────


# ── Tests ─────────────────────────────────────────────────────────────


class TestReadStart:
    def test_admin_gets_root_prompt_node_and_placeholder(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post("/api/read/start", json={"model": "gpt-5"})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        prompt_node = Node.query.get(data["prompt_node_id"])
        llm_node = Node.query.get(data["llm_node_id"])
        assert prompt_node.parent_id is None
        assert prompt_node.content is None          # reference, not a copy
        assert prompt_node.is_system_prompt
        assert prompt_node.prompt_key == "read"
        prompt_artifact = NodeContextArtifact.query.filter_by(
            node_id=prompt_node.id, artifact_type="prompt",
        ).first()
        assert prompt_artifact is not None
        assert UserPrompt.query.get(prompt_artifact.artifact_id).prompt_key == "read"
        assert "{ca_tweets?days=1}" in prompt_node.get_content()
        assert llm_node.parent_id == prompt_node.id
        assert llm_node.node_type == "llm"

    def test_auto_generate_off_creates_only_the_prompt_node(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post("/api/read/start",
                           json={"model": "gpt-5", "auto_generate": False})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert "llm_node_id" not in data
        prompt_node = Node.query.get(data["prompt_node_id"])
        assert prompt_node.prompt_key == "read"
        assert prompt_node.content is None
        assert Node.query.count() == 1

    def test_non_admin_refused_before_any_node_exists(self, app):
        client = app.test_client()
        bob = _make_user("bob")
        _db.session.commit()

        _login(client, bob.id)
        resp = client.post("/api/read/start", json={"model": "gpt-5"})

        assert resp.status_code == 403
        assert "not available" in resp.get_json()["error"]
        assert Node.query.count() == 0

    def test_unsupported_model_rejected(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post("/api/read/start", json={"model": "nope"})
        assert resp.status_code == 400
        assert Node.query.count() == 0


class TestReadFromNode:
    def test_prompt_attached_under_node_with_placeholder_below(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        entry = _make_node(alice, content="morning entry")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        prompt_node = Node.query.get(data["prompt_node_id"])
        llm_node = Node.query.get(data["llm_node_id"])
        assert prompt_node.parent_id == entry.id
        assert prompt_node.content is None
        assert prompt_node.prompt_key == "read_thread"
        assert prompt_node.get_artifact("prompt").prompt_key == "read_thread"
        assert prompt_node.ai_usage == entry.ai_usage
        assert prompt_node.privacy_level == entry.privacy_level
        assert llm_node.parent_id == prompt_node.id

    def test_auto_generate_off_attaches_only_the_prompt(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        entry = _make_node(alice, content="entry")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5", "auto_generate": False})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert "llm_node_id" not in data
        prompt_node = Node.query.get(data["prompt_node_id"])
        assert prompt_node.parent_id == entry.id
        assert prompt_node.prompt_key == "read_thread"
        assert Node.query.count() == 2

    def test_works_inside_an_agentic_thread(self, app):
        """A textmode thread keeps its own root prompt; the read prompt is
        appended under the current node, not swapped in for it."""
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        root = _make_prompt_node(alice, "textmode")
        entry = _make_node(alice, parent_id=root.id, content="entry")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})
        assert resp.status_code == 202
        prompt_node = Node.query.get(resp.get_json()["prompt_node_id"])
        assert prompt_node.parent_id == entry.id
        assert prompt_node.prompt_key == "read_thread"
        assert root.get_prompt_key() == "textmode"

    def test_non_admin_refused(self, app):
        client = app.test_client()
        bob = _make_user("bob")
        entry = _make_node(bob, content="entry")
        _db.session.commit()

        _login(client, bob.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})
        assert resp.status_code == 403
        assert Node.query.count() == 1

    def test_other_users_node_rejected(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        bob = _make_user("bob")
        entry = _make_node(bob, content="bob's entry")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})
        assert resp.status_code == 403
        assert Node.query.count() == 1

    def test_ai_usage_none_rejected(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        entry = _make_node(alice, content="entry", ai_usage="none")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})
        assert resp.status_code == 400
        assert Node.query.count() == 1

    def test_missing_node_404(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _db.session.commit()
        _login(client, alice.id)
        resp = client.post("/api/read/from-node/9999", json={"model": "gpt-5"})
        assert resp.status_code == 404
