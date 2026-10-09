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
    # Glean is on by the user's own choice (#435): these tests are about
    # the read itself, not the switch (test_glean.py covers that).
    kwargs.setdefault("glean_enabled", True)
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

    def test_a_glean_always_creates_the_reply(self, app):
        """#435: the Glean button is the request, so auto_generate does
        not apply: the prompt and the reply under it, always."""
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        entry = _make_node(alice, content="entry")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5", "auto_generate": False})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        prompt_node = Node.query.get(data["prompt_node_id"])
        assert prompt_node.parent_id == entry.id
        assert prompt_node.prompt_key == "read_thread"
        assert Node.query.get(data["llm_node_id"]).parent_id == prompt_node.id
        assert Node.query.filter_by(node_type="user").count() == 2

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
        # A glean is always live (#435): the "_live" marker.
        assert json.loads(llm_node.tool_calls_meta) == [
            {"name": "_read"}, {"name": "_live"}]
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
        assert resp.status_code == 403
        assert resp.get_json()["code"] == "ai_usage_none"
        assert Node.query.count() == 1

    def test_ai_usage_none_above_rejected(self, app):
        # The read sends the whole thread above the node: a 'none' entry
        # anywhere there refuses it, before the prompt is attached.
        client = app.test_client()
        alice = _make_user("alice", is_admin=True)
        root = _make_node(alice, content="kept from AI", ai_usage="none")
        entry = _make_node(alice, content="entry", ai_usage="chat",
                           parent_id=root.id)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-5"})
        assert resp.status_code == 403
        assert resp.get_json()["code"] == "ai_usage_none"
        assert Node.query.count() == 2

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
    "claude-haiku-5.5": {"provider": "anthropic", "display_name": "Haiku 5.5",
                         "read": True, "chat": False},
}


@pytest.fixture
def app355(app):
    app.config["SUPPORTED_MODELS"] = MODELS_355
    app.config["DEFAULT_LLM_MODEL"] = "claude-opus-4.6"
    app.config["READ_DEFAULT_MODEL"] = "gpt-6-luna"
    # A glean stays on the provider of the user's chat model (#435).
    app.config["GLEAN_MODEL_ANTHROPIC"] = "claude-haiku-5.5"
    app.config["GLEAN_MODEL_OPENAI"] = "gpt-6-luna"
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
        # The conversation runs on OpenAI, like the thread's last read.
        t = _read_thread(alice, chat_model="gpt-6-luna")
        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{t['note2'].id}", json={})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model == "gpt-6-sol"

    def test_read_further_never_follows_a_read_of_another_provider(self, app355):
        """#435: the conversation runs on Anthropic, so the next glean does
        too, whatever model the thread's last read ran on."""
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)  # chat on Opus 4.6, the read on GPT-6 Sol
        _login(client, alice.id)
        resp = client.post(f"/api/read/from-node/{t['note2'].id}", json={})
        assert resp.status_code == 202, resp.get_json()
        assert (Node.query.get(resp.get_json()["llm_node_id"]).llm_model
                == "claude-haiku-5.5")

    def test_placeholder_under_a_read_reply_runs_on_a_read_model(self, app355):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice)
        _render(t["read"])  # a finished read has its render pinned
        # Directly under the read reply a reply is another read, on a read
        # model of the user's provider (Anthropic: the chat default), not
        # the GPT-6 Sol of the read before it (#435).
        node, _ = create_llm_placeholder(t["read"].id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "claude-haiku-5.5"
        # Under the prompt (below a typed note) it is the first read.
        pnote = _make_node(alice, parent_id=t["prompt"].id, content="first")
        _db.session.commit()
        node, _ = create_llm_placeholder(pnote.id, "claude-opus-4.6", alice.id)
        assert node.llm_model == "claude-haiku-5.5"
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
        # A read, on the read model of the user's provider (#435).
        assert node.llm_model == "claude-haiku-5.5"

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
        # The user's replies run on Anthropic (the chat default, Opus 4.6):
        # a glean does too (#435), not the thread's GPT-6 Sol read.
        read = client.get(f"/api/nodes/{t['note2'].id}/suggested-model?purpose=read").get_json()
        assert read == {"suggested_model": "claude-haiku-5.5",
                        "source": "provider_default"}
        fresh = client.get("/api/nodes/default-model?purpose=read").get_json()
        assert fresh["suggested_model"] == "claude-haiku-5.5"
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

    def test_below_the_users_reply_the_nearest_node_decides(self, app355):
        # Voice review 2026-09-29: an LLM reply after the recommendation
        # carries the thread's setting, so the walk no longer skips it.
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        t = _thread_with_read(alice)
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage="train")
        chat = _make_node(alice, parent_id=note.id, node_type="llm",
                          llm_model="claude-opus-4.6", content="because",
                          ai_usage="train")
        _db.session.commit()
        assert reply_ai_usage(note, alice) == "train"
        assert reply_ai_usage(chat, alice) == "train"
        # A value the user set by hand on either is theirs to keep.
        chat.ai_usage = "chat"
        _db.session.commit()
        assert reply_ai_usage(chat, alice) == "chat"
        note.ai_usage = "chat"
        chat.ai_usage = "train"
        _db.session.commit()
        assert reply_ai_usage(chat, alice) == "train"

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

    def test_an_ai_reply_to_the_users_reply_takes_the_threads_train(self, app355):
        # Voice review 2026-09-29: only the recommendation reply is 'chat'
        # by construction; the AI answer to the user's reply under it is
        # 'train' when the thread is.
        from backend.utils.llm_nodes import create_llm_placeholder, reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="none")
        t = _thread_with_read(alice)
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage=reply_ai_usage(t["read"], alice))
        _db.session.commit()
        assert note.ai_usage == "train"
        node, _ = create_llm_placeholder(note.id, "claude-opus-4.6", alice.id,
                                         ai_usage=note.ai_usage,
                                         enqueue=False)
        assert node.ai_usage == "train"
        # Through the route behind the thread page's "LLM Response".
        client = app355.test_client()
        _login(client, alice.id)
        resp = client.post(f"/api/nodes/{note.id}/llm",
                           json={"model": "claude-opus-4.6"})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["node_id"]).ai_usage == "train"

    def test_the_recommendation_node_stays_chat(self, app355):
        # The read reply that presents the picks is 'chat' however it is
        # asked for: under the read prompt (a read) or directly under a
        # read reply (a read again). 'none' is never raised.
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True, default_ai_usage="train")
        t = _thread_with_read(alice)
        for parent in (t["prompt"], t["read"]):
            node, _ = create_llm_placeholder(parent.id, "gpt-6-sol", alice.id,
                                             ai_usage="train", enqueue=False)
            assert node.ai_usage == "chat", parent
        # A reply marked 'none' is not generated at all (2026-10-01).
        from backend.utils.llm_nodes import AIUsageRefused
        with pytest.raises(AIUsageRefused):
            create_llm_placeholder(t["read"].id, "gpt-6-sol", alice.id,
                                   ai_usage="none", enqueue=False)
        # Outside a read thread the caller's value stands.
        node, _ = create_llm_placeholder(t["root"].id, "claude-opus-4.6",
                                         alice.id, ai_usage="train",
                                         enqueue=False)
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

    def test_a_skipped_read_node_set_to_none_does_not_decide(self, app355):
        # Voice review 2026-09-29: the rule "a skipped node set to 'none'
        # makes the reply 'none'" is gone. The Read's own nodes pass
        # nothing on, whatever their value.
        from backend.utils.llm_nodes import reply_ai_usage
        alice = _make_user("alice", is_admin=True, default_ai_usage="chat")
        t = _thread_with_read(alice)
        t["prompt"].ai_usage = "none"
        t["read"].ai_usage = "none"
        _db.session.commit()
        assert reply_ai_usage(t["read"], alice) == "train"
        # With the read at the root: the account default.
        prompt = _make_prompt_node(alice, "read")
        read = _make_node(alice, parent_id=prompt.id, node_type="llm",
                          llm_model="gpt-6-sol", content="picks",
                          ai_usage="none")
        _render(read)
        assert reply_ai_usage(read, alice) == "chat"
        # A node that is not the Read's still decides, 'none' included.
        note = _make_node(alice, parent_id=read.id, content="later",
                          ai_usage="none")
        _db.session.commit()
        assert reply_ai_usage(note, alice) == "none"

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


