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
    app.config["READ_DEFAULT_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5", "read": True},
    }

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.read import read_bp
    app.register_blueprint(read_bp, url_prefix="/api/read")
    from backend.routes.nodes import nodes_bp
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

    def test_inside_a_read_thread_it_reads_further_without_a_second_prompt(self, app):
        """The thread has its read prompt: the button makes a read turn
        under the current node, marked "_read", and attaches nothing —
        the whole thread stays the context. auto_generate does not
        apply: the click is the request."""
        import json
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        prompt = _make_prompt_node(alice, "read")
        comment = _make_node(alice, parent_id=prompt.id, content="more on tools")
        _db.session.commit()
        before = Node.query.count()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{comment.id}",
                           json={"model": "gpt-5", "auto_generate": False})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert "prompt_node_id" not in data
        llm_node = Node.query.get(data["llm_node_id"])
        assert llm_node.parent_id == comment.id
        assert llm_node.node_type == "llm"
        assert json.loads(llm_node.tool_calls_meta) == [{"name": "_read"}]
        assert Node.query.count() == before + 1
        assert data["task_id"] == "fake-task-id"

    def test_a_poc_thread_with_the_placeholder_in_the_text_reads_further_too(self, app):
        """The 2026-09-13 PoC copied the prompt into the node: no key, no
        link, {ca_tweets} in the content. Still a read thread."""
        import json
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        legacy = _make_node(alice, content="Read these.\n\n{ca_tweets?days=1}")
        reply = _make_node(alice, parent_id=legacy.id, content="verdict", node_type="llm")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{reply.id}", json={"model": "gpt-5"})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert "prompt_node_id" not in data
        llm_node = Node.query.get(data["llm_node_id"])
        assert llm_node.parent_id == reply.id
        assert json.loads(llm_node.tool_calls_meta) == [{"name": "_read"}]

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


# ── Tests: AI usage is always 'chat' ───────────────────────────────────


