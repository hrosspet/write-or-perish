"""The approval dialog's API (Peter, 2026-11-12): the waiting write calls
of a reply are listed with the thread, and each is approved or rejected on
its own. Only the call's owner sees or decides it.

Builds the waiting calls the way the task does (ApprovalHold) and runs
the routes on a test app; the task module is the harness's real one.
"""
import json
import sys

import pytest
from flask import Flask, g

from backend.tests.test_retrieval_loop import _llm_task_mod
from backend.extensions import db as _db
from backend.models import Draft, Node, User, UserArtifact


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

    from backend.routes.tool_approvals import tool_approvals_bp
    from backend.routes.nodes import nodes_bp
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(tool_approvals_bp,
                           url_prefix="/api/tool-approvals")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    return app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _client(app, user):
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


@pytest.fixture
def thread(app):
    """Ben's thread: his entry, Anna's reply, his entry, his AI reply that
    asked to change his memory; the change waits."""
    from backend.utils.tool_approvals import ApprovalHold
    ben = User(username="ben", default_ai_usage="chat")
    anna = User(username="anna", default_ai_usage="chat")
    bot = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add_all([ben, anna, bot])
    _db.session.flush()
    memory = UserArtifact(user_id=ben.id, kind="memory", title="Memory",
                          generated_by="test", ai_usage="chat")
    memory.set_content("- likes tea")
    _db.session.add(memory)
    nodes = []
    parent = None
    for author, text in ((ben, "ben's note"), (anna, "anna replies"),
                         (ben, "ben answers")):
        node = Node(user_id=author.id, human_owner_id=author.id,
                    parent_id=parent.id if parent else None,
                    node_type="user", privacy_level="public", ai_usage="chat")
        node.set_content(text)
        _db.session.add(node)
        _db.session.flush()
        nodes.append(node)
        parent = node
    reply = Node(user_id=bot.id, human_owner_id=ben.id, parent_id=parent.id,
                 node_type="llm", llm_model="gpt-5",
                 llm_task_status="completed", privacy_level="private",
                 ai_usage="chat")
    reply.set_content("I proposed a change to your memory.")
    _db.session.add(reply)
    _db.session.flush()
    inp = {"kind": "memory", "updated_content": "- likes tea\n- trusts anna",
           "base_version": 1}
    fields = ApprovalHold(["other_user"], reply.id).hold(
        "update_artifact", inp, reply, ben.id,
        previous_artifact_id=memory.id, kind="memory")
    reply.tool_calls_meta = json.dumps([{
        "name": "update_artifact", "input": {"kind": "memory"}, **fields}])
    _db.session.commit()
    return ben, anna, nodes, reply, fields["pending_id"]


def _memory(ben):
    _db.session.expire_all()
    return UserArtifact.latest_for(ben.id, "memory").get_content()


def test_the_thread_lists_the_waiting_call_with_its_diff(app, thread):
    ben, anna, nodes, reply, pending_id = thread

    data = _client(app, ben).get(
        f"/api/tool-approvals?node={nodes[0].id}").get_json()

    [item] = data["pending"]
    assert item["id"] == pending_id
    assert item["turn_node_id"] == reply.id
    assert item["title"] == "Change your Memory"
    assert item["kinds"] == ["other_user"]
    assert {"type": "add", "text": "- trusts anna"} in item["lines"]


def test_the_node_page_carries_the_waiting_calls(app, thread):
    ben, anna, nodes, reply, pending_id = thread

    data = _client(app, ben).get(f"/api/nodes/{reply.id}").get_json()

    assert [p["id"] for p in data["pending_approvals"]] == [pending_id]


def test_someone_else_neither_sees_nor_decides_it(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, anna)

    assert client.get(
        f"/api/tool-approvals?node={nodes[0].id}").get_json()["pending"] == []
    assert "pending_approvals" not in client.get(
        f"/api/nodes/{nodes[0].id}").get_json()
    assert client.post(
        f"/api/tool-approvals/{pending_id}/approve").status_code == 404
    assert client.post(
        f"/api/tool-approvals/{pending_id}/reject").status_code == 404
    assert _memory(ben) == "- likes tea"


def test_approve_runs_it(app, thread):
    ben, anna, nodes, reply, pending_id = thread

    resp = _client(app, ben).post(f"/api/tool-approvals/{pending_id}/approve")

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {"outcome": "approved", "pending": []}
    assert _memory(ben) == "- likes tea\n- trusts anna"
    assert _db.session.get(Draft, pending_id) is None


def test_reject_changes_nothing(app, thread):
    ben, anna, nodes, reply, pending_id = thread

    resp = _client(app, ben).post(f"/api/tool-approvals/{pending_id}/reject")

    assert resp.get_json()["outcome"] == "rejected"
    assert _memory(ben) == "- likes tea"
    assert _db.session.get(Draft, pending_id) is None
    _db.session.expire_all()
    entry = json.loads(_db.session.get(Node, reply.id).tool_calls_meta)[0]
    assert entry["status"] == "rejected"


