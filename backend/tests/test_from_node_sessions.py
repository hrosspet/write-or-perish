"""Tests for POST /reflect/from-node/<node_id> and /orient/from-node/<node_id>.

Verifies the 4-case behavior matrix:
  prompt present + user node  → processing (LLM placeholder created)
  prompt present + LLM node   → recording  (parent_id = context node)
  no prompt      + user node  → processing (system prompt + LLM placeholder)
  no prompt      + LLM node   → recording  (system prompt created, parent_id = it)

Also verifies authorization and ancestor-walking prompt detection.

NOTE: This file is named test_from_node_* (not test_reflect_*) so that it
is collected alphabetically before test_quotes.py, which replaces
backend.models with a MagicMock at module level.  The force-import pattern
used here (shared with test_audio_access.py) cannot recover from that.
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
# _create_llm_placeholder gets our mock instead of triggering
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
for _mod in [k for k in list(sys.modules)
             if k == "flask_login" or k.startswith("backend.")]:
    _m = sys.modules[_mod]
    if isinstance(_m, MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node            # noqa: E402
import backend.models as _real_backend_models    # noqa: E402

# Re-register task mock after force-import loop
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module


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

    from backend.routes.reflect import reflect_bp
    from backend.routes.orient import orient_bp
    app.register_blueprint(reflect_bp, url_prefix="/api/reflect")
    app.register_blueprint(orient_bp, url_prefix="/api/orient")

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
               llm_model=None, ai_usage="chat"):
    n = Node(
        user_id=user.id,
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


# ── Tests: Reflect from-node ─────────────────────────────────────────────

class TestReflectFromNodeMatrix:
    """Test the 4-case behavior matrix for reflect."""

    def test_prompt_present_user_node_returns_processing(self, app):
        """User node in a thread with prompt → create LLM child → processing."""
        client = app.test_client()

        alice = _make_user("alice")
        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "reflect")

        # Build chain: prompt_node → user_node
        prompt_node = _make_node(alice, content=prompt_text)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my thoughts")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/reflect/from-node/{user_node.id}",
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

    def test_prompt_present_llm_node_returns_recording(self, app):
        """LLM node in a thread with prompt → recording mode."""
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "reflect")

        # Build chain: prompt_node → user_node → llm_node
        prompt_node = _make_node(alice, content=prompt_text)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my thoughts")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI response", node_type="llm",
                              llm_model="gpt-5")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/reflect/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "recording"
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
            f"/api/reflect/from-node/{user_node.id}",
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

        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "reflect")
        assert system_node.get_content() == prompt_text

    def test_no_prompt_llm_node_creates_system_prompt_and_recording(
        self, app
    ):
        """LLM node with no prompt → system prompt child → recording."""
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
            f"/api/reflect/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "recording"

        # Verify system prompt was created as child of llm_node
        system_node = Node.query.get(data["parent_id"])
        assert system_node.parent_id == llm_node.id

        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "reflect")
        assert system_node.get_content() == prompt_text


class TestReflectFromNodeAncestorWalking:
    """Test that prompt detection walks the full ancestor chain."""

    def test_prompt_detected_at_root(self, app):
        """Prompt at root of a deep chain is found."""
        client = app.test_client()

        alice = _make_user("alice")
        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "reflect")

        # Deep chain: prompt → n1 → n2 → n3
        prompt_node = _make_node(alice, content=prompt_text)
        n1 = _make_node(alice, parent_id=prompt_node.id, content="a")
        n2 = _make_node(alice, parent_id=n1.id, content="b")
        n3 = _make_node(alice, parent_id=n2.id, content="c")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/reflect/from-node/{n3.id}",
            json={"model": "gpt-5"},
        )

        # Should detect prompt → processing mode (user node with prompt)
        assert resp.status_code == 202
        assert resp.get_json()["mode"] == "processing"

    def test_prompt_detected_mid_chain(self, app):
        """Prompt in middle of chain (from prior resume) is found."""
        client = app.test_client()

        alice = _make_user("alice")
        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "reflect")

        # Chain: regular → prompt → user_node
        regular = _make_node(alice, content="original text")
        prompt_node = _make_node(alice, parent_id=regular.id,
                                 content=prompt_text)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="reflecting")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/reflect/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        assert resp.get_json()["mode"] == "processing"


class TestReflectFromNodeAuth:
    """Test authorization for from-node endpoint."""

    def test_unauthorized_user_rejected(self, app):
        """User cannot start reflect from someone else's node."""
        client = app.test_client()

        alice = _make_user("alice")
        bob = _make_user("bob")
        alice_node = _make_node(alice, content="alice's thoughts")
        _db.session.commit()

        _login(client, bob.id)
        resp = client.post(
            f"/api/reflect/from-node/{alice_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 403

    def test_llm_node_authorized_via_parent(self, app):
        """User can reflect from an LLM node if parent belongs to them."""
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
            f"/api/reflect/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        # Should succeed (recording mode, no prompt)
        assert resp.status_code == 200

    def test_nonexistent_node_404(self, app):
        """Requesting from-node on non-existent node returns 404."""
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            "/api/reflect/from-node/99999",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 404

    def test_unauthenticated_rejected(self, app):
        """Unauthenticated user is rejected."""
        client = app.test_client()
        resp = client.post(
            "/api/reflect/from-node/1",
            json={"model": "gpt-5"},
        )
        assert resp.status_code in (401, 302)


# ── Tests: Orient from-node ──────────────────────────────────────────────

class TestOrientFromNodeMatrix:
    """Test the 4-case behavior matrix for orient (mirrors reflect)."""

    def test_prompt_present_user_node_returns_processing(self, app):
        client = app.test_client()

        alice = _make_user("alice")
        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "orient")

        prompt_node = _make_node(alice, content=prompt_text)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my priorities")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/orient/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert "llm_node_id" in data

    def test_prompt_present_llm_node_returns_recording(self, app):
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        from backend.utils.prompts import get_user_prompt
        prompt_text = get_user_prompt(alice.id, "orient")

        prompt_node = _make_node(alice, content=prompt_text)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my priorities")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI orient", node_type="llm",
                              llm_model="gpt-5")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/orient/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "recording"
        assert data["parent_id"] == llm_node.id

    def test_no_prompt_user_node_creates_system_prompt_and_processing(
        self, app
    ):
        client = app.test_client()

        alice = _make_user("alice")
        user_node = _make_node(alice, content="just some text")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/orient/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["mode"] == "processing"

        # Verify system prompt is the orient prompt
        llm_node = Node.query.get(data["llm_node_id"])
        system_node = Node.query.get(llm_node.parent_id)

        from backend.utils.prompts import get_user_prompt
        orient_prompt = get_user_prompt(alice.id, "orient")
        assert system_node.get_content() == orient_prompt

    def test_no_prompt_llm_node_creates_system_prompt_and_recording(
        self, app
    ):
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        user_node = _make_node(alice, content="some text")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI reply", node_type="llm",
                              llm_model="gpt-5")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/orient/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "recording"

        system_node = Node.query.get(data["parent_id"])
        from backend.utils.prompts import get_user_prompt
        orient_prompt = get_user_prompt(alice.id, "orient")
        assert system_node.get_content() == orient_prompt
