"""A reply whose job can't be queued fails instead of waiting forever (#385).

Every reply is saved first (the placeholder is committed) and its job
queued second. When the queueing raises (the broker is unreachable), no
job exists; before #385 the reply stayed 'pending', and the pages showed
"Thinking…" until their poll gave up. Now the placeholder is marked
failed with a message written for the user, never the exception's text,
and the exception is logged as an error, which reaches Sentry
(llm_nodes.enqueue_reply_job).

Same harness as test_no_replies_for_none (minimal Flask app, completion
task module stubbed, the finalize task body run with the celery glue
stubbed), so nothing reaches a model or a broker. The Glean routes are
covered in test_read_routes, the upload's reply in
test_textmode_upload_agentic.
"""
import logging
from unittest.mock import MagicMock

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, st, _user, _client, _node, _reply, _prompt_root,
    _voice_draft,
)
from backend.extensions import db as _db
from backend.models import Draft, Node

# What a Redis client says when the broker is down: it names the host,
# so it must never reach the user.
BROKER_DOWN = "Error 111 connecting to redis:6379. Connection refused."


@pytest.fixture
def web(app):  # noqa: F811
    """The app with the reply routes (test_no_replies_for_none)."""
    return app


@pytest.fixture
def queue_down(task_mod):  # noqa: F811
    """The completion task's .delay raises as it does with the broker
    down."""
    task_mod.generate_llm_response.delay.side_effect = (
        ConnectionRefusedError(BROKER_DOWN))
    return task_mod


def _message():
    from backend.utils.llm_nodes import REPLY_NOT_QUEUED_MESSAGE
    return REPLY_NOT_QUEUED_MESSAGE


def _assert_failed(llm_node_id):
    reply = Node.query.get(llm_node_id)
    assert reply.node_type == "llm"
    assert reply.llm_task_status == "failed"
    assert reply.llm_task_error == _message()
    assert BROKER_DOWN not in reply.llm_task_error
    assert reply.llm_task_id is None
    return reply


# ── The helper ───────────────────────────────────────────────────────────

class TestEnqueueReplyJob:
    def test_a_failed_dispatch_fails_the_reply_and_logs_an_error(
            self, web, caplog):
        from backend.utils.llm_nodes import enqueue_reply_job
        alice = _user()
        reply = _reply(_node(alice), alice)
        reply.llm_task_status = "pending"
        _db.session.commit()

        def dispatch():
            raise ConnectionRefusedError(BROKER_DOWN)

        with caplog.at_level(logging.ERROR):
            assert enqueue_reply_job(reply, dispatch) is None

        _db.session.expire_all()
        _assert_failed(reply.id)
        # An error with its traceback: Sentry's logging integration turns
        # it into an event. The line names the exception's type only.
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert errors[0].exc_info[1].args == (BROKER_DOWN,)
        assert BROKER_DOWN not in errors[0].getMessage()
        assert "ConnectionRefusedError" in errors[0].getMessage()

    def test_audio_waiting_for_the_job_fails_with_it(self, web):
        from backend.utils.llm_nodes import enqueue_reply_job
        alice = _user()
        reply = _reply(_node(alice), alice)
        reply.llm_task_status = "pending"
        reply.tts_task_status = "pending"
        _db.session.commit()

        enqueue_reply_job(reply, MagicMock(side_effect=OSError("down")))

        _db.session.expire_all()
        assert Node.query.get(reply.id).tts_task_status == "failed"

    def test_a_queued_job_keeps_the_reply_pending(self, web):
        from backend.utils.llm_nodes import enqueue_reply_job
        alice = _user()
        reply = _reply(_node(alice), alice)
        reply.llm_task_status = "pending"
        _db.session.commit()

        assert enqueue_reply_job(
            reply, lambda: MagicMock(id="task-1")) == "task-1"
        assert enqueue_reply_job(
            reply, lambda: MagicMock(id="task-2"),
            record_task_id=False) == "task-2"

        _db.session.expire_all()
        fresh = Node.query.get(reply.id)
        assert fresh.llm_task_status == "pending"
        assert fresh.llm_task_error is None
        assert fresh.llm_task_id == "task-1"

    def test_create_llm_placeholder_returns_the_failed_reply(
            self, web, queue_down):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _user()
        entry = _node(alice)
        _db.session.commit()

        llm_node, task_id = create_llm_placeholder(entry.id, "gpt-5",
                                                   alice.id)

        assert task_id is None
        _assert_failed(llm_node.id)
        assert llm_node.parent_id == entry.id