def test_a_decided_call_is_gone(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)
    client.post(f"/api/tool-approvals/{pending_id}/reject")

    assert client.post(
        f"/api/tool-approvals/{pending_id}/approve").status_code == 404
    assert _memory(ben) == "- likes tea"


def test_a_waiting_call_is_not_an_input_draft(app, thread):
    """The reply box under the reply reads and saves its own draft, never
    the waiting call kept on the same node."""
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)

    resp = client.get(f"/api/drafts/?parent_id={reply.id}")

    assert f'"id": {pending_id}' not in json.dumps(resp.get_json() or {})
    client.delete(f"/api/drafts/?parent_id={reply.id}")
    assert _db.session.get(Draft, pending_id) is not None


# ── One decision per call ──────────────────────────────────────────────


def _versions(ben):
    _db.session.expire_all()
    return UserArtifact.query.filter_by(user_id=ben.id, kind="memory").count()


def test_a_call_is_claimed_once(app, thread):
    """Two requests deciding the same call at once: the conditional claim
    lets one through."""
    from backend.utils.tool_approvals import claim
    ben, anna, nodes, reply, pending_id = thread

    assert claim(pending_id, ben.id) is True
    assert claim(pending_id, ben.id) is False
    assert claim(pending_id, anna.id) is False


def test_two_approvals_of_one_call_write_once(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)

    first = client.post(f"/api/tool-approvals/{pending_id}/approve")
    second = client.post(f"/api/tool-approvals/{pending_id}/approve")

    assert first.get_json()["outcome"] == "approved"
    assert second.status_code == 404
    assert _versions(ben) == 2


def test_an_approval_while_another_request_decides_it_runs_nothing(
        app, thread):
    """The other request (a reject on another device) holds the claim:
    the approval answers as for a call already decided and writes
    nothing."""
    from backend.utils.tool_approvals import claim
    ben, anna, nodes, reply, pending_id = thread
    assert claim(pending_id, ben.id)

    resp = _client(app, ben).post(f"/api/tool-approvals/{pending_id}/approve")

    assert resp.status_code == 404
    assert _versions(ben) == 1


def test_reject_then_approve_writes_nothing(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)

    client.post(f"/api/tool-approvals/{pending_id}/reject")
    resp = client.post(f"/api/tool-approvals/{pending_id}/approve")

    assert resp.status_code == 404
    assert _versions(ben) == 1


def test_a_run_that_fails_leaves_the_call_waiting(app, thread):
    """The memory changed since the call: approving saves nothing, says
    why, and the call still waits (approve again or reject)."""
    ben, anna, nodes, reply, pending_id = thread
    edited = UserArtifact(user_id=ben.id, kind="memory", title="Memory",
                          generated_by="user", ai_usage="chat")
    edited.set_content("- likes coffee")
    _db.session.add(edited)
    _db.session.commit()
    client = _client(app, ben)

    resp = client.post(f"/api/tool-approvals/{pending_id}/approve")

    data = resp.get_json()
    assert data["outcome"] == "failed"
    assert "changed since" in data["error"]
    assert [p["id"] for p in data["pending"]] == [pending_id]
    assert _memory(ben) == "- likes coffee"
    assert client.post(
        f"/api/tool-approvals/{pending_id}/reject").status_code == 200


def test_an_error_while_running_leaves_the_call_waiting(
        app, thread, monkeypatch):
    ben, anna, nodes, reply, pending_id = thread

    def broken(*a, **k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(_llm_task_mod, "_execute_tool_calls", broken)
    client = _client(app, ben)

    resp = client.post(f"/api/tool-approvals/{pending_id}/approve")

    assert resp.status_code == 500
    assert "still waits" in resp.get_json()["error"]
    assert [p["id"] for p in client.get(
        f"/api/tool-approvals?node={nodes[0].id}").get_json()["pending"]] \
        == [pending_id]
    assert _versions(ben) == 1


def test_each_call_of_one_dialog_is_decided_on_its_own(app, thread):
    """Two waiting calls of one reply: rejecting one leaves the other
    waiting, and the answer lists it."""
    from backend.utils.tool_approvals import ApprovalHold
    ben, anna, nodes, reply, pending_id = thread
    scratch = UserArtifact(user_id=ben.id, kind="scratchpad",
                           title="Scratchpad", generated_by="test",
                           ai_usage="chat")
    scratch.set_content("notes")
    _db.session.add(scratch)
    _db.session.flush()
    second = ApprovalHold(["other_user"], reply.id).hold(
        "update_artifact",
        {"kind": "scratchpad", "updated_content": "notes\nmore",
         "base_version": 1},
        reply, ben.id, previous_artifact_id=scratch.id,
        kind="scratchpad")["pending_id"]
    _db.session.commit()
    client = _client(app, ben)

    resp = client.post(f"/api/tool-approvals/{pending_id}/reject")

    assert [p["id"] for p in resp.get_json()["pending"]] == [second]
    resp = client.post(f"/api/tool-approvals/{second}/approve")
    assert resp.get_json() == {"outcome": "approved", "pending": []}
    assert _memory(ben) == "- likes tea"