class TestNoLockBelowARead:
    """Voice review 2026-09-29: AI replies below a Read are not locked to
    'chat'. The editor and the "apply to my replies" cascade treat them
    like any other reply; only the Read's own nodes (the prompt and a read
    reply with picks, ca_feed.is_feed_node) still refuse 'train'."""

    def _thread(self, alice):
        t = _thread_with_read(alice, root_usage="chat")
        from backend.models import ExternalItem, FeedPick
        item = ExternalItem(user_id=alice.id, source="community_archive",
                            external_id="t1", author_handle="someone")
        item.set_content("a tweet")
        _db.session.add(item)
        _db.session.flush()
        _db.session.add(FeedPick(node_id=t["read"].id, user_id=alice.id,
                                 external_item_id=item.id, rank=1))
        note = _make_node(alice, parent_id=t["read"].id, content="why #2?",
                          ai_usage="chat")
        chat = _make_node(alice, parent_id=note.id, node_type="llm",
                          llm_model="claude-opus-4.6", content="because",
                          ai_usage="chat")
        _db.session.commit()
        return dict(t, note=note, chat=chat)

    def test_the_editor_allows_train_on_an_ai_reply_below_a_read(self, app355):
        client = app355.test_client()
        alice = _make_user("alice", is_admin=True)
        t = self._thread(alice)
        _login(client, alice.id)
        resp = client.put(f"/api/nodes/{t['chat'].id}",
                          json={"content": "because", "ai_usage": "train"})
        assert resp.status_code == 200, resp.get_json()
        assert Node.query.get(t["chat"].id).ai_usage == "train"
        # The recommendation node itself still refuses it.
        resp = client.put(f"/api/nodes/{t['read'].id}",
                          json={"content": "picks", "ai_usage": "train"})
        assert resp.status_code == 400
        assert Node.query.get(t["read"].id).ai_usage == "chat"

    def test_the_cascade_raises_replies_below_a_read(self, app355):
        from backend.utils.node_settings import apply_settings_to_descendants
        alice = _make_user("alice", is_admin=True)
        t = self._thread(alice)
        changed = apply_settings_to_descendants(t["root"], alice.id,
                                                ai_usage="train")
        _db.session.commit()
        assert {n.id for n in changed} == {t["note"].id, t["chat"].id}
        for name in ("prompt", "read"):
            assert Node.query.get(t[name].id).ai_usage == "chat", name


# ── Read-only models: "chat": False (Peter, 2026-10-02) ────────────────

MODELS_RO = dict(MODELS_355, **{
    "gpt-6.1-sol": {"provider": "openai", "display_name": "GPT-6.1 Sol",
                    "read": True, "chat": False},
    "claude-sonnet-5.5": {"provider": "anthropic",
                          "display_name": "Sonnet 5.5",
                          "read": True, "chat": False},
})


@pytest.fixture
def app_ro(app):
    app.config["SUPPORTED_MODELS"] = MODELS_RO
    app.config["DEFAULT_LLM_MODEL"] = "claude-opus-4.6"
    app.config["READ_DEFAULT_MODEL"] = "gpt-6-luna"
    app.config["GLEAN_MODEL_ANTHROPIC"] = "claude-sonnet-5.5"
    app.config["GLEAN_MODEL_OPENAI"] = "gpt-6-luna"
    from backend.routes.admin import admin_bp
    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    return app


