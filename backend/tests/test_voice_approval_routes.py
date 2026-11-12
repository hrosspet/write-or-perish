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


def _entry_under(reply, ben, with_prompt):
    """Ben's entry under the reply whose change waits (typed in Text mode,
    or a recording saved without a reply), in a thread with or without an
    agentic prompt above."""
    if with_prompt:
        from backend.models import UserPrompt
        from backend.utils.context_artifacts import attach_context_artifacts
        prompt = UserPrompt(user_id=ben.id, prompt_key="voice", title="Voice",
                            generated_by="default")
        prompt.set_content("system prompt body")
        _db.session.add(prompt)
        _db.session.flush()
        root = reply
        while root.parent is not None:
            root = root.parent
        attach_context_artifacts(root.id, ben.id, prompt_record=prompt)
    entry = Node(user_id=ben.id, human_owner_id=ben.id, parent_id=reply.id,
                 node_type="user", privacy_level="private", ai_usage="chat")
    entry.set_content("typed after the reply")
    _db.session.add(entry)
    _db.session.commit()
    return entry


@pytest.mark.parametrize("with_prompt", [True, False])
def test_voice_from_an_entry_makes_no_reply_while_a_change_waits(
        app, thread, monkeypatch, with_prompt):
    """Voice Mode pressed on an entry under the reply whose change waits
    (web NodeDetail, iPhone ThreadModel.startVoice): no reply is made; the
    Voice screen opens ready (where the dialog shows), as for a gleaning.
    Once the change is decided, the same press starts a reply."""
    import backend.routes.voice as voice_mod
    ben, anna, nodes, reply, pending_id = thread
    entry = _entry_under(reply, ben, with_prompt)
    started = []

    def placeholder(parent_id, model_id, user_id, **kwargs):
        started.append(parent_id)
        return Node.query.get(parent_id)

    monkeypatch.setattr(voice_mod, "create_llm_placeholder_node", placeholder)
    client = _client(app, ben)

    resp = client.post(f"/api/voice/from-node/{entry.id}", json={})

    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["mode"] == "ready"
    assert started == []
    assert Node.query.filter_by(node_type="llm").count() == 1   # R only
    if with_prompt:
        assert data["parent_id"] == entry.id
    else:
        # The Voice prompt is attached under the entry, as for a gleaning.
        assert Node.query.get(data["parent_id"]).parent_id == entry.id

    client.post(f"/api/tool-approvals/{pending_id}/reject")
    resp = client.post(f"/api/voice/from-node/{entry.id}", json={})
    assert resp.get_json()["mode"] == "processing"
    assert len(started) == 1


def test_a_voice_reply_through_llm_response_waits_too(app, thread, monkeypatch):
    """POST /nodes/<id>/llm with source_mode "voice" (no client sends it)
    is refused like a Voice turn; LLM Response in Text mode is not."""
    import backend.routes.nodes as nodes_mod
    ben, anna, nodes, reply, pending_id = thread
    asked = []

    def placeholder(parent_id, model_id, user_id, **kwargs):
        asked.append(kwargs.get("source_mode"))
        return Node.query.get(parent_id), "task-1"

    monkeypatch.setattr(nodes_mod, "create_llm_placeholder", placeholder)
    client = _client(app, ben)

    _refused(client.post(f"/api/nodes/{reply.id}/llm",
                         json={"source_mode": "voice"}))
    resp = client.post(f"/api/nodes/{reply.id}/llm",
                       json={"source_mode": "textmode"})
    assert resp.status_code != 409
    assert asked == ["textmode"]


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


# ── The spoken announcement: its own cached clip ────────────────────────
# Peter, 2026-11-12: "the announcement should be TTSed as a separate chunk
# and appended to the queue. So that when it's not pending, it's not voiced
# out anymore". The players ask GET /api/voice/approval-announcement for
# the reply they play and append the clip while its calls wait.

@pytest.fixture
def clips(monkeypatch, tmp_path):
    """The TTS side of the clip, faked: each call is recorded and writes
    a small file; the model and voice can be changed."""
    from backend.utils import announcement_audio
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
    state = {"voice": "alloy", "calls": [], "costs": [], "fail": False}
    monkeypatch.setattr(announcement_audio, "_tts_settings",
                        lambda: ("gpt-4o-mini-tts", state["voice"]))

    def synthesize(text, path):
        if state["fail"]:
            raise RuntimeError("no TTS key")
        state["calls"].append(text)
        path.write_bytes(b"ID3" + text.encode())
        return 4.2

    monkeypatch.setattr(announcement_audio, "_synthesize", synthesize)
    monkeypatch.setattr(announcement_audio, "_log_cost",
                        lambda user_id, seconds: state["costs"].append(
                            (user_id, seconds)))
    return state


