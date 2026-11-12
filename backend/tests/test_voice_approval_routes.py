"""No Voice turn continues a thread while a change the AI proposed there
waits for the user's approval (Peter, 2026-11-12: "the lockscreen controls
shouldn't allow next recording").

The clients disable recording; the server refuses it too, so an older
client (or a lock-screen command the client missed) cannot continue: a
Voice recording's streaming init and the Voice reply route answer 409
``approval_pending``. Dictation, Text mode and other threads are not
affected, and once every waiting call is approved or rejected Voice works
again. Builds the waiting call the way the task does (the
test_tool_approval_routes thread).
"""
import json
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from flask import Flask

from backend.tests.test_retrieval_loop import _llm_task_mod
from backend.tests.test_tool_approval_routes import (  # noqa: F401
    _client, thread)
from backend.extensions import db as _db
from backend.models import Draft, Node, User, UserArtifact
from backend.utils.tool_approvals import (
    ApprovalHold, approval_pending_message, waiting_in_thread)

CODE = "approval_pending"


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
    from backend.routes.voice import voice_bp
    app.register_blueprint(tool_approvals_bp,
                           url_prefix="/api/tool-approvals")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(voice_bp, url_prefix="/api/voice")
    return app


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    import backend.routes.drafts as drafts_mod
    monkeypatch.setattr(drafts_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _init(client, parent=None, label="Voice"):
    body = {"label": label, "ai_usage": "chat"}
    if label is None:
        body.pop("label")
    if parent is not None:
        body["parent_id"] = parent.id
    return client.post("/api/drafts/streaming/init", json=body)


def _refused(resp, count=1):
    assert resp.status_code == 409, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data == {"error": approval_pending_message(count),
                    "code": CODE, "pending": count}


def _sessions():
    return Draft.query.filter(Draft.session_id.isnot(None)).count()


def test_a_voice_recording_is_refused_while_a_change_waits(app, thread):
    ben, anna, nodes, reply, pending_id = thread

    _refused(_init(_client(app, ben), reply))

    assert _sessions() == 0                  # no session, no mic
    assert _db.session.get(Draft, pending_id).label == "tool_pending"


def test_anywhere_in_the_thread(app, thread):
    """Under any node of the thread: the calls GET /api/tool-approvals
    lists for it."""
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)
    for node in (nodes[0], nodes[2], reply):
        _refused(_init(client, node))
    later = Node(user_id=ben.id, human_owner_id=ben.id, parent_id=reply.id,
                 node_type="user", privacy_level="private", ai_usage="chat")
    later.set_content("typed on")
    _db.session.add(later)
    _db.session.commit()
    _refused(_init(client, later))


def test_the_voice_reply_route_refuses_too(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    before = Node.query.count()

    resp = _client(app, ben).post("/api/voice/", json={
        "content": "spoken words", "parent_id": reply.id})

    _refused(resp)
    assert Node.query.count() == before


def test_dictation_and_other_threads_are_not_affected(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)
    # Dictation in the same thread (Text mode's mic): no Voice label.
    assert _init(client, reply, label=None).status_code == 201
    # A new Voice conversation.
    assert _init(client).status_code == 201
    # Voice in another thread of Ben's.
    other = Node(user_id=ben.id, human_owner_id=ben.id, node_type="user",
                 privacy_level="private", ai_usage="chat")
    other.set_content("another thread")
    _db.session.add(other)
    _db.session.commit()
    assert _init(client, other).status_code == 201


def test_someone_elses_waiting_call_does_not_block_anna(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    assert waiting_in_thread(anna.id, nodes[1]) == 0
    assert waiting_in_thread(ben.id, nodes[1]) == 1


@pytest.mark.parametrize("approve", [True, False])
def test_voice_works_again_once_the_call_is_decided(app, thread, approve):
    ben, anna, nodes, reply, pending_id = thread
    client = _client(app, ben)
    _refused(_init(client, reply))

    verb = "approve" if approve else "reject"
    resp = client.post(f"/api/tool-approvals/{pending_id}/{verb}")
    assert resp.get_json()["pending"] == []

    assert _init(client, reply).status_code == 201


def test_every_waiting_call_must_be_decided(app, thread):
    """Two calls wait: deciding one is not enough."""
    ben, anna, nodes, reply, pending_id = thread
    memory = UserArtifact.latest_for(ben.id, "memory")
    fields = ApprovalHold(["other_user"], reply.id).hold(
        "update_artifact", {"kind": "memory", "updated_content": "- tea",
                            "base_version": 1}, reply, ben.id,
        previous_artifact_id=memory.id, kind="memory")
    meta = json.loads(reply.tool_calls_meta)
    meta.append({"name": "update_artifact", "input": {"kind": "memory"},
                 **fields})
    reply.tool_calls_meta = json.dumps(meta)
    _db.session.commit()
    client = _client(app, ben)

    _refused(_init(client, reply), count=2)
    client.post(f"/api/tool-approvals/{pending_id}/reject")
    _refused(_init(client, reply), count=1)
    client.post(f"/api/tool-approvals/{fields['pending_id']}/reject")
    assert _init(client, reply).status_code == 201


def test_a_stale_change_still_blocks_until_rejected(app, thread):
    """Ben edited his memory since: the call can only be rejected, and it
    blocks Voice until he does."""
    ben, anna, nodes, reply, pending_id = thread
    newer = UserArtifact(user_id=ben.id, kind="memory", title="Memory",
                         generated_by="user", ai_usage="chat")
    newer.set_content("- likes coffee")
    _db.session.add(newer)
    _db.session.commit()
    client = _client(app, ben)
    [item] = client.get(
        f"/api/tool-approvals?node={reply.id}").get_json()["pending"]
    assert item["stale"] is True

    _refused(_init(client, reply))
    client.post(f"/api/tool-approvals/{pending_id}/reject")
    assert _init(client, reply).status_code == 201


def test_a_call_of_a_deleted_reply_does_not_block(app, thread):
    ben, anna, nodes, reply, pending_id = thread
    reply.deleted_at = datetime.utcnow()
    _db.session.commit()

    assert _init(_client(app, ben), nodes[2]).status_code == 201


def test_the_thread_walk_is_bounded_in_queries(app, thread):
    """The check runs before every Voice recording: three queries for the
    thread whatever its length, plus one for the waiting calls."""
    from sqlalchemy import event
    ben, anna, nodes, reply, pending_id = thread
    parent = reply
    for i in range(30):
        node = Node(user_id=ben.id, human_owner_id=ben.id,
                    parent_id=parent.id, node_type="user",
                    privacy_level="private", ai_usage="chat")
        node.set_content(f"turn {i}")
        _db.session.add(node)
        _db.session.flush()
        parent = node
    _db.session.commit()
    _db.session.expire_all()
    tip = _db.session.get(Node, parent.id)
    statements = []
    engine = _db.engine
    listener = MagicMock(side_effect=lambda *a, **k: statements.append(a[2]))
    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert waiting_in_thread(ben.id, tip) == 1
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    # ancestors (walk + row load), descendants, the drafts, the one
    # reply's row: no query per level.
    assert len(statements) <= 6, statements