class TestFeedAiUsage:
    """The reply quotes other people's public tweets, which Loore has no
    licence to train on: a 'train' default or thread is lowered to
    'chat' on both nodes of a read (ca_feed.FEED_AI_USAGE)."""

    def test_train_default_lowered_to_chat_on_start(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post("/api/read/start", json={"model": "gpt-5"})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert Node.query.get(data["prompt_node_id"]).ai_usage == "chat"
        assert Node.query.get(data["llm_node_id"]).ai_usage == "chat"

    def test_train_thread_lowered_to_chat_from_node(self, app):
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        entry = _make_node(alice, content="entry", ai_usage="train")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert Node.query.get(data["prompt_node_id"]).ai_usage == "chat"
        assert Node.query.get(data["llm_node_id"]).ai_usage == "chat"
        assert Node.query.get(entry.id).ai_usage == "train"  # untouched


# ── Tests: cancel & rerun ──────────────────────────────────────────────


def _make_read_reply(alice, *, status="processing", batch=True,
                     provider="openai", task_id="old-task"):
    """A read prompt node with an LLM reply under it, optionally parked
    on a submitted batch."""
    import json
    prompt = _make_prompt_node(alice, "read")
    prompt.prompt_key = "read"
    reply = _make_node(alice, parent_id=prompt.id, content="[pending]",
                       node_type="llm", llm_model="gpt-5")
    reply.llm_task_status = status
    reply.llm_task_id = task_id
    if batch:
        reply.tool_calls_meta = json.dumps([{
            "name": "_batch", "batch_id": "batch_1", "custom_id": f"node-{reply.id}",
            "model": "gpt-5", "provider": provider,
            "submitted_at": "2026-09-16T08:00:00", "status": "submitted",
        }])
    _db.session.commit()
    return prompt, reply


def _llm_task(monkeypatch):
    """A fresh mock of the completion task module for one test: other
    test files install their own mock of backend.tasks.llm_completion,
    and the route imports it at call time."""
    module = MagicMock()
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion", module)
    return module.generate_llm_response


class TestRerun:
    def test_cancels_batch_and_reruns_live(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        import json
        from backend.utils import llm_batch
        cancelled = []
        monkeypatch.setattr(llm_batch, "openai_batch_cancel_one",
                            lambda key, batch_id: cancelled.append(batch_id))
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        prompt, reply = _make_read_reply(alice)

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert data["live"] is True
        assert data["cancelled_batches"] == ["batch_1"]
        assert cancelled == ["batch_1"]
        task.app.control.revoke \
            .assert_called_once_with("old-task")
        task.apply_async.assert_called_once()
        _, kwargs = task.apply_async.call_args
        assert kwargs["args"] == (prompt.id, reply.id, "gpt-5", alice.id)
        assert kwargs["kwargs"] == {"source_mode": None, "ca_live": True}
        fresh = Node.query.get(reply.id)
        # The new task id is on the node before the dispatch (the task's
        # superseded-poll guard compares against it).
        assert fresh.llm_task_id == kwargs["task_id"] == data["task_id"]
        assert fresh.llm_task_id != "old-task"
        assert fresh.llm_task_status == "processing"
        assert fresh.llm_task_progress == 0
        meta = json.loads(fresh.tool_calls_meta)
        # The old entry stays as history, marked cancelled; the task's
        # poll only looks at 'submitted' entries.
        assert [m["status"] for m in meta] == ["cancelled"]
        assert meta[0]["cancelled_at"]

    def test_batch_being_withdrawn_is_cancelled_by_the_rerun_too(self, app, monkeypatch):
        """An entry the poll is withdrawing (the user hit the spend cap)
        is still live: the rerun cancels it again, best effort, and
        marks it cancelled like a submitted one."""
        task = _llm_task(monkeypatch)
        import json
        from backend.utils import llm_batch
        cancelled = []
        monkeypatch.setattr(llm_batch, "openai_batch_cancel_one",
                            lambda key, batch_id: cancelled.append(batch_id))
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        prompt, reply = _make_read_reply(alice)
        meta = json.loads(reply.tool_calls_meta)
        meta[0]["status"] = "cancelling"
        meta[0]["cancel_requested_at"] = "2026-09-17T10:00:00"
        reply.tool_calls_meta = json.dumps(meta)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})

        assert resp.status_code == 202, resp.get_json()
        assert resp.get_json()["cancelled_batches"] == ["batch_1"]
        assert cancelled == ["batch_1"]
        task.apply_async.assert_called_once()
        meta = json.loads(Node.query.get(reply.id).tool_calls_meta)
        assert [m["status"] for m in meta] == ["cancelled"]
        assert meta[0]["cancel_requested_at"] == "2026-09-17T10:00:00"

    def test_resubmits_as_batch_when_not_live(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        from backend.utils import llm_batch
        monkeypatch.setattr(llm_batch, "openai_batch_cancel_one",
                            lambda key, batch_id: None)
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        prompt, reply = _make_read_reply(alice)

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={})

        assert resp.status_code == 202, resp.get_json()
        assert resp.get_json()["live"] is False
        _, kwargs = task.apply_async.call_args
        assert kwargs["kwargs"]["ca_live"] is False

    def test_cancel_failure_does_not_block_the_rerun(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        import json
        from backend.utils import llm_batch

        def boom(key, batch_id):
            raise RuntimeError("already ended")
        monkeypatch.setattr(llm_batch, "openai_batch_cancel_one", boom)
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _, reply = _make_read_reply(alice)

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})

        assert resp.status_code == 202, resp.get_json()
        meta = json.loads(Node.query.get(reply.id).tool_calls_meta)
        assert meta[0]["status"] == "cancelled"
        assert "already ended" in meta[0]["cancel_error"]

    def test_failed_reply_without_batch_reruns(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _, reply = _make_read_reply(alice, status="failed", batch=False)
        reply.llm_task_error = "boom"
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})

        assert resp.status_code == 202, resp.get_json()
        assert resp.get_json()["cancelled_batches"] == []
        fresh = Node.query.get(reply.id)
        assert fresh.llm_task_status == "processing"
        assert fresh.llm_task_error is None
        task.apply_async.assert_called_once()

    def test_completed_reply_is_left_alone(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        _, reply = _make_read_reply(alice, status="completed")

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})

        assert resp.status_code == 409
        task.apply_async.assert_not_called()

    def test_ordinary_llm_reply_is_not_a_read(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        entry = _make_node(alice, content="entry")
        reply = _make_node(alice, parent_id=entry.id, content="[pending]",
                           node_type="llm", llm_model="gpt-5")
        reply.llm_task_status = "processing"
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})

        assert resp.status_code == 400
        task.apply_async.assert_not_called()

    def test_non_admin_refused(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        client = app.test_client()
        bob = _make_user("bob")
        _, reply = _make_read_reply(bob)

        _login(client, bob.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})
        assert resp.status_code == 403

    def test_other_users_reply_rejected(self, app, monkeypatch):
        task = _llm_task(monkeypatch)
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        eve = _make_user("eve", is_admin=True)
        _, reply = _make_read_reply(alice)

        _login(client, eve.id)
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})
        assert resp.status_code == 403