class TestReadOnlyModels:
    def test_the_list_flags_them_read_and_not_chat(self, app_ro):
        client = app_ro.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)
        models = {m["id"]: m for m in
                  client.get("/api/nodes/models").get_json()["models"]}
        for ro in ("gpt-6.1-sol", "claude-sonnet-5.5"):
            assert (models[ro]["read"], models[ro]["chat"]) == (True, False)
        # Absent means chat.
        assert models["claude-opus-4.6"]["chat"] is True
        assert (models["gpt-6-luna"]["read"], models["gpt-6-luna"]["chat"]) == (True, True)
        assert "claude-opus-5" not in models

    def test_chat_and_read_predicates(self, app_ro):
        from backend.utils.llm_nodes import is_chat_model, is_read_model
        assert is_read_model("gpt-6.1-sol") and not is_chat_model("gpt-6.1-sol")
        assert is_read_model("claude-sonnet-5.5")
        assert not is_chat_model("claude-sonnet-5.5")
        assert is_chat_model("claude-opus-4.6") and is_chat_model("gpt-6-luna")
        assert not is_chat_model("claude-opus-5")      # deprecated
        assert not is_chat_model("nope")

    def test_a_read_runs_on_them(self, app_ro):
        client = app_ro.test_client()
        alice = _make_user("alice", is_admin=True)
        _db.session.commit()
        _login(client, alice.id)
        resp = client.post("/api/read/start", json={"model": "claude-sonnet-5.5"})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model == "claude-sonnet-5.5"

        t = _read_thread(alice, read_model="gpt-6.1-sol", chat_model="gpt-6-luna")
        resp = client.post(f"/api/read/from-node/{t['note2'].id}", json={})
        assert resp.status_code == 202, resp.get_json()
        # The thread's last read was on GPT-6.1 Sol, of the provider the
        # conversation runs on: the next one is too.
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model == "gpt-6.1-sol"

    def test_a_read_turn_keeps_the_read_only_model(self, app_ro):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice, read_model="gpt-6.1-sol")
        _render(t["read"])
        # Directly under a rendered read reply: a read again.
        node, _ = create_llm_placeholder(t["read"].id, "claude-sonnet-5.5", alice.id)
        assert node.llm_model == "claude-sonnet-5.5"

    def test_a_chat_reply_sent_with_one_is_refused(self, app_ro, monkeypatch):
        # Never moved to the chat default: that could be another provider
        # (Peter, 2026-10-02). 400, nothing created, nothing enqueued, no
        # provider called.
        from backend.llm_providers import LLMProvider
        provider = MagicMock()
        monkeypatch.setattr(LLMProvider, "get_completion", provider)
        delay = _mock_llm_task_module.generate_llm_response.delay
        delay.reset_mock()
        client = app_ro.test_client()
        alice = _make_user("alice", is_admin=True)
        root = _make_node(alice, content="a thought")
        _db.session.commit()
        _login(client, alice.id)
        before = Node.query.count()
        resp = client.post(f"/api/nodes/{root.id}/llm",
                           json={"model": "gpt-6.1-sol"})
        assert resp.status_code == 400, resp.get_json()
        body = resp.get_json()
        assert body["code"] == "model_read_only"
        assert body["model"] == "gpt-6.1-sol"
        assert body["error"] == ("GPT-6.1 Sol is only for Read. Choose "
                                 "another model for replies.")
        assert Node.query.count() == before
        assert Node.query.filter_by(node_type="llm").count() == 0
        delay.assert_not_called()
        provider.assert_not_called()

    def test_a_chat_turn_in_a_read_thread_is_refused(self, app_ro):
        # Below the chat reply of a finished read the turn is chat: the
        # read-only model is refused there too, before any write.
        from backend.utils.llm_nodes import (
            ReadOnlyModelRefused, create_llm_placeholder)
        delay = _mock_llm_task_module.generate_llm_response.delay
        delay.reset_mock()
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice, read_model="claude-sonnet-5.5",
                         chat_model="gpt-6-luna")
        _render(t["read"])  # a finished read: what follows a note is chat
        before = Node.query.count()
        with pytest.raises(ReadOnlyModelRefused) as exc:
            create_llm_placeholder(t["note2"].id, "claude-sonnet-5.5", alice.id)
        assert exc.value.model_id == "claude-sonnet-5.5"
        _db.session.rollback()
        assert Node.query.count() == before
        delay.assert_not_called()

    def test_a_read_again_through_the_reply_route_keeps_one(self, app_ro):
        # The generic reply route cannot tell a read from a chat turn and
        # does not need to: directly under a rendered read reply the turn
        # is a read again, which runs on the read-only model as asked.
        client = app_ro.test_client()
        alice = _make_user("alice", is_admin=True)
        t = _read_thread(alice, read_model="gpt-6.1-sol")
        _render(t["read"])
        _login(client, alice.id)
        resp = client.post(f"/api/nodes/{t['read'].id}/llm",
                           json={"model": "claude-sonnet-5.5"})
        assert resp.status_code == 202, resp.get_json()
        assert (Node.query.get(resp.get_json()["node_id"]).llm_model
                == "claude-sonnet-5.5")

    def test_a_deprecated_model_still_runs_on_the_chat_default(self, app_ro):
        # Unchanged here (#355): the same rule question applies to it and
        # gets its own issue.
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _make_user("alice", is_admin=True)
        root = _make_node(alice, content="a thought")
        _db.session.commit()
        node, _ = create_llm_placeholder(root.id, "claude-opus-5", alice.id)
        assert node.llm_model == "claude-opus-4.6"

    def test_a_saved_read_only_preference_falls_back(self, app_ro):
        # Set directly, as if saved before the flag existed: the Account
        # page never offers it and PUT /dashboard/user refuses it.
        from backend.utils.llm_nodes import (
            default_model_for, effective_preferred_model, resolve_chat_model)
        client = app_ro.test_client()
        alice = _make_user("alice", is_admin=True, preferred_model="gpt-6.1-sol")
        _db.session.commit()
        assert effective_preferred_model(alice) is None
        assert default_model_for(alice) == "claude-opus-4.6"
        assert resolve_chat_model(None, alice) == ("claude-opus-4.6", "default")
        _login(client, alice.id)
        assert client.get("/api/nodes/default-model").get_json() == {
            "suggested_model": "claude-opus-4.6", "source": "default"}

    def test_a_chat_reply_on_one_is_not_inherited(self, app_ro):
        from backend.utils.llm_nodes import resolve_chat_model
        alice = _make_user("alice", preferred_model="gpt-6-luna")
        root = _make_node(alice, content="a thought")
        reply = _make_node(alice, parent_id=root.id, node_type="llm",
                           llm_model="gpt-6.1-sol", content="hi")
        note = _make_node(alice, parent_id=reply.id, content="and?")
        _db.session.commit()
        assert resolve_chat_model(note, alice) == ("gpt-6-luna", "user_preference")

    def test_the_account_default_refuses_one(self, app_ro):
        client = app_ro.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)
        resp = client.put("/api/dashboard/user", json={"preferred_model": "gpt-6.1-sol"})
        assert resp.status_code == 400
        assert User.query.get(alice.id).preferred_model is None
        resp = client.put("/api/dashboard/user", json={"preferred_model": "claude-opus-4.6"})
        assert resp.status_code == 200, resp.get_json()
        assert User.query.get(alice.id).preferred_model == "claude-opus-4.6"

    def test_a_poll_refuses_one(self, app_ro):
        client = app_ro.test_client()
        boss = _make_user("boss", is_admin=True)
        _db.session.commit()
        _login(client, boss.id)
        resp = client.post("/api/admin/polls",
                           json={"question": "q?", "model_id": "claude-sonnet-5.5"})
        assert resp.status_code == 400
        assert "Read only" in resp.get_json()["error"]
        # A deprecated model is no chat model either (is_chat_model).
        resp = client.post("/api/admin/polls",
                           json={"question": "q?", "model_id": "claude-opus-5"})
        assert resp.status_code == 400
        assert "deprecated" in resp.get_json()["error"]
        resp = client.post("/api/admin/polls",
                           json={"question": "q?", "model_id": "claude-opus-4.6"})
        assert resp.status_code == 201, resp.get_json()

    def test_the_shipped_entries_are_read_only(self):
        from backend.config import Config
        for key, api_model, provider in (
                ("gpt-6.1-sol", "gpt-6.1-sol", "openai"),
                ("claude-sonnet-5.5", "claude-sonnet-5-5", "anthropic"),
                # Peter, 2026-10-08.
                ("claude-haiku-5.5", "claude-haiku-5-5", "anthropic")):
            cfg = Config.SUPPORTED_MODELS[key]
            assert (cfg["read"], cfg["chat"]) == (True, False), key
            assert (cfg["api_model"], cfg["provider"]) == (api_model, provider)
            assert not cfg.get("deprecated") and not cfg.get("featured")


# ── Glean (#435, Peter 2026-10-09) ───────────────────────────────────────

MODELS_GLEAN = {
    "claude-opus-4.6": {"provider": "anthropic", "display_name": "Opus 4.6"},
    "gpt-6-sol": {"provider": "openai", "display_name": "GPT-6 Sol"},
    "gpt-6-luna": {"provider": "openai", "display_name": "GPT-6 Luna",
                   "read": True},
    "claude-haiku-5.5": {"provider": "anthropic", "display_name": "Haiku 5.5",
                         "read": True, "chat": False},
}


@pytest.fixture
def app_glean(app):
    app.config["SUPPORTED_MODELS"] = MODELS_GLEAN
    app.config["DEFAULT_LLM_MODEL"] = "claude-opus-4.6"
    app.config["READ_DEFAULT_MODEL"] = "gpt-6-luna"
    app.config["GLEAN_MODEL_ANTHROPIC"] = "claude-haiku-5.5"
    app.config["GLEAN_MODEL_OPENAI"] = "gpt-6-luna"
    app.config["GLEAN_FOR_ALL"] = False
    app.config["GLEAN_USER_IDS"] = set()
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.textmode import textmode_bp
    from backend.routes.voice import voice_bp
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(voice_bp, url_prefix="/api/voice")
    return app


def _glean_user(app, username="ana", **kwargs):
    """A non-admin on the Glean list, with no explicit choice unless one
    is given."""
    kwargs.setdefault("glean_enabled", None)
    u = _make_user(username, **kwargs)
    app.config["GLEAN_USER_IDS"] = set(app.config["GLEAN_USER_IDS"]) | {u.id}
    _db.session.commit()
    return u


def _add_x_account(user):
    from backend.models import ExternalAccount
    _db.session.add(ExternalAccount(user_id=user.id, provider="twitter"))
    _db.session.commit()


def _add_saved_tweet(user, source="community_archive"):
    from backend.models import ExternalItem
    item = ExternalItem(user_id=user.id, source=source, external_id="1")
    item.set_content("a tweet")
    _db.session.add(item)
    _db.session.commit()


def _add_imported_tweet(user):
    n = _make_node(user, content="my old tweet")
    n.origin = "twitter"
    _db.session.commit()