def _voice_reply(reply):
    meta = json.loads(reply.tool_calls_meta)
    meta.append({"name": "_mode", "source_mode": "voice"})
    reply.tool_calls_meta = json.dumps(meta)
    _db.session.commit()


def _announcement(client, node):
    return client.get(f"/api/voice/approval-announcement?node={node.id}")


def test_a_reply_with_a_waiting_change_gets_the_clip(app, thread, clips):
    from backend.utils.announcement_audio import ANNOUNCEMENT_TEXTS
    ben, anna, nodes, reply, pending_id = thread
    _voice_reply(reply)
    client = _client(app, ben)

    data = _announcement(client, reply).get_json()

    assert data["count"] == 1 and data["duration"] == 4.2
    assert data["url"].startswith(
        "/api/voice/approval-announcement/one.mp3?v=")
    assert clips["calls"] == [ANNOUNCEMENT_TEXTS["one"]]
    assert clips["costs"] == [(ben.id, 4.2)]
    audio = client.get(data["url"])
    assert audio.status_code == 200 and audio.mimetype == "audio/mpeg"
    assert audio.data == b"ID3" + ANNOUNCEMENT_TEXTS["one"].encode()


def test_the_clip_is_made_once(app, thread, clips):
    """Cached on disk: later replies, replays and the audio route cost no
    speech call."""
    ben, anna, nodes, reply, pending_id = thread
    _voice_reply(reply)
    client = _client(app, ben)
    first = _announcement(client, reply).get_json()
    second = _announcement(client, reply).get_json()
    client.get(first["url"])

    assert first == second
    assert len(clips["calls"]) == 1 and len(clips["costs"]) == 1


def test_another_voice_makes_another_clip(app, thread, clips):
    ben, anna, nodes, reply, pending_id = thread
    _voice_reply(reply)
    client = _client(app, ben)
    alloy = _announcement(client, reply).get_json()["url"]
    clips["voice"] = "nova"
    nova = _announcement(client, reply).get_json()["url"]

    assert alloy != nova
    assert len(clips["calls"]) == 2


def test_several_waiting_changes_get_the_plural_clip(app, thread, clips):
    from backend.utils.announcement_audio import ANNOUNCEMENT_TEXTS
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
    _voice_reply(reply)

    data = _announcement(_client(app, ben), reply).get_json()

    assert data["count"] == 2
    assert "/several.mp3?v=" in data["url"]
    assert clips["calls"] == [ANNOUNCEMENT_TEXTS["several"]]
    assert "2" not in ANNOUNCEMENT_TEXTS["several"]


@pytest.mark.parametrize("approve", [True, False])
def test_no_clip_once_the_change_is_decided(app, thread, clips, approve):
    """A replay after the decision plays the reply alone."""
    ben, anna, nodes, reply, pending_id = thread
    _voice_reply(reply)
    client = _client(app, ben)
    assert _announcement(client, reply).get_json()["count"] == 1

    verb = "approve" if approve else "reject"
    client.post(f"/api/tool-approvals/{pending_id}/{verb}")

    assert _announcement(client, reply).get_json() == {
        "count": 0, "url": None, "duration": None}


def test_no_clip_when_it_cannot_be_made(app, thread, clips):
    """No TTS key or a provider error: no clip, the reply plays alone; the
    change still waits and still blocks Voice."""
    ben, anna, nodes, reply, pending_id = thread
    _voice_reply(reply)
    clips["fail"] = True
    client = _client(app, ben)

    assert _announcement(client, reply).get_json() == {
        "count": 1, "url": None, "duration": None}
    assert client.get(
        "/api/voice/approval-announcement/one.mp3").status_code == 503
    assert clips["costs"] == []
    _refused(_init(client, reply))


def test_only_the_users_own_voice_reply(app, thread, clips):
    ben, anna, nodes, reply, pending_id = thread
    # Not made in Voice mode (no "_mode" entry): nothing to announce.
    assert _announcement(_client(app, ben), reply).get_json()["count"] == 0
    _voice_reply(reply)
    # Someone else's reply looks like no reply at all.
    assert _announcement(_client(app, anna), reply).status_code == 404
    assert _client(app, ben).get(
        "/api/voice/approval-announcement/loud.mp3").status_code == 404
    assert clips["calls"] == []
