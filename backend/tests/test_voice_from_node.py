"""Tests for POST /voice/from-node/<node_id>.

Verifies the 4-case behavior matrix:
  prompt present + user node  → processing (LLM placeholder created)
  prompt present + LLM node   → processing (TTS playback)
  no prompt      + user node  → processing (system prompt + LLM placeholder)
  no prompt      + LLM node   → processing (system prompt created)

Also verifies authorization and ancestor-walking prompt detection.
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

    from backend.routes.voice import voice_bp
    app.register_blueprint(voice_bp, url_prefix="/api/voice")

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

class TestVoiceFromNodeMatrix:
    """Test the 4-case behavior matrix for voice."""

    def test_prompt_present_user_node_returns_processing(self, app):
        """User node in a thread with prompt → create LLM child → processing."""
        client = app.test_client()

        alice = _make_user("alice")

        # Build chain: prompt_node → user_node
        prompt_node = _make_prompt_node(alice, "voice")
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my thoughts")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert "llm_node_id" in data

        # Verify LLM placeholder was created as child of user_node
        llm_node = Node.query.get(data["llm_node_id"])
        assert llm_node is not None
        assert llm_node.parent_id == user_node.id
        assert llm_node.node_type == "llm"

    def test_prompt_present_llm_node_returns_processing(self, app):
        """LLM node in a thread with prompt → processing mode (TTS playback)."""
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")

        # Build chain: prompt_node → user_node → llm_node
        prompt_node = _make_prompt_node(alice, "voice")
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my thoughts")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI response", node_type="llm",
                              llm_model="gpt-5")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert data["llm_node_id"] == llm_node.id
        assert data["parent_id"] == llm_node.id

    def test_no_prompt_user_node_creates_system_prompt_and_processing(
        self, app
    ):
        """User node with no prompt in chain → system prompt + LLM → processing."""
        client = app.test_client()

        alice = _make_user("alice")
        # Plain node — no prompt in ancestors
        user_node = _make_node(alice, content="just some text")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["mode"] == "processing"

        # Verify chain: user_node → system_prompt_node → llm_node
        llm_node = Node.query.get(data["llm_node_id"])
        system_node = Node.query.get(llm_node.parent_id)
        assert system_node.parent_id == user_node.id
        assert system_node.node_type == "user"
        assert system_node.is_system_prompt
        assert system_node.content is None
        prompt_artifact = NodeContextArtifact.query.filter_by(
            node_id=system_node.id, artifact_type="prompt",
        ).first()
        assert prompt_artifact is not None
        linked_prompt = UserPrompt.query.get(prompt_artifact.artifact_id)
        assert linked_prompt.prompt_key == "voice"

    def test_no_prompt_llm_node_creates_system_prompt_and_processing(
        self, app
    ):
        """LLM node with no prompt → system prompt child → processing (TTS)."""
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")

        # LLM node without any prompt ancestor (e.g. from converse)
        user_node = _make_node(alice, content="some text")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI reply", node_type="llm",
                              llm_model="gpt-5")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert data["llm_node_id"] == llm_node.id

        # Verify system prompt was created as child of llm_node
        system_node = Node.query.get(data["parent_id"])
        assert system_node.parent_id == llm_node.id
        assert system_node.is_system_prompt
        assert system_node.content is None
        linked_prompt = system_node.get_artifact("prompt")
        assert linked_prompt.prompt_key == "voice"


class TestVoiceFromNodeAncestorWalking:
    """Test that prompt detection walks the full ancestor chain."""

    def test_prompt_detected_at_root(self, app):
        """Prompt at root of a deep chain is found."""
        client = app.test_client()

        alice = _make_user("alice")

        # Deep chain: prompt → n1 → n2 → n3
        prompt_node = _make_prompt_node(alice, "voice")
        n1 = _make_node(alice, parent_id=prompt_node.id, content="a")
        n2 = _make_node(alice, parent_id=n1.id, content="b")
        n3 = _make_node(alice, parent_id=n2.id, content="c")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{n3.id}",
            json={"model": "gpt-5"},
        )

        # Should detect prompt → processing mode (user node with prompt)
        assert resp.status_code == 202
        assert resp.get_json()["mode"] == "processing"

    def test_prompt_detected_mid_chain(self, app):
        """Prompt in middle of chain (from prior resume) is found."""
        client = app.test_client()

        alice = _make_user("alice")

        # Chain: regular → prompt → user_node
        regular = _make_node(alice, content="original text")
        prompt_node = _make_prompt_node(alice, "voice",
                                        parent_id=regular.id)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="reflecting")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        assert resp.get_json()["mode"] == "processing"


class TestVoiceFromNodeAuth:
    """Test authorization for from-node endpoint."""

    def test_unauthorized_user_rejected(self, app):
        """User cannot start voice from someone else's node."""
        client = app.test_client()

        alice = _make_user("alice")
        bob = _make_user("bob")
        alice_node = _make_node(alice, content="alice's thoughts")
        _db.session.commit()

        _login(client, bob.id)
        resp = client.post(
            f"/api/voice/from-node/{alice_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 403

    def test_llm_node_authorized_via_parent(self, app):
        """User can start voice from an LLM node if parent belongs to them."""
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")

        alice_node = _make_node(alice, content="alice text")
        llm_node = _make_node(llm_user, parent_id=alice_node.id,
                              content="AI response", node_type="llm",
                              llm_model="gpt-5")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        # Should succeed (processing mode, no prompt)
        assert resp.status_code == 200

    def test_nonexistent_node_404(self, app):
        """Requesting from-node on non-existent node returns 404."""
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            "/api/voice/from-node/99999",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 404

    def test_unauthenticated_rejected(self, app):
        """Unauthenticated user is rejected."""
        client = app.test_client()
        resp = client.post(
            "/api/voice/from-node/1",
            json={"model": "gpt-5"},
        )
        assert resp.status_code in (401, 302)


class TestVoiceFromNodeAiUsageInheritance:
    """Test that ai_usage is inherited from the target node."""

    def test_inherits_ai_usage_from_target_node(self, app):
        """New nodes should inherit ai_usage from the target node."""
        client = app.test_client()

        alice = _make_user("alice")
        # Node with ai_usage="train"
        user_node = _make_node(alice, content="trainable content",
                               ai_usage="train")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()

        # The system prompt node should inherit ai_usage from the target
        llm_node = Node.query.get(data["llm_node_id"])
        system_node = Node.query.get(llm_node.parent_id)
        assert system_node.ai_usage == "train"