# ── Tests: the read never runs under the agentic prompt ────────────────


class TestStripAgenticPrompts:
    """The context a read is built from drops Voice / Text mode prompt
    nodes wherever they sit and keeps the conversation around them."""

    def test_agentic_root_dropped_sharing_kept(self, app):
        from backend.utils.session_helpers import (
            chain_has_agentic_prompt, strip_agentic_prompts)
        alice = _make_user("alice", is_admin=True)
        voice = _make_prompt_node(alice, "voice")
        voice.prompt_key = "voice"
        sharing = _make_node(alice, parent_id=voice.id, content="my morning")
        reply = _make_node(alice, parent_id=sharing.id, content="reply",
                           node_type="llm", llm_model="gpt-5")
        read = _make_prompt_node(alice, "read_thread", parent_id=reply.id)
        read.prompt_key = "read_thread"
        _db.session.commit()

        chain, dropped = strip_agentic_prompts(
            [voice, sharing, reply, read], keep=read)

        assert dropped == [voice]
        assert chain == [sharing, reply, read]
        assert not chain_has_agentic_prompt(chain)

    def test_mid_thread_agentic_prompt_dropped_history_before_it_kept(self, app):
        from backend.utils.session_helpers import strip_agentic_prompts
        alice = _make_user("alice", is_admin=True)
        root = _make_node(alice, content="root sharing")
        textmode = _make_prompt_node(alice, "textmode", parent_id=root.id)
        textmode.prompt_key = "textmode"
        later = _make_node(alice, parent_id=textmode.id, content="later")
        read = _make_prompt_node(alice, "read_thread", parent_id=later.id)
        read.prompt_key = "read_thread"
        _db.session.commit()

        chain, dropped = strip_agentic_prompts(
            [root, textmode, later, read], keep=read)

        assert dropped == [textmode]
        assert chain == [root, later, read]

    def test_linked_prompt_without_stamp_is_still_recognised(self, app):
        # Roots attached before Node.prompt_key existed resolve the key
        # through the linked UserPrompt (Node.get_prompt_key).
        from backend.utils.session_helpers import strip_agentic_prompts
        alice = _make_user("alice", is_admin=True)
        voice = _make_prompt_node(alice, "voice")
        sharing = _make_node(alice, parent_id=voice.id, content="x")
        _db.session.commit()
        assert voice.prompt_key is None
        chain, dropped = strip_agentic_prompts([voice, sharing])
        assert dropped == [voice] and chain == [sharing]

    def test_nothing_to_drop(self, app):
        from backend.utils.session_helpers import strip_agentic_prompts
        alice = _make_user("alice", is_admin=True)
        read = _make_prompt_node(alice, "read")
        read.prompt_key = "read"
        _db.session.commit()
        chain, dropped = strip_agentic_prompts([read])
        assert dropped == [] and chain == [read]

    def test_keep_wins_over_the_key(self, app):
        from backend.utils.session_helpers import strip_agentic_prompts
        alice = _make_user("alice", is_admin=True)
        voice = _make_prompt_node(alice, "voice")
        voice.prompt_key = "voice"
        _db.session.commit()
        chain, dropped = strip_agentic_prompts([voice], keep=voice)
        assert dropped == [] and chain == [voice]


# ── Model choice (#355) ───────────────────────────────────────────────

MODELS_355 = {
    "claude-opus-4.6": {"provider": "anthropic", "display_name": "Opus 4.6",
                        "featured": True},
    "claude-opus-5": {"provider": "anthropic", "display_name": "Opus 5",
                      "deprecated": True},
    "gpt-6-luna": {"provider": "openai", "display_name": "GPT-6 Luna",
                   "read": True},
    "gpt-6-sol": {"provider": "openai", "display_name": "GPT-6 Sol",
                  "read": True},
}


@pytest.fixture
def app355(app):
    app.config["SUPPORTED_MODELS"] = MODELS_355
    app.config["DEFAULT_LLM_MODEL"] = "claude-opus-4.6"
    app.config["READ_DEFAULT_MODEL"] = "gpt-6-luna"
    return app


