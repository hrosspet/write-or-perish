"""Tests for /api/textmode and source_mode propagation on /nodes/<id>/llm.

Covers:
- POST /textmode/start: success, validation (empty content, bad privacy),
  privacy_level + ai_usage honored, creates system/user/LLM nodes.
- POST /textmode/<conv_id>/message: requires parent_id, foreign auth,
  parent-not-descendant rejection, privacy inheritance from parent.
- GET  /textmode/from-node/<id>: foreign auth.
- POST /nodes/<id>/llm: forwards source_mode to create_llm_placeholder.
"""

import os
import sys
from unittest.mock import MagicMock, patch

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

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
from backend.models import User, Node            # noqa: E402
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

    from backend.routes.textmode import textmode_bp
    from backend.routes.nodes import nodes_bp
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")

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
               privacy_level="private", ai_usage="chat", human_owner=None,
               llm_model=None):
    n = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent_id,
        node_type=node_type,
        llm_model=llm_model,
        privacy_level=privacy_level,
        ai_usage=ai_usage,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


# ── /textmode/start ──────────────────────────────────────────────────────

class TestTextmodeStart:
    def test_success_creates_system_user_llm_nodes(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(
            "/api/textmode/start",
            json={"content": "my first message", "model": "gpt-5"},
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert "conversation_id" in data
        assert "user_node_id" in data
        assert "llm_node_id" in data
        assert data["task_id"] == "fake-task-id"

        system = Node.query.get(data["conversation_id"])
        user_node = Node.query.get(data["user_node_id"])
        llm_node = Node.query.get(data["llm_node_id"])
        assert system.parent_id is None
        assert user_node.parent_id == system.id
        assert llm_node.parent_id == user_node.id
        assert llm_node.node_type == "llm"
        assert user_node.get_content() == "my first message"

    def test_rejects_empty_content(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post("/api/textmode/start", json={"content": "   "})
        assert resp.status_code == 400

        resp = client.post("/api/textmode/start", json={})
        assert resp.status_code == 400

    def test_rejects_invalid_privacy_level(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(
            "/api/textmode/start",
            json={"content": "hi", "privacy_level": "bogus"},
        )
        assert resp.status_code == 400
        assert "privacy" in resp.get_json()["error"].lower()

    def test_honors_privacy_level_and_ai_usage(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(
            "/api/textmode/start",
            json={
                "content": "public thought",
                "privacy_level": "public",
                "ai_usage": "none",
            },
        )
        assert resp.status_code == 202
        data = resp.get_json()
        system = Node.query.get(data["conversation_id"])
        user_node = Node.query.get(data["user_node_id"])
        assert system.privacy_level == "public"
        assert user_node.privacy_level == "public"
        assert user_node.ai_usage == "none"

    def test_rejects_unauthenticated(self, app):
        client = app.test_client()
        resp = client.post("/api/textmode/start", json={"content": "hi"})
        assert resp.status_code == 401


# ── /textmode/<conv_id>/message ──────────────────────────────────────────

class TestTextmodeAddMessage:
    def _start_conversation(self, client):
        resp = client.post(
            "/api/textmode/start",
            json={"content": "first", "model": "gpt-5"},
        )
        assert resp.status_code == 202
        return resp.get_json()

    def test_success_with_parent_id(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)
        conv = self._start_conversation(client)

        resp = client.post(
            f"/api/textmode/{conv['conversation_id']}/message",
            json={
                "content": "follow-up",
                "parent_id": conv["llm_node_id"],
                "model": "gpt-5",
            },
        )
        assert resp.status_code == 202
        data = resp.get_json()
        user_node = Node.query.get(data["user_node_id"])
        assert user_node.parent_id == conv["llm_node_id"]
        assert user_node.get_content() == "follow-up"

    def test_requires_parent_id(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)
        conv = self._start_conversation(client)

        resp = client.post(
            f"/api/textmode/{conv['conversation_id']}/message",
            json={"content": "no parent"},
        )
        assert resp.status_code == 400

    def test_foreign_conversation_is_forbidden(self, app):
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        # Build alice's conversation chain directly in the DB so we don't
        # need to log alice in via the test client.
        alice_system = _make_node(alice, content="(system)")
        alice_user_msg = _make_node(alice, parent_id=alice_system.id,
                                    content="alice msg")
        _db.session.commit()

        bob_client = app.test_client()
        _login(bob_client, bob.id)
        resp = bob_client.post(
            f"/api/textmode/{alice_system.id}/message",
            json={
                "content": "sneaky",
                "parent_id": alice_user_msg.id,
            },
        )
        assert resp.status_code == 403

    def test_parent_id_not_in_conversation_rejected(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        # Start two independent conversations; parent_id from conv B
        # should not be accepted as a descendant of conv A.
        conv_a = self._start_conversation(client)
        conv_b = self._start_conversation(client)

        resp = client.post(
            f"/api/textmode/{conv_a['conversation_id']}/message",
            json={
                "content": "cross-convo",
                "parent_id": conv_b["llm_node_id"],
            },
        )
        assert resp.status_code == 400
        assert "conversation" in resp.get_json()["error"].lower()

    def test_privacy_inherits_from_parent(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(
            "/api/textmode/start",
            json={"content": "pub", "privacy_level": "public"},
        )
        conv = resp.get_json()

        resp = client.post(
            f"/api/textmode/{conv['conversation_id']}/message",
            json={
                "content": "still public?",
                "parent_id": conv["llm_node_id"],
            },
        )
        data = resp.get_json()
        assert Node.query.get(data["user_node_id"]).privacy_level == "public"


# ── /textmode/from-node/<id> ─────────────────────────────────────────────

class TestTextmodeFromNode:
    def test_foreign_node_forbidden(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        alice_node = _make_node(alice, content="hers")
        _db.session.commit()

        _login(client, bob.id)
        resp = client.get(f"/api/textmode/from-node/{alice_node.id}")
        assert resp.status_code == 403

    def test_returns_chronological_chain(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(alice, content="root")
        child = _make_node(alice, parent_id=root.id, content="child")
        grandchild = _make_node(
            alice, parent_id=child.id, content="grandchild",
        )
        _db.session.commit()

        _login(client, alice.id)
        resp = client.get(f"/api/textmode/from-node/{grandchild.id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["conversation_id"] == root.id
        contents = [m["content"] for m in data["messages"]]
        assert contents == ["child", "grandchild"]  # root excluded


# ── POST /nodes/<id>/llm source_mode propagation ─────────────────────────

class TestNodesLlmSourceMode:
    def test_source_mode_forwarded_to_create_llm_placeholder(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        parent = _make_node(alice, content="parent")
        _db.session.commit()

        _login(client, alice.id)
        with patch(
            "backend.routes.nodes.create_llm_placeholder",
            wraps=None,
        ) as mock_create:
            # Return value must be (llm_node, task_id)
            llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
            llm_node = _make_node(llm_user, parent_id=parent.id,
                                  node_type="llm", llm_model="gpt-5")
            _db.session.commit()
            mock_create.return_value = (llm_node, "task-123")

            resp = client.post(
                f"/api/nodes/{parent.id}/llm",
                json={"model": "gpt-5", "source_mode": "textmode"},
            )
            assert resp.status_code == 202
            assert mock_create.called
            kwargs = mock_create.call_args.kwargs
            assert kwargs.get("source_mode") == "textmode"

    def test_source_mode_omitted_defaults_to_none(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        parent = _make_node(alice, content="parent")
        _db.session.commit()

        _login(client, alice.id)
        with patch(
            "backend.routes.nodes.create_llm_placeholder",
        ) as mock_create:
            llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
            llm_node = _make_node(llm_user, parent_id=parent.id,
                                  node_type="llm", llm_model="gpt-5")
            _db.session.commit()
            mock_create.return_value = (llm_node, "task-123")

            resp = client.post(
                f"/api/nodes/{parent.id}/llm",
                json={"model": "gpt-5"},
            )
            assert resp.status_code == 202
            kwargs = mock_create.call_args.kwargs
            assert kwargs.get("source_mode") is None

    def test_invalid_source_mode_rejected(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        parent = _make_node(alice, content="parent")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/nodes/{parent.id}/llm",
            json={"model": "gpt-5", "source_mode": "hack"},
        )
        assert resp.status_code == 400
        assert "source_mode" in resp.get_json()["error"].lower()


# ── Cycle guard on ancestor walks ────────────────────────────────────────

class TestTextmodeCycleGuard:
    def test_self_referential_parent_does_not_hang_message(self, app):
        """parent_id whose ancestor chain loops back on itself must return
        in finite time — visited-set + MAX_HOPS prevent infinite looping."""
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        # Legitimate conversation root
        system_node = _make_node(alice, content="(system)")
        _db.session.commit()

        # Create a second node and force a self-loop: node.parent_id = node.id
        loop_node = _make_node(alice, parent_id=system_node.id, content="loop")
        _db.session.commit()
        loop_node.parent_id = loop_node.id
        _db.session.commit()

        _login(client, alice.id)

        # add_message must walk from loop_node up to system_node, see the
        # cycle, and reject rather than hang.
        import signal

        def _timeout(signum, frame):
            raise TimeoutError("cycle guard did not return in finite time")

        old_handler = signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(5)
        try:
            resp = client.post(
                f"/api/textmode/{system_node.id}/message",
                json={"content": "x", "parent_id": loop_node.id},
            )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        # Cycle means we never reach system_node → not-a-descendant 400.
        assert resp.status_code == 400

    def test_self_referential_parent_does_not_hang_from_node(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        loop_node = _make_node(alice, content="self-loop")
        _db.session.commit()
        loop_node.parent_id = loop_node.id
        _db.session.commit()

        _login(client, alice.id)

        import signal

        def _timeout(signum, frame):
            raise TimeoutError("from-node cycle guard hung")

        old_handler = signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(5)
        try:
            resp = client.get(f"/api/textmode/from-node/{loop_node.id}")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        # The walk terminates (single-node chain where the loop is caught
        # on the second iteration); the node is treated as its own root.
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["conversation_id"] == loop_node.id


# ── _get_previous_source_mode ────────────────────────────────────────────

def _load_real_get_previous_source_mode():
    """Load the real _get_previous_source_mode function from
    backend/tasks/llm_completion.py without triggering the full Celery +
    llm-providers import chain. We extract the function source and exec
    it in a minimal namespace."""
    import ast
    import os as _os
    src_path = _os.path.join(
        _os.path.dirname(__file__), "..", "tasks", "llm_completion.py",
    )
    with open(src_path) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_get_previous_source_mode"):
            ns = {"json": __import__("json")}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         src_path, "exec"), ns)
            return ns["_get_previous_source_mode"]
    raise RuntimeError("_get_previous_source_mode not found in source")


class TestGetPreviousSourceMode:
    def test_returns_none_when_no_llm_nodes(self, app):
        fn = _load_real_get_previous_source_mode()
        alice = _make_user("alice")
        _db.session.commit()
        chain = [
            _make_node(alice, content="root"),
            _make_node(alice, content="child"),
        ]
        assert fn(chain) is None

    def test_finds_most_recent_mode_marker(self, app):
        """_get_previous_source_mode should walk the chain in reverse and
        return the source_mode of the nearest LLM node that stored one."""
        import json as _json
        fn = _load_real_get_previous_source_mode()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        _db.session.commit()

        root = _make_node(alice, content="root")
        older_llm = _make_node(
            llm_user, parent_id=root.id, content="older",
            node_type="llm", llm_model="gpt-5",
        )
        older_llm.tool_calls_meta = _json.dumps([
            {"name": "_mode", "source_mode": "voice"},
        ])
        user_msg = _make_node(
            alice, parent_id=older_llm.id, content="reply",
        )
        newer_llm = _make_node(
            llm_user, parent_id=user_msg.id, content="newer",
            node_type="llm", llm_model="gpt-5",
        )
        newer_llm.tool_calls_meta = _json.dumps([
            {"name": "_mode", "source_mode": "textmode"},
        ])
        _db.session.commit()

        chain = [root, older_llm, user_msg, newer_llm]
        assert fn(chain) == "textmode"
        # Truncate the chain to before newer_llm: should fall back to voice.
        assert fn(chain[:3]) == "voice"

    def test_ignores_llm_nodes_without_mode_marker(self, app):
        import json as _json
        fn = _load_real_get_previous_source_mode()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        _db.session.commit()

        root = _make_node(alice, content="root")
        llm_no_mode = _make_node(
            llm_user, parent_id=root.id, content="old, no marker",
            node_type="llm", llm_model="gpt-5",
        )
        llm_no_mode.tool_calls_meta = _json.dumps([
            {"name": "propose_todo", "status": "success"},
        ])
        _db.session.commit()

        assert fn([root, llm_no_mode]) is None