# ── Every route that starts a reply ──────────────────────────────────────

class TestRoutes:
    def test_llm_response_button(self, web, queue_down):
        alice = _user()
        entry = _node(alice)
        _db.session.commit()

        resp = _client(web, alice).post(f"/api/nodes/{entry.id}/llm",
                                        json={})

        # The page opens the reply and polls it, as for any reply; the
        # poll answers 'failed' with the message, which the page toasts.
        assert resp.status_code == 202, resp.get_json()
        _assert_failed(resp.get_json()["node_id"])

    def test_the_poll_reports_the_failure_to_the_owner(self, web, queue_down):
        alice = _user()
        entry = _node(alice)
        _db.session.commit()
        client = _client(web, alice)
        node_id = client.post(f"/api/nodes/{entry.id}/llm",
                              json={}).get_json()["node_id"]

        data = client.get(f"/api/nodes/{node_id}/llm-status").get_json()

        assert data["status"] == "failed"
        assert data["error"] == _message()

    def test_text_mode_start(self, web, queue_down):
        alice = _user()
        _db.session.commit()

        resp = _client(web, alice).post("/api/textmode/start", json={
            "content": "hello"})

        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert Node.query.get(data["user_node_id"]).get_content() == "hello"
        _assert_failed(data["llm_node_id"])

    def test_text_mode_message(self, web, queue_down):
        alice = _user()
        root = _prompt_root(alice, "textmode")
        msg = _node(alice, root)
        _db.session.commit()

        resp = _client(web, alice).post(
            f"/api/textmode/{root.id}/message",
            json={"content": "more", "parent_id": msg.id})

        assert resp.status_code == 202, resp.get_json()
        _assert_failed(resp.get_json()["llm_node_id"])

    def test_text_mode_from_node(self, web, queue_down):
        alice = _user()
        entry = _node(alice)
        _db.session.commit()

        resp = _client(web, alice).post(
            f"/api/textmode/from-node/{entry.id}",
            json={"content": "a reply", "ai_usage": "chat"})

        assert resp.status_code == 202, resp.get_json()
        _assert_failed(resp.get_json()["llm_node_id"])

    def test_voice_from_node(self, web, queue_down):
        alice = _user()
        root = _prompt_root(alice, "voice")
        msg = _node(alice, root)
        _db.session.commit()

        resp = _client(web, alice).post(f"/api/voice/from-node/{msg.id}",
                                        json={})

        assert resp.status_code == 202, resp.get_json()
        _assert_failed(resp.get_json()["llm_node_id"])

    def test_voice_post(self, web, queue_down):
        alice = _user()
        _db.session.commit()

        resp = _client(web, alice).post("/api/voice/", json={
            "content": "spoken words"})

        assert resp.status_code == 202, resp.get_json()
        _assert_failed(resp.get_json()["llm_node_id"])

    def test_save_as_node_with_auto_generate(self, web, queue_down):
        alice = _user()
        entry = _node(alice)
        draft = Draft(user_id=alice.id, parent_id=entry.id,
                      session_id="sess-queue", streaming_status="completed",
                      privacy_level="private", ai_usage="chat")
        draft.set_content("recorded words")
        _db.session.add(draft)
        _db.session.commit()

        resp = _client(web, alice).post(
            "/api/drafts/streaming/sess-queue/save-as-node",
            json={"auto_generate": True})

        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert "llm_error" not in data
        assert Node.query.get(data["id"]).get_content() == "recorded words"
        _assert_failed(data["llm_node_id"])


# ── A finished Voice recording ───────────────────────────────────────────

class TestVoiceFinalize:
    def test_the_reply_fails_and_the_recording_is_kept(self, st):  # noqa: F811
        st._fake_llm.generate_llm_response.si.return_value \
            .apply_async.side_effect = ConnectionRefusedError(BROKER_DOWN)
        user = _user()
        _voice_draft(user, "sess-down", "chat", text="spoken words")

        result = st.finalize_draft_streaming(
            MagicMock(), "sess-down", 1, label="Voice", user_id=user.id,
            parent_id=None, model="gpt-5")

        assert result["status"] == "completed"
        draft = Draft.query.filter_by(session_id="sess-down").one()
        assert draft.streaming_status == "completed"
        # The Voice screen gets the reply's id and polls it: 'failed'
        # plays the error sound and toasts the message.
        reply = _assert_failed(draft.llm_node_id)
        # No audio is coming for it either.
        assert reply.tts_task_status == "failed"
        entry = Node.query.get(reply.parent_id)
        assert entry.get_content() == "spoken words"