def _read_thread(alice, read_model="gpt-6-sol", chat_model="claude-opus-4.6"):
    """root → read prompt → read reply (read_model) → note → chat reply
    (chat_model) → note. Returns the nodes by name."""
    root = _make_node(alice, content="a thought")
    prompt = _make_prompt_node(alice, "read_thread", parent_id=root.id)
    read = _make_node(alice, parent_id=prompt.id, node_type="llm",
                      llm_model=read_model, content="picks")
    note = _make_node(alice, parent_id=read.id, content="about #2")
    chat = _make_node(alice, parent_id=note.id, node_type="llm",
                      llm_model=chat_model, content="sure")
    note2 = _make_node(alice, parent_id=chat.id, content="and #3?")
    _db.session.commit()
    return dict(root=root, prompt=prompt, read=read, note=note, chat=chat,
                note2=note2)


class TestModelChoice:
    def test_chat_default_skips_the_read_reply(self, app355):
        from backend.utils.llm_nodes import resolve_chat_model
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        # Under the read reply's note the nearest reply is the Sol read:
        # the chat default must not inherit it.
        assert resolve_chat_model(t["note"], alice) == ("claude-opus-4.6", "default")
        alice.preferred_model = "claude-opus-4.6"
        assert resolve_chat_model(t["note"], alice)[1] == "user_preference"
        assert resolve_chat_model(t["note2"], alice) == ("claude-opus-4.6", "predecessor")

    def test_read_default_skips_the_chat_reply(self, app355):
        from backend.utils.llm_nodes import resolve_read_model
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        assert resolve_read_model(t["note2"]) == ("gpt-6-sol", "predecessor")
        assert resolve_read_model(t["root"]) == ("gpt-6-luna", "default")
        assert resolve_read_model(None) == ("gpt-6-luna", "default")

    def test_read_default_ignores_a_read_on_a_non_read_model(self, app355):
        from backend.utils.llm_nodes import resolve_read_model
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice, read_model="claude-opus-4.6")
        assert resolve_read_model(t["note2"]) == ("gpt-6-luna", "default")

    def test_read_route_refuses_a_chat_model(self, app355):
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True)
        _db.session.commit()
        _login(client, alice.id)
        resp = client.post("/api/read/start", json={"model": "claude-opus-4.6"})
        assert resp.status_code == 400
        assert "GPT-6 Luna" in resp.get_json()["error"]

    def test_read_further_without_a_model_takes_the_threads_read_model(self, app355):
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{t['note2'].id}", json={})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model == "gpt-6-sol"

    def test_placeholder_under_a_read_reply_runs_on_a_read_model(self, app355):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        _render(t["read"])  # a finished read has its render pinned
        # Directly under the read reply a reply is another read.
        node, _ = create_llm_placeholder(t["read"].id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "gpt-6-sol"
        # Under the prompt (below a typed note) it is the first read.
        pnote = _make_node(alice, parent_id=t["prompt"].id, content="first")
        _db.session.commit()
        node, _ = create_llm_placeholder(pnote.id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "gpt-6-luna"
        # A note after the read reply makes it a chat: the model stays.
        node, _ = create_llm_placeholder(t["note"].id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "claude-opus-4.6"


def _mark(node, *entries):
    import json as _json
    node.tool_calls_meta = _json.dumps([{"name": e} for e in entries])
    _db.session.commit()


def _render(node):
    """Give *node* a pinned render: a read reply the task counts."""
    from backend.models import FeedRender
    _db.session.add(FeedRender(node_id=node.id))
    _db.session.commit()


class TestPlaceholderAgreesWithTheTask:
    """The placeholder guard applies the task's own rule (ca_feed.ca_turn),
    so failed or unrendered reads count the same on both sides (#356
    review, finding 2)."""

    def test_after_a_failed_first_read_the_next_reply_is_still_the_read(self, app355):
        from backend.utils.llm_nodes import create_llm_placeholder, reply_read_turn
        alice = _make_user("alice", is_admin=True)
        root = _make_node(alice, content="a thought")
        prompt = _make_prompt_node(alice, "read_thread", parent_id=root.id)
        failed = _make_node(alice, parent_id=prompt.id, node_type="llm",
                            llm_model="gpt-6-luna", content="failed")
        failed.llm_task_status = "failed"
        note = _make_node(alice, parent_id=failed.id, content="try again")
        _db.session.commit()
        assert reply_read_turn(note) == "read"
        node, _ = create_llm_placeholder(note.id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "gpt-6-luna"

    def test_a_reply_on_a_failed_read_further_is_a_chat(self, app355):
        from backend.utils.llm_nodes import create_llm_placeholder, reply_read_turn
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        _render(t["read"])
        further = _make_node(alice, parent_id=t["note2"].id, node_type="llm",
                             llm_model="gpt-6-sol", content="failed")
        _mark(further, "_read")
        assert reply_read_turn(further) == "chat"
        node, _ = create_llm_placeholder(further.id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "claude-opus-4.6"

    def test_directly_under_a_rendered_read_it_is_another_read(self, app355):
        from backend.utils.llm_nodes import reply_read_turn
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        _render(t["read"])
        assert reply_read_turn(t["read"]) == "read_again"
        assert reply_read_turn(t["note"]) == "chat"
        assert reply_read_turn(t["root"]) is None

    def test_a_deprecated_model_sent_explicitly_is_replaced(self, app355):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True, preferred_model="claude-opus-5")
        root = _make_node(alice, content="a thought")
        _db.session.commit()
        node, _ = create_llm_placeholder(root.id, "claude-opus-5", alice.id)
        # The saved preference is deprecated too: LLM_NAME decides.
        assert node.llm_model == "claude-opus-4.6"


class TestModelEndpoints:
    def test_models_carry_the_flags_and_skip_deprecated(self, app355):
        client = app355.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)
        models = {m["id"]: m for m in client.get("/api/nodes/models").get_json()["models"]}
        assert "claude-opus-5" not in models
        assert models["claude-opus-4.6"]["featured"] is True
        assert models["gpt-6-luna"]["read"] is True

    def test_purpose_read_and_a_deprecated_preference(self, app355):
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True, preferred_model="claude-opus-5")
        t = _read_thread(alice)
        _login(client, alice.id)
        read = client.get(f"/api/nodes/{t['note2'].id}/suggested-model?purpose=read").get_json()
        assert read == {"suggested_model": "gpt-6-sol", "source": "predecessor"}
        fresh = client.get("/api/nodes/default-model?purpose=read").get_json()
        assert fresh["suggested_model"] == "gpt-6-luna"
        chat = client.get("/api/nodes/default-model").get_json()
        assert chat == {"suggested_model": "claude-opus-4.6", "source": "default"}
        assert client.get("/api/nodes/default-model?purpose=x").status_code == 400


class TestModelWalkQueryCount:
    def test_walks_are_constant_in_thread_depth(self, app355):
        from sqlalchemy import event
        from backend.utils.llm_nodes import (
            reply_ai_usage, reply_read_turn, resolve_chat_model,
            resolve_read_model)
        alice = _make_user("alice", is_admin=True)
        parent = _make_node(alice, content="start")
        for i in range(200):
            reply = _make_node(alice, parent_id=parent.id, node_type="llm",
                               llm_model="claude-opus-5", content="r")
            parent = _make_node(alice, parent_id=reply.id, content="u")
        _db.session.commit()
        _db.session.expire_all()
        tip = Node.query.get(parent.id)
        statements = []
        listener = lambda *a, **k: statements.append(1)  # noqa: E731
        event.listen(_db.engine, "before_cursor_execute", listener)
        try:
            resolve_read_model(tip)
            resolve_chat_model(tip, alice)
            reply_read_turn(tip)
            reply_ai_usage(tip, alice)
        finally:
            event.remove(_db.engine, "before_cursor_execute", listener)
        # Each call: chain (2) + linked prompt keys (1) + read replies
        # (2), about 5 whatever the depth; a per-level walk made ~1,000
        # on a 400-node thread.
        assert len(statements) <= 28, len(statements)


# ── A reply's AI usage under a read (#362) ────────────────────────────

def _thread_with_read(alice, root_usage="train"):
    """root (the user's own, *root_usage*) → read prompt → read reply, both
    'chat' as the read routes make them. Returns the nodes by name."""
    root = _make_node(alice, content="a thought", ai_usage=root_usage)
    prompt = _make_prompt_node(alice, "read_thread", parent_id=root.id)
    read = _make_node(alice, parent_id=prompt.id, node_type="llm",
                      llm_model="gpt-6-sol", content="picks")
    _render(read)
    return dict(root=root, prompt=prompt, read=read)


class TestReplyAiUsage:
    def test_under_a_read_the_thread_decides_not_the_read(self, app355):
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="none")
        t = _thread_with_read(alice)
        assert (t["prompt"].ai_usage, t["read"].ai_usage) == ("chat", "chat")
        # The node above the read decides, not the account default.
        assert reply_ai_usage(t["read"], alice) == "train"
        assert reply_ai_usage(t["prompt"], alice) == "train"

    def test_a_read_at_the_root_takes_the_account_default(self, app355):
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        prompt = _make_prompt_node(alice, "read")
        read = _make_node(alice, parent_id=prompt.id, node_type="llm",
                          llm_model="gpt-6-sol", content="nothing today")
        _db.session.commit()
        # A pick-less read reply with no render yet: still the read's.
        assert reply_ai_usage(read, alice) == "train"
        alice.default_ai_usage = "chat"
        assert reply_ai_usage(read, alice) == "chat"

    def test_below_the_users_reply_its_choice_carries_on(self, app355):
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        t = _thread_with_read(alice)
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage="train")
        # The chat turn about the picks is stored 'chat' for the tweets in
        # its context; the reply under it takes the user's note, not it.
        chat = _make_node(alice, parent_id=note.id, node_type="llm",
                          llm_model="claude-opus-4.6", content="because",
                          ai_usage="chat")
        _db.session.commit()
        assert reply_ai_usage(note, alice) == "train"
        assert reply_ai_usage(chat, alice) == "train"
        # A note the user set to 'chat' is theirs to keep.
        note.ai_usage = "chat"
        _db.session.commit()
        assert reply_ai_usage(chat, alice) == "chat"

    def test_outside_a_read_the_parent_decides_as_before(self, app355):
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", default_ai_usage="train")
        entry = _make_node(alice, content="mine", ai_usage="train")
        # An LLM reply the user lowered by hand keeps its say.
        reply = _make_node(alice, parent_id=entry.id, node_type="llm",
                           llm_model="claude-opus-4.6", content="r",
                           ai_usage="chat")
        _db.session.commit()
        assert reply_ai_usage(entry, alice) == "train"
        assert reply_ai_usage(reply, alice) == "chat"
        assert reply_ai_usage(None, alice) == "train"

    def test_a_poc_prompt_is_seen_in_the_parents_own_text(self, app355):
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="none")
        root = _make_node(alice, content="a thought", ai_usage="train")
        poc = _make_node(alice, parent_id=root.id, content="Read {ca_tweets}")
        _db.session.commit()
        assert reply_ai_usage(poc, alice, parent_content=poc.get_content()) == "train"

    def test_a_placeholder_in_a_read_thread_is_stored_as_chat(self, app355):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        t = _thread_with_read(alice)
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage="train")
        _db.session.commit()
        # A chat turn about the picks: the tweets are in its context.
        node, _ = create_llm_placeholder(note.id, "claude-opus-4.6", alice.id,
                                         ai_usage="train")
        assert node.ai_usage == "chat"
        # 'none' is never raised.
        node, _ = create_llm_placeholder(note.id, "claude-opus-4.6", alice.id,
                                         ai_usage="none")
        assert node.ai_usage == "none"
        # Outside a read thread the caller's value stands.
        node, _ = create_llm_placeholder(t["root"].id, "claude-opus-4.6",
                                         alice.id, ai_usage="train")
        assert node.ai_usage == "train"

    def test_get_node_returns_the_reply_default(self, app355):
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True, default_ai_usage="none")
        t = _thread_with_read(alice)
        _login(client, alice.id)
        data = client.get(f"/api/nodes/{t['read'].id}").get_json()
        assert data["ai_usage"] == "chat"
        assert data["reply_ai_usage"] == "train"
        data = client.get(f"/api/nodes/{t['root'].id}").get_json()
        assert data["reply_ai_usage"] == "train"

    def test_a_none_the_walk_would_pass_still_holds(self, app355):
        # 'none' is only ever the user's choice: a chat turn they lowered
        # to it keeps what they write below out of AI too.
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        t = _thread_with_read(alice)
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage="train")
        chat = _make_node(alice, parent_id=note.id, node_type="llm",
                          llm_model="claude-opus-4.6", content="because",
                          ai_usage="none")
        _db.session.commit()
        assert reply_ai_usage(chat, alice) == "none"

    def test_an_llm_node_above_the_read_still_decides(self, app355):
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        root = _make_node(alice, content="a thought", ai_usage="train")
        earlier = _make_node(alice, parent_id=root.id, node_type="llm",
                             llm_model="claude-opus-4.6", content="hm",
                             ai_usage="chat")
        prompt = _make_prompt_node(alice, "read_thread", parent_id=earlier.id)
        read = _make_node(alice, parent_id=prompt.id, node_type="llm",
                          llm_model="gpt-6-sol", content="picks")
        _db.session.commit()
        assert reply_ai_usage(read, alice) == "chat"

    def test_get_node_walks_only_in_the_owners_read_thread(self, app355, monkeypatch):
        import backend.utils.llm_nodes as llm_nodes
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True, default_ai_usage="none")
        bob = _make_user("bob", default_ai_usage="train")
        t = _thread_with_read(alice)
        plain = _make_node(alice, content="no read here", ai_usage="train")
        t["read"].privacy_level = "public"
        _db.session.commit()
        walks = []
        real = llm_nodes.reply_ai_usage
        monkeypatch.setattr(llm_nodes, "reply_ai_usage",
                            lambda *a, **k: walks.append(1) or real(*a, **k))
        _login(client, alice.id)
        assert client.get(f"/api/nodes/{plain.id}").get_json()["reply_ai_usage"] == "train"
        assert walks == []
        assert client.get(f"/api/nodes/{t['read'].id}").get_json()["reply_ai_usage"] == "train"
        assert walks == [1]
        # Someone else viewing a public read reply gets its own value.
        from flask import g
        g.pop("_login_user", None)  # the fixture's app context outlives requests
        _login(client, bob.id)
        data = client.get(f"/api/nodes/{t['read'].id}").get_json()
        assert data["reply_ai_usage"] == "chat"
        assert walks == [1]