class TestGleanSwitch:
    """Who gleans: the rollout gate, then the user's own switch. Without a
    choice it is on with Community Archive or X data, off without."""

    def test_off_without_x_or_archive_data(self, app_glean):
        from backend.utils.glean import glean_enabled
        ana = _glean_user(app_glean)
        assert glean_enabled(ana) is False

    @pytest.mark.parametrize("give", [
        lambda u: setattr(u, "twitter_id", "123"),          # X sign-up / Connect X
        lambda u: setattr(u, "prefilled_handle", "ana"),    # Community Archive prefill
        _add_x_account,                                     # X connected for bookmarks
        _add_imported_tweet,                                # an uploaded X archive
        _add_saved_tweet,                                   # saved archive tweets
        lambda u: _add_saved_tweet(u, "twitter_bookmark"),  # X bookmarks
    ])
    def test_on_by_default_with_x_or_archive_data(self, app_glean, give):
        from backend.utils.glean import glean_enabled
        ana = _glean_user(app_glean)
        give(ana)
        _db.session.commit()
        assert glean_enabled(ana) is True

    def test_connecting_x_later_turns_it_on(self, app_glean):
        from backend.utils.glean import glean_enabled
        ana = _glean_user(app_glean)
        assert glean_enabled(ana) is False
        _add_x_account(ana)
        assert glean_enabled(ana) is True

    def test_a_glean_pick_is_not_the_users_own_data(self, app_glean):
        from backend.utils.glean import glean_enabled
        ana = _glean_user(app_glean)
        _add_saved_tweet(ana, "read_pick")
        assert glean_enabled(ana) is False

    def test_the_explicit_choice_wins(self, app_glean):
        from backend.utils.glean import glean_enabled
        off = _glean_user(app_glean, "off", twitter_id="123", glean_enabled=False)
        on = _glean_user(app_glean, "on", glean_enabled=True)
        assert glean_enabled(off) is False
        assert glean_enabled(on) is True

    def test_outside_the_gate_never(self, app_glean):
        from backend.utils.glean import glean_enabled
        bob = _make_user("bob", glean_enabled=True, twitter_id="9")
        _db.session.commit()
        assert glean_enabled(bob) is False
        app_glean.config["GLEAN_FOR_ALL"] = True
        assert glean_enabled(bob) is True

    def test_the_payload_and_the_account_switch(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, twitter_id="123")
        _login(client, ana.id)
        user = client.get("/api/dashboard/").get_json()["user"]
        assert (user["glean_available"], user["glean_enabled"]) == (True, True)

        resp = client.put("/api/dashboard/user", json={"glean_enabled": False})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["user"]["glean_enabled"] is False
        assert User.query.get(ana.id).glean_enabled is False
        # With the switch off the glean route refuses too.
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        resp = client.post(f"/api/read/from-node/{entry.id}", json={})
        assert resp.status_code == 403
        assert "Account settings" in resp.get_json()["error"]

    def test_outside_the_gate_the_payload_hides_it(self, app_glean):
        client = app_glean.test_client()
        bob = _make_user("bob", glean_enabled=True)
        _db.session.commit()
        _login(client, bob.id)
        user = client.get("/api/dashboard/").get_json()["user"]
        assert (user["glean_available"], user["glean_enabled"]) == (False, False)