class TestReadBuiltRepliesStayChat:
    """A reply built with a read in its context is never raised to 'train'
    afterwards (#362): not in the node editor, not by the cascade."""

    def _thread(self, alice):
        t = _thread_with_read(alice, root_usage="chat")
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage="chat")
        chat = _make_node(alice, parent_id=note.id, node_type="llm",
                          llm_model="claude-opus-4.6", content="because",
                          ai_usage="chat")
        # A pick-less read reply ("nothing today") under a second prompt.
        prompt2 = _make_prompt_node(alice, "read_thread", parent_id=chat.id)
        empty = _make_node(alice, parent_id=prompt2.id, node_type="llm",
                           llm_model="gpt-6-sol", content="nothing today")
        _render(empty)
        return dict(t, note=note, chat=chat, empty=empty)

    def test_built_on_a_read(self, app355):
        from backend.utils.llm_nodes import built_on_a_read
        alice = _make_user("alice", is_admin=True)
        t = self._thread(alice)
        assert built_on_a_read(t["chat"]) and built_on_a_read(t["empty"])
        assert built_on_a_read(t["read"])
        assert not built_on_a_read(t["note"])      # the user's own
        assert not built_on_a_read(t["root"])

    def test_the_editor_refuses_train(self, app355):
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True)
        t = self._thread(alice)
        _login(client, alice.id)
        resp = client.put(f"/api/nodes/{t['chat'].id}",
                          json={"content": "because", "ai_usage": "train"})
        assert resp.status_code == 400
        assert Node.query.get(t["chat"].id).ai_usage == "chat"
        resp = client.put(f"/api/nodes/{t['note'].id}",
                          json={"content": "why #2?", "ai_usage": "train"})
        assert resp.status_code == 200, resp.get_json()

    def test_the_cascade_leaves_them(self, app355):
        from backend.utils.node_settings import apply_settings_to_descendants
        alice = _make_user("alice", is_admin=True)
        t = self._thread(alice)
        changed = apply_settings_to_descendants(t["root"], alice.id,
                                                ai_usage="train")
        _db.session.commit()
        assert {n.id for n in changed} == {t["note"].id}
        for name in ("prompt", "read", "chat", "empty"):
            assert Node.query.get(t[name].id).ai_usage == "chat", name
        # From below the read prompt the rule holds too.
        t["note"].ai_usage = "chat"
        _db.session.commit()
        changed = apply_settings_to_descendants(t["note"], alice.id,
                                                ai_usage="train")
        assert changed == []