class TestGleanCardThreads:
    """A thread started from the Glean card carries started_from on its
    root; every node of it reports glean_thread."""

    def test_text_mode_from_the_glean_card(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        _login(client, ana.id)
        resp = client.post("/api/textmode/start", json={
            "content": "On my mind.", "entry": "glean", "ai_usage": "chat"})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert Node.query.get(data["conversation_id"]).started_from == "glean"
        for node_id in (data["user_node_id"], data["llm_node_id"]):
            assert client.get(f"/api/nodes/{node_id}").get_json()["glean_thread"] is True

    def test_text_mode_from_the_reflect_card(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        _login(client, ana.id)
        data = client.post("/api/textmode/start", json={
            "content": "On my mind.", "ai_usage": "chat"}).get_json()
        assert Node.query.get(data["conversation_id"]).started_from is None
        assert client.get(f"/api/nodes/{data['user_node_id']}").get_json()["glean_thread"] is False

    def test_no_mark_for_a_user_without_glean(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=False)
        _login(client, ana.id)
        data = client.post("/api/textmode/start", json={
            "content": "On my mind.", "entry": "glean", "ai_usage": "chat"}).get_json()
        assert Node.query.get(data["conversation_id"]).started_from is None

    def test_voice_from_the_glean_card(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        _login(client, ana.id)
        resp = client.post("/api/voice/", json={"content": "Spoken.", "entry": "glean"})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["parent_id"]).started_from == "glean"

    def test_the_finalize_task_marks_a_fresh_voice_thread(self, app_glean):
        from types import SimpleNamespace
        from backend.tasks.streaming_transcription import _create_system_node_early
        ana = _glean_user(app_glean, glean_enabled=True)
        root_id = _create_system_node_early(
            ana.id, "voice", SimpleNamespace(ai_usage="chat"), entry="glean")
        assert Node.query.get(root_id).started_from == "glean"
        plain = _create_system_node_early(
            ana.id, "voice", SimpleNamespace(ai_usage="chat"))
        assert Node.query.get(plain).started_from is None

    def test_voice_never_plays_a_gleaning(self, app_glean):
        """Continuing by voice from a gleaning opens the record button
        under it: the gleaning is never read aloud."""
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        root = _make_prompt_node(ana, "voice")
        root.prompt_key = "voice"
        prompt = _make_prompt_node(ana, "read_thread", parent_id=root.id)
        llm = _make_user("claude-haiku-5.5", twitter_id="llm-claude-haiku-5.5")
        gleaning = _make_node(llm, parent_id=prompt.id, node_type="llm",
                              llm_model="claude-haiku-5.5", human_owner=ana)
        gleaning.llm_task_status = "completed"
        _db.session.commit()
        _login(client, ana.id)
        resp = client.post(f"/api/voice/from-node/{gleaning.id}", json={})
        assert resp.status_code == 200, resp.get_json()
        # "ready": open the record button, play nothing. llm_node_id stays
        # for the iPhone app, which decodes it on every answer.
        assert resp.get_json() == {"mode": "ready", "parent_id": gleaning.id,
                                   "llm_node_id": gleaning.id}


class TestNoGleanUnderAFailedReply:
    """A glean asked for under a failed reply (the Glean button or the menu
    entry on a failed gleaning) starts from its parent, so the failed
    placeholder text never reaches the model (#435 review)."""

    def test_it_starts_from_the_failed_replys_parent(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        _login(client, ana.id)
        first = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        failed = Node.query.get(first["llm_node_id"])
        failed.llm_task_status = "failed"
        _db.session.commit()

        again = client.post(f"/api/read/from-node/{failed.id}", json={}).get_json()
        new = Node.query.get(again["llm_node_id"])
        assert new.parent_id == failed.parent_id == first["prompt_node_id"]

    def test_a_finished_gleaning_is_still_gleaned_under(self, app_glean):
        """Glean again under a finished gleaning keeps its picks in view."""
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        _login(client, ana.id)
        first = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        done = Node.query.get(first["llm_node_id"])
        done.llm_task_status = "completed"
        _db.session.commit()
        again = client.post(f"/api/read/from-node/{done.id}", json={}).get_json()
        assert Node.query.get(again["llm_node_id"]).parent_id == done.id


class TestFailedGleaningSaysWhy:
    """The thread page shows why a gleaning failed instead of its
    placeholder text (#435): the reason is on the node, for its owner."""

    def test_owner_gets_the_reason_others_do_not(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        bob = _make_user("bob")
        llm = _make_user("claude-haiku-5.5", glean_enabled=None)
        entry = _make_node(ana, content="a reflection")
        entry.privacy_level = "public"
        failed = _make_node(llm, parent_id=entry.id, node_type="llm",
                            llm_model="claude-haiku-5.5", human_owner=ana,
                            content="[LLM response generation pending...]")
        failed.privacy_level = "public"
        failed.llm_task_status = "failed"
        failed.llm_task_error = "The model provider did not answer."
        _db.session.commit()

        _login(client, ana.id)
        data = client.get(f"/api/nodes/{failed.id}").get_json()
        assert data["llm_task_error"] == "The model provider did not answer."

        # Flask-Login caches the user on g, which the fixture's app
        # context keeps between clients.
        from flask import g
        g.pop("_login_user", None)
        client = app_glean.test_client()
        _login(client, bob.id)
        data = client.get(f"/api/nodes/{failed.id}").get_json()
        assert data["id"] == failed.id
        assert "llm_task_error" not in data


class TestGleanIsLiveOnTheUsersProvider:
    def _entry(self, app, **user_kwargs):
        client = app.test_client()
        ana = _glean_user(app, glean_enabled=True, **user_kwargs)
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        _login(client, ana.id)
        return client, ana, entry

    def test_a_glean_is_a_live_call(self, app_glean):
        import json as _json
        client, _, entry = self._entry(app_glean)
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        meta = _json.loads(Node.query.get(data["llm_node_id"]).tool_calls_meta)
        assert {"name": "_live"} in meta

    def test_the_admins_read_start_stays_a_batch(self, app_glean):
        import json as _json
        client = app_glean.test_client()
        boss = _make_user("boss", is_admin=True, preferred_model="gpt-6-sol")
        _db.session.commit()
        _login(client, boss.id)
        data = client.post("/api/read/start", json={}).get_json()
        meta = _json.loads(Node.query.get(data["llm_node_id"]).tool_calls_meta)
        assert {"name": "_read_batch"} in meta
        assert {"name": "_live"} not in meta

    def test_a_glean_is_never_marked_for_the_batch(self, app_glean):
        import json as _json
        client, _, entry = self._entry(app_glean)
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        meta = _json.loads(Node.query.get(data["llm_node_id"]).tool_calls_meta)
        assert {"name": "_read_batch"} not in meta
        # Glean again too.
        data = client.post(f"/api/read/from-node/{data['llm_node_id']}",
                           json={}).get_json()
        meta = _json.loads(Node.query.get(data["llm_node_id"]).tool_calls_meta)
        assert {"name": "_read_batch"} not in meta
        assert {"name": "_live"} in meta

    def test_an_anthropic_user_gleans_on_an_anthropic_model(self, app_glean):
        client, _, entry = self._entry(app_glean, preferred_model="claude-opus-4.6")
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        assert Node.query.get(data["llm_node_id"]).llm_model == "claude-haiku-5.5"

    def test_an_openai_user_gleans_on_an_openai_model(self, app_glean):
        client, _, entry = self._entry(app_glean, preferred_model="gpt-6-sol")
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        assert Node.query.get(data["llm_node_id"]).llm_model == "gpt-6-luna"

    def test_the_glean_model_is_a_setting(self, app_glean):
        app_glean.config["SUPPORTED_MODELS"] = {
            **MODELS_GLEAN,
            "claude-sonnet-5.5": {"provider": "anthropic", "display_name": "Sonnet 5.5",
                                  "read": True, "chat": False},
        }
        app_glean.config["GLEAN_MODEL_ANTHROPIC"] = "claude-sonnet-5.5"
        client, _, entry = self._entry(app_glean, preferred_model="claude-opus-4.6")
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        assert Node.query.get(data["llm_node_id"]).llm_model == "claude-sonnet-5.5"

    @pytest.mark.parametrize("named", ["gpt-6-luna", "claude-sonnet-5.5"])
    def test_a_user_who_gleans_names_the_model(self, app_glean, named):
        """Peter, 2026-10-09: everyone on Glean picks from the same read
        models as admins. An Anthropic user's pick runs as picked, another
        provider's model or a pricier one of their own."""
        app_glean.config["SUPPORTED_MODELS"] = {
            **MODELS_GLEAN,
            "claude-sonnet-5.5": {"provider": "anthropic", "display_name": "Sonnet 5.5",
                                  "read": True, "chat": False},
        }
        client, _, entry = self._entry(app_glean, preferred_model="claude-opus-4.6")
        resp = client.post(f"/api/read/from-node/{entry.id}", json={"model": named})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model == named

    def test_a_named_model_must_be_a_read_model(self, app_glean):
        client, _, entry = self._entry(app_glean, preferred_model="claude-opus-4.6")
        before = Node.query.count()
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "claude-opus-4.6"})
        assert resp.status_code == 400
        assert "Haiku 5.5" in resp.get_json()["error"]
        assert Node.query.count() == before

    def test_glean_again_on_the_named_model(self, app_glean):
        """A second glean in the thread runs on the model named for it,
        another provider's too."""
        client, _, entry = self._entry(app_glean, preferred_model="claude-opus-4.6")
        first = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        reply = Node.query.get(first["llm_node_id"])
        assert reply.llm_model == "claude-haiku-5.5"
        reply.llm_task_status = "completed"
        _db.session.commit()
        resp = client.post(f"/api/read/from-node/{reply.id}",
                           json={"model": "gpt-6-luna"})
        assert resp.status_code == 202, resp.get_json()
        again = Node.query.get(resp.get_json()["llm_node_id"])
        assert (again.parent_id, again.llm_model) == (reply.id, "gpt-6-luna")

    @pytest.mark.parametrize("who", ["outside the gate", "switch off"])
    def test_without_glean_a_named_model_is_refused(self, app_glean, who):
        """A user who may not glean can't name a model either: the glean is
        refused before anything is created, and the model choice is not
        theirs anywhere else (may_choose_glean_model)."""
        from backend.utils.glean import may_choose_glean_model
        client = app_glean.test_client()
        if who == "outside the gate":
            ana = _make_user("ana", glean_enabled=True)  # not on GLEAN_USER_IDS
        else:
            ana = _glean_user(app_glean, glean_enabled=False)
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        _login(client, ana.id)
        before = Node.query.count()
        resp = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-6-luna"})
        assert resp.status_code == 403
        assert Node.query.count() == before
        assert may_choose_glean_model(ana) is False

    def test_who_may_choose_the_model(self, app_glean):
        from backend.utils.glean import may_choose_glean_model
        boss = _make_user("boss", is_admin=True, glean_enabled=False)
        ana = _glean_user(app_glean, glean_enabled=True)
        assert may_choose_glean_model(boss) is True
        assert may_choose_glean_model(ana) is True
        assert may_choose_glean_model(None) is False

    @pytest.mark.parametrize("account_model,expected", [
        ("claude-opus-4.6", "claude-haiku-5.5"),
        ("gpt-6-sol", "gpt-6-luna"),
    ])
    def test_the_pickers_default_follows_the_account_models_provider(
            self, app_glean, account_model, expected):
        """The Glean picker's default (and a glean with no model named) is
        the glean model of the provider the user's own model is from:
        Loore's default never crosses providers."""
        client, _, entry = self._entry(app_glean, preferred_model=account_model)
        resp = client.get(f"/api/nodes/{entry.id}/suggested-model?purpose=read")
        assert resp.get_json() == {"suggested_model": expected,
                                   "source": "provider_default"}
        resp = client.get("/api/nodes/default-model?purpose=read")
        assert resp.get_json()["suggested_model"] == expected
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        assert Node.query.get(data["llm_node_id"]).llm_model == expected

    def test_another_users_node_is_refused_with_a_named_model(self, app_glean):
        client, _, _ = self._entry(app_glean)
        bob = _glean_user(app_glean, "bob", glean_enabled=True)
        bobs = _make_node(bob, content="bob's reflection")
        _db.session.commit()
        before = Node.query.count()
        resp = client.post(f"/api/read/from-node/{bobs.id}",
                           json={"model": "gpt-6-luna"})
        assert resp.status_code == 403
        assert Node.query.count() == before

    def test_an_admin_may_for_an_evaluation(self, app_glean):
        client = app_glean.test_client()
        boss = _make_user("boss", is_admin=True, preferred_model="claude-opus-4.6")
        entry = _make_node(boss, content="a reflection")
        _db.session.commit()
        _login(client, boss.id)
        data = client.post(f"/api/read/from-node/{entry.id}",
                           json={"model": "gpt-6-luna"}).get_json()
        assert Node.query.get(data["llm_node_id"]).llm_model == "gpt-6-luna"

    def test_a_reply_under_the_glean_prompt(self, app_glean):
        """A read reached another way (a reply asked for under the prompt):
        a chat model sent with it gives way to the default of the user's
        provider; a read model the user names is used, another
        provider's too."""
        from backend.utils.llm_nodes import create_llm_placeholder
        ana = _glean_user(app_glean, glean_enabled=True, preferred_model="claude-opus-4.6")
        prompt = _make_prompt_node(ana, "read_thread")
        other = _make_prompt_node(ana, "read_thread")
        _db.session.commit()
        node, _ = create_llm_placeholder(prompt.id, "claude-opus-4.6", ana.id)
        assert node.llm_model == "claude-haiku-5.5"
        node, _ = create_llm_placeholder(other.id, "gpt-6-luna", ana.id)
        assert node.llm_model == "gpt-6-luna"

    def test_rerun_is_admin_only(self, app_glean):
        client, ana, entry = self._entry(app_glean)
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        reply = Node.query.get(data["llm_node_id"])
        reply.llm_task_status = "failed"
        _db.session.commit()
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})
        assert resp.status_code == 403

    def test_a_batch_rerun_drops_the_live_marker(self, app_glean):
        import json as _json
        client = app_glean.test_client()
        boss = _make_user("boss", is_admin=True, glean_enabled=True)
        entry = _make_node(boss, content="a reflection")
        _db.session.commit()
        _login(client, boss.id)
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        reply = Node.query.get(data["llm_node_id"])
        reply.llm_task_status = "failed"
        _db.session.commit()
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": False})
        assert resp.status_code == 202, resp.get_json()
        meta = _json.loads(Node.query.get(reply.id).tool_calls_meta or "[]")
        assert {"name": "_live"} not in meta
        assert {"name": "_read_batch"} in meta
        # A live rerun of it removes the batch mark again.
        node = Node.query.get(reply.id)
        node.llm_task_status = "failed"
        _db.session.commit()
        resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})
        assert resp.status_code == 202, resp.get_json()
        meta = _json.loads(Node.query.get(reply.id).tool_calls_meta or "[]")
        assert {"name": "_read_batch"} not in meta

    def test_no_rerun_while_a_live_glean_runs(self, app_glean):
        """A second run would render the day into the same reply while the
        first one runs (#435: Peter's double render)."""
        client = app_glean.test_client()
        boss = _make_user("boss", is_admin=True, glean_enabled=True)
        entry = _make_node(boss, content="a reflection")
        _db.session.commit()
        _login(client, boss.id)
        data = client.post(f"/api/read/from-node/{entry.id}", json={}).get_json()
        reply = Node.query.get(data["llm_node_id"])
        for status in ("pending", "processing"):
            reply.llm_task_status = status
            _db.session.commit()
            resp = client.post(f"/api/read/{reply.id}/rerun", json={"live": True})
            assert resp.status_code == 409, resp.get_json()
            assert resp.get_json()["error"] == "This glean is still running."


class TestGleanOnlyThroughGlean:
    """A non-admin's read runs only as a glean: from the read prompt Glean
    attaches, while they glean. The raw {ca_tweets} placeholder, the read
    prompts' text and /read/start stay admin experiments (#435 review)."""

    def test_a_typed_placeholder_is_admin_only(self, app_glean):
        from backend.utils.llm_nodes import create_llm_placeholder
        from backend.utils.placeholders import CaTweetsValidationError
        ana = _glean_user(app_glean, glean_enabled=True)
        entry = _make_node(ana, content="{ca_tweets?days=3} what now?")
        _db.session.commit()
        with pytest.raises(CaTweetsValidationError):
            create_llm_placeholder(entry.id, "claude-opus-4.6", ana.id)
        boss = _make_user("boss", is_admin=True)
        own = _make_node(boss, content="{ca_tweets?days=1}")
        _db.session.commit()
        node, _ = create_llm_placeholder(own.id, "gpt-6-luna", boss.id)
        assert node.llm_model == "gpt-6-luna"

    def test_the_read_prompt_needs_the_switch(self, app_glean):
        from backend.utils.llm_nodes import create_llm_placeholder
        from backend.utils.placeholders import CaTweetsValidationError
        ana = _glean_user(app_glean, glean_enabled=False)
        prompt = _make_prompt_node(ana, "read_thread")
        _db.session.commit()
        with pytest.raises(CaTweetsValidationError):
            create_llm_placeholder(prompt.id, "claude-opus-4.6", ana.id)

    def test_the_task_gate_checks_where_the_placeholder_is(self, app_glean):
        from backend.utils.glean import read_turn_allowed
        ana = _glean_user(app_glean, glean_enabled=True)
        read_prompt = _make_prompt_node(ana, "read_thread")
        voice_prompt = _make_prompt_node(ana, "voice")
        plain = _make_node(ana, content="{ca_tweets}")
        boss = _make_user("boss", is_admin=True)
        _db.session.commit()
        assert read_turn_allowed(ana, read_prompt) is True
        assert read_turn_allowed(ana, voice_prompt) is False
        assert read_turn_allowed(ana, plain) is False
        assert read_turn_allowed(boss, plain) is True
        ana.glean_enabled = False
        _db.session.commit()
        assert read_turn_allowed(ana, read_prompt) is False
        assert read_turn_allowed(None, read_prompt) is False

    def test_users_cannot_change_the_read_prompts(self, app_glean):
        from backend.routes.prompts import prompts_bp
        app_glean.register_blueprint(prompts_bp, url_prefix="/api/prompts")
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        _login(client, ana.id)
        resp = client.put("/api/prompts/read_thread", json={"content": "x {ca_tweets}"})
        assert resp.status_code == 403
        resp = client.post("/api/prompts/read_thread/revert/1")
        assert resp.status_code == 403
        # Nor carry the placeholder in a prompt of their own.
        resp = client.put("/api/prompts/voice", json={"content": "and {ca_tweets?days=3}"})
        assert resp.status_code == 400
        resp = client.put("/api/prompts/voice", json={"content": "a prompt of mine"})
        assert resp.status_code == 200

    def test_read_start_is_admin_only(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        _login(client, ana.id)
        assert client.post("/api/read/start", json={}).status_code == 403

    def _detach(self, node, text):
        from backend.models import NodeContextArtifact
        NodeContextArtifact.query.filter_by(
            node_id=node.id, artifact_type="prompt").delete()
        node.prompt_key = "read_thread"
        node.set_content(text)
        _db.session.commit()

    def test_a_read_prompt_with_the_users_own_text_is_not_glean(self, app_glean):
        """A read prompt whose link to Loore's version is gone (a
        per-thread edit) or points to a version the user wrote keeps its
        key but is the user's text: no read for a non-admin."""
        from backend.models import UserPrompt
        from backend.utils.glean import is_glean_prompt_node, read_turn_allowed
        from backend.utils.llm_nodes import create_llm_placeholder
        from backend.utils.placeholders import CaTweetsValidationError
        ana = _glean_user(app_glean, glean_enabled=True)
        linked = _make_prompt_node(ana, "read_thread")
        detached = _make_prompt_node(ana, "read_thread")
        _db.session.commit()
        assert is_glean_prompt_node(linked, ana) is True
        self._detach(detached, "Read it all. {ca_tweets?days=3}")
        assert is_glean_prompt_node(detached, ana) is False
        assert read_turn_allowed(ana, detached) is False
        with pytest.raises(CaTweetsValidationError):
            create_llm_placeholder(detached.id, "claude-opus-4.6", ana.id)
        # Linked to a version the user wrote.
        record = linked.get_artifact("prompt")
        own = UserPrompt(user_id=ana.id, prompt_key="read_thread",
                         title="Glean", generated_by="user")
        own.set_content("Mine. {ca_tweets?days=3}")
        _db.session.add(own)
        _db.session.flush()
        from backend.models import NodeContextArtifact
        NodeContextArtifact.query.filter_by(
            node_id=linked.id, artifact_type="prompt").update(
                {"artifact_id": own.id})
        _db.session.commit()
        _db.session.expire_all()
        assert record.generated_by == "default"
        assert is_glean_prompt_node(Node.query.get(linked.id), ana) is False

    def test_users_cannot_rewrite_a_read_prompt_in_a_thread(self, app_glean):
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        prompt = _make_prompt_node(ana, "read_thread")
        _db.session.commit()
        _login(client, ana.id)
        resp = client.put(f"/api/nodes/{prompt.id}", json={
            "content": "Read it all. {ca_tweets?days=3}", "detach_prompt": True})
        assert resp.status_code == 403
        assert Node.query.get(prompt.id).get_artifact("prompt") is not None
        # A settings-only edit (the same text) stays open.
        same = Node.query.get(prompt.id).get_content()
        resp = client.put(f"/api/nodes/{prompt.id}", json={
            "content": same, "privacy_level": "private"})
        assert resp.status_code == 200, resp.get_json()

    def test_prompt_writes_on_read_prompts_are_admin_only(self, app_glean):
        from backend.models import UserPrompt
        from backend.routes.prompts import prompts_bp
        app_glean.register_blueprint(prompts_bp, url_prefix="/api/prompts")
        client = app_glean.test_client()
        ana = _glean_user(app_glean, glean_enabled=True)
        _login(client, ana.id)
        assert client.post("/api/prompts/read_thread/revert-to-default").status_code == 403
        assert client.post("/api/prompts/read_thread/acknowledge-default").status_code == 403
        # Restoring an older version runs the same {ca_tweets} check as a save.
        old = UserPrompt(user_id=ana.id, prompt_key="voice", title="Voice Mode",
                         generated_by="user")
        old.set_content("old text {ca_tweets?days=3}")
        _db.session.add(old)
        _db.session.commit()
        resp = client.post(f"/api/prompts/voice/revert/{old.id}")
        assert resp.status_code == 400
        assert UserPrompt.query.filter_by(user_id=ana.id, prompt_key="voice").count() == 1


class TestGleanInSomeoneElsesThread:
    """A glean is the user's own: their provider, their read prompt,
    private (#435 review)."""

    def _public_thread(self, alice, chat_model="claude-opus-4.6"):
        root = _make_node(alice, content="a public post")
        root.privacy_level = "public"
        llm = _make_user(chat_model, twitter_id=f"llm-{chat_model}")
        reply = _make_node(llm, parent_id=root.id, node_type="llm",
                           llm_model=chat_model, human_owner=alice)
        reply.privacy_level = "public"
        _db.session.commit()
        return root, reply

    def _bob_under(self, app, parent):
        bob = _glean_user(app, "bob", glean_enabled=True, preferred_model="gpt-6-sol")
        note = _make_node(bob, parent_id=parent.id, content="my reflection on it")
        note.privacy_level = "public"
        _db.session.commit()
        return bob, note

    def test_the_provider_is_the_users_own_not_the_threads(self, app_glean):
        """Bob's replies run on OpenAI; Alice's AI reply above his entry ran
        on Anthropic. His glean stays on OpenAI."""
        alice = _make_user("alice")
        _, reply = self._public_thread(alice)
        bob, note = self._bob_under(app_glean, reply)
        client = app_glean.test_client()
        _login(client, bob.id)
        resp = client.post(f"/api/read/from-node/{note.id}", json={})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model == "gpt-6-luna"

    def test_the_users_own_ai_reply_in_the_thread_counts(self, app_glean):
        from backend.utils.llm_nodes import glean_provider
        alice = _make_user("alice")
        _, reply = self._public_thread(alice)
        bob, note = self._bob_under(app_glean, reply)  # account: GPT-6 Sol
        # Without an AI reply of his own: his account model, not Alice's
        # Opus reply above his entry.
        assert glean_provider(note, bob) == "openai"
        # His own AI reply in the thread decides over the account model.
        llm = User.query.filter_by(username="claude-opus-4.6").first()
        own = _make_node(llm, parent_id=note.id, node_type="llm",
                         llm_model="claude-opus-4.6", human_owner=bob)
        _db.session.commit()
        assert glean_provider(own, bob) == "anthropic"

    def test_a_glean_on_a_public_entry_is_private(self, app_glean):
        alice = _make_user("alice")
        _, reply = self._public_thread(alice)
        bob, note = self._bob_under(app_glean, reply)
        client = app_glean.test_client()
        _login(client, bob.id)
        data = client.post(f"/api/read/from-node/{note.id}", json={}).get_json()
        assert Node.query.get(data["prompt_node_id"]).privacy_level == "private"
        assert Node.query.get(data["llm_node_id"]).privacy_level == "private"

    def test_someone_elses_read_prompt_is_never_reused(self, app_glean):
        """Alice's read prompt above Bob's entry (an older public one) is
        not Bob's: his glean attaches a read prompt of his own."""
        from backend.utils.glean import is_glean_prompt_node, read_turn_allowed
        alice = _glean_user(app_glean, "alice", glean_enabled=True)
        root, reply = self._public_thread(alice)
        alices = _make_prompt_node(alice, "read_thread", parent_id=reply.id)
        alices.privacy_level = "public"
        _db.session.commit()
        bob, note = self._bob_under(app_glean, alices)
        assert is_glean_prompt_node(alices, alice) is True
        assert is_glean_prompt_node(alices, bob) is False
        assert read_turn_allowed(bob, alices) is False
        client = app_glean.test_client()
        _login(client, bob.id)
        data = client.post(f"/api/read/from-node/{note.id}", json={}).get_json()
        prompt = Node.query.get(data["prompt_node_id"])
        assert (prompt.parent_id, prompt.user_id) == (note.id, bob.id)
        assert Node.query.get(data["llm_node_id"]).parent_id == prompt.id


class TestGleanModelFailsClosed:
    """The read model is the server's choice and fails rather than falls
    back (#435)."""

    def test_an_unresolvable_provider_fails(self, app_glean, monkeypatch):
        from backend.utils import llm_nodes
        from backend.utils.glean import GleanModelUnavailable
        ana = _glean_user(app_glean, glean_enabled=True)
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        monkeypatch.setattr(llm_nodes, "glean_provider", lambda *a, **k: None)
        with pytest.raises(GleanModelUnavailable):
            llm_nodes.resolve_read_model(entry, user=ana)

    def test_a_missing_or_wrong_glean_model_setting_fails(self, app_glean):
        from backend.utils.glean import GleanModelUnavailable
        from backend.utils.llm_nodes import resolve_read_model
        ana = _glean_user(app_glean, glean_enabled=True, preferred_model="gpt-6-sol")
        entry = _make_node(ana, content="a reflection")
        _db.session.commit()
        # No OpenAI glean model configured: no READ_DEFAULT_MODEL in its place.
        app_glean.config["GLEAN_MODEL_OPENAI"] = None
        with pytest.raises(GleanModelUnavailable):
            resolve_read_model(entry, user=ana)
        # Configured to another provider's model: refused, not used.
        app_glean.config["GLEAN_MODEL_OPENAI"] = "claude-haiku-5.5"
        with pytest.raises(GleanModelUnavailable):
            resolve_read_model(entry, user=ana)

    def test_the_default_keeps_the_users_own_earlier_read_of_that_provider(self, app_glean):
        """As the admin picker did before (#435): an earlier read of the
        user's own in the thread, on the default's provider, is the
        default ("glean again" keeps a model picked there). Without the
        choice (Glean switched off) it is always the provider's glean
        model."""
        from backend.utils.llm_nodes import resolve_read_model
        app_glean.config["SUPPORTED_MODELS"] = {
            **MODELS_GLEAN,
            "gpt-6.1-sol": {"provider": "openai", "display_name": "GPT-6.1 Sol",
                            "read": True, "chat": False},
        }
        ana = _glean_user(app_glean, glean_enabled=True, preferred_model="gpt-6-sol")
        t = _read_thread(ana, read_model="gpt-6.1-sol", chat_model="gpt-6-sol")
        assert resolve_read_model(t["note2"], user=ana) == ("gpt-6.1-sol", "predecessor")
        boss = _make_user("boss", is_admin=True, preferred_model="gpt-6-sol")
        b = _read_thread(boss, read_model="gpt-6.1-sol", chat_model="gpt-6-sol")
        assert resolve_read_model(b["note2"], user=boss) == ("gpt-6.1-sol", "predecessor")
        ana.glean_enabled = False
        _db.session.commit()
        assert resolve_read_model(t["note2"], user=ana) == ("gpt-6-luna", "provider_default")

    def test_a_pick_on_another_provider_is_never_the_default(self, app_glean):
        """Ana's conversation is on Anthropic; she gleaned once on GPT-6
        Luna. The next default is Haiku 5.5 again."""
        from backend.utils.llm_nodes import resolve_read_model
        ana = _glean_user(app_glean, glean_enabled=True, preferred_model="claude-opus-4.6")
        t = _read_thread(ana, read_model="gpt-6-luna", chat_model="claude-opus-4.6")
        assert resolve_read_model(t["note2"], user=ana) == (
            "claude-haiku-5.5", "provider_default")

    def test_someone_elses_earlier_read_is_not_the_default(self, app_glean):
        """A read made for Alice higher up the thread is not Bob's choice."""
        from backend.utils.llm_nodes import resolve_read_model
        app_glean.config["SUPPORTED_MODELS"] = {
            **MODELS_GLEAN,
            "gpt-6.1-sol": {"provider": "openai", "display_name": "GPT-6.1 Sol",
                            "read": True, "chat": False},
        }
        alice = _glean_user(app_glean, "alice", glean_enabled=True, preferred_model="gpt-6-sol")
        t = _read_thread(alice, read_model="gpt-6.1-sol", chat_model="gpt-6-sol")
        bob = _glean_user(app_glean, "bob", glean_enabled=True, preferred_model="gpt-6-sol")
        note = _make_node(bob, parent_id=t["note2"].id, content="mine")
        _db.session.commit()
        assert resolve_read_model(note, user=bob) == ("gpt-6-luna", "provider_default")

    @pytest.mark.parametrize("chooser", ["admin", "glean user"])
    def test_nobody_names_the_model_under_someone_elses_node(self, app_glean, chooser):
        """The choice holds only under a node of the user's own, for admins
        and everyone who gleans."""
        from backend.utils.llm_nodes import create_llm_placeholder
        ana = _glean_user(app_glean, glean_enabled=True)
        prompt = _make_prompt_node(ana, "read_thread")
        if chooser == "admin":
            boss = _make_user("boss", is_admin=True, preferred_model="claude-opus-4.6")
        else:
            boss = _glean_user(app_glean, "bob", glean_enabled=True,
                               preferred_model="claude-opus-4.6")
        _db.session.commit()
        from backend.utils.llm_nodes import _Chain, _read_turn_model
        assert _read_turn_model(prompt, boss, "gpt-6-luna", _Chain(prompt)) == "claude-haiku-5.5"
        own = _make_prompt_node(boss, "read_thread")
        _db.session.commit()
        node, _ = create_llm_placeholder(own.id, "gpt-6-luna", boss.id)
        assert node.llm_model == "gpt-6-luna"


class TestPickDisplayNames:
    def _picks(self, ana, node, refs):
        from backend.utils.ca_feed import save_feed_picks
        picks = [{"n": n, "rank": i + 1, "qt": "why", "relevance": 50,
                  "recommend": True, "ref": ref}
                 for i, (n, ref) in enumerate(sorted(refs.items()))]
        rows = save_feed_picks(ana.id, node, picks, picked_by="gpt-6-luna")
        _db.session.commit()
        return rows

    def test_saving_a_pick_keeps_the_display_name(self, app_glean):
        ana = _glean_user(app_glean)
        node = _make_node(ana, content="reply", node_type="llm", llm_model="gpt-6-luna")
        rows = self._picks(ana, node, {
            1: {"username": "alice", "tweet_id": "11", "text": "t",
                "display_name": "Alice Aalto"},
            2: {"username": "bob", "tweet_id": "22", "text": "t",
                "display_name": None},
        })
        assert [r.item.author_name for r in rows] == ["Alice Aalto", None]

    def test_the_users_own_row_gets_the_name_it_lacked(self, app_glean):
        from backend.models import ExternalItem
        ana = _glean_user(app_glean)
        own = ExternalItem(user_id=ana.id, source="twitter_bookmark",
                           external_id="11", author_handle="alice")
        own.set_content("t")
        _db.session.add(own)
        node = _make_node(ana, content="reply", node_type="llm", llm_model="gpt-6-luna")
        self._picks(ana, node, {1: {"username": "alice", "tweet_id": "11",
                                    "text": "t", "display_name": "Alice Aalto"}})
        assert ExternalItem.query.get(own.id).author_name == "Alice Aalto"

    def test_the_one_time_fill_for_past_picks(self, app_glean, tmp_path, monkeypatch):
        from backend.models import ExternalItem
        from backend.utils import community_archive as ca
        from backend.utils.ca_feed import fill_pick_author_names
        ana = _glean_user(app_glean)
        node = _make_node(ana, content="reply", node_type="llm", llm_model="gpt-6-luna")
        rows = self._picks(ana, node, {
            1: {"username": "alice", "tweet_id": "11", "text": "t"},
            2: {"username": "Nobody", "tweet_id": "22", "text": "t"},
        })
        # A saved tweet nobody picked is not touched.
        other = ExternalItem(user_id=ana.id, source="twitter_bookmark",
                             external_id="33", author_handle="alice")
        other.set_content("t")
        _db.session.add(other)
        _db.session.commit()
        asked = []

        def _names(snapshot_dir, handles):
            asked.append(sorted(handles))
            return {"alice": "Alice Aalto"}
        monkeypatch.setattr(ca, "fetch_display_names", _names)

        assert fill_pick_author_names(tmp_path) == (2, 1)   # dry run
        assert ExternalItem.query.get(rows[0].item.id).author_name is None
        assert fill_pick_author_names(tmp_path, apply=True) == (2, 1)
        _db.session.commit()
        assert ExternalItem.query.get(rows[0].item.id).author_name == "Alice Aalto"
        assert ExternalItem.query.get(rows[1].item.id).author_name is None
        assert ExternalItem.query.get(other.id).author_name is None
        assert asked[0] == ["Nobody", "alice"]
        # Idempotent: only the one the archive has no name for is left.
        assert fill_pick_author_names(tmp_path, apply=True) == (1, 0)

    def test_the_card_and_the_reference_page_get_it(self, app_glean):
        from backend.models import ExternalItem
        from backend.utils.quotes import get_ext_quote_data
        from backend.routes.external import _serialize_item
        ana = _glean_user(app_glean)
        item = ExternalItem(user_id=ana.id, source="read_pick", external_id="11",
                            author_handle="alice", author_name="Alice Aalto")
        item.set_content("t")
        _db.session.add(item)
        _db.session.commit()
        assert get_ext_quote_data([item.id], ana.id)[item.id]["author_name"] == "Alice Aalto"
        assert _serialize_item(item)["author_name"] == "Alice Aalto"
