"""A failed reply's error text goes to its owner only.

When an AI reply fails, its node keeps the reason (llm_task_error). The
reply's owner (the user who asked for it, else the node's author: the
owner GET /nodes/<id> and the access checks use) gets that text on the
status poll (GET /nodes/<id>/llm-status) and in the llm-stream SSE's
done event. Anyone else who can see the reply, admins included, gets the
status 'failed' with error null. The same holds for a task's warnings on
the status poll, and for a failed recording's transcription error on
GET /nodes/<id>/transcription-status.

Routes run in the test_no_replies_for_none harness (in-memory sqlite, the
completion task stubbed), with the SSE blueprint added.
"""
import json

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user, _node, _reply, _client,
)
from backend.extensions import db as _db
from backend.utils.privacy import is_node_owner

ERROR = "Provider said: overloaded (request req_9f2c, org quota)"
WARNING = "Your entry is saved. Loore couldn't start a reply: quota."
TRANSCRIPTION_ERROR = "APIError: upstream 502 from transcription provider"


@pytest.fixture
def sse_app(app):  # noqa: F811
    from backend.routes.sse import sse_bp
    app.register_blueprint(sse_bp, url_prefix="/api/sse")
    return app


def _failed_reply(entry, owner):
    reply = _reply(entry, owner)
    reply.privacy_level = "public"
    reply.llm_task_status = "failed"
    reply.llm_task_error = ERROR
    _db.session.commit()
    return reply


def _public_failed_reply(alice):
    """alice's public entry with her failed AI reply under it."""
    return _failed_reply(_node(alice, privacy_level="public"), alice)


def _done_event(client, node_id):
    resp = client.get(f"/api/sse/nodes/{node_id}/llm-stream")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    events = []
    for block in resp.get_data(as_text=True).strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines.get("event"), json.loads(lines["data"])))
    assert [e for e, _ in events] == ["done"]
    return events[0][1]


class TestStatusPoll:
    def test_someone_else_gets_failed_without_the_text(self, app):  # noqa: F811
        alice = _user("alice")
        reply = _public_failed_reply(alice)
        bob = _user("bob")
        _db.session.commit()

        resp = _client(app, bob).get(f"/api/nodes/{reply.id}/llm-status")
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["status"] == "failed"
        assert data["error"] is None
        assert ERROR not in resp.get_data(as_text=True)

    def test_an_admin_who_is_not_the_owner_gets_the_same(self, app):  # noqa: F811
        alice = _user("alice")
        reply = _public_failed_reply(alice)
        admin = _user("admin")
        admin.is_admin = True
        _db.session.commit()

        resp = _client(app, admin).get(f"/api/nodes/{reply.id}/llm-status")
        assert resp.get_json()["status"] == "failed"
        assert resp.get_json()["error"] is None

    def test_the_owner_gets_the_text(self, app):  # noqa: F811
        alice = _user("alice")
        reply = _public_failed_reply(alice)

        resp = _client(app, alice).get(f"/api/nodes/{reply.id}/llm-status")
        assert resp.get_json()["status"] == "failed"
        assert resp.get_json()["error"] == ERROR

    def test_the_owner_is_who_asked_for_the_reply(self, app):  # noqa: F811
        # bob asks for a reply under alice's public entry: the failure is
        # bob's, so bob gets the text and alice does not.
        alice = _user("alice")
        bob = _user("bob")
        reply = _failed_reply(_node(alice, privacy_level="public"), bob)

        assert _client(app, alice).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()["error"] is None
        assert _client(app, bob).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()["error"] == ERROR

    def test_warnings_go_to_the_owner_only(self, app):  # noqa: F811
        alice = _user("alice")
        entry = _node(alice, privacy_level="public")
        entry.llm_task_warnings = json.dumps([WARNING])
        bob = _user("bob")
        _db.session.commit()

        theirs = _client(app, bob).get(f"/api/nodes/{entry.id}/llm-status")
        assert theirs.get_json()["warnings"] == []
        assert WARNING not in theirs.get_data(as_text=True)
        mine = _client(app, alice).get(f"/api/nodes/{entry.id}/llm-status")
        assert mine.get_json()["warnings"] == [WARNING]


class TestStreamDone:
    def test_someone_else_gets_failed_without_the_text(self, sse_app):
        alice = _user("alice")
        reply = _public_failed_reply(alice)
        bob = _user("bob")
        _db.session.commit()

        done = _done_event(_client(sse_app, bob), reply.id)
        assert done == {"status": "failed", "continuation_node_id": None,
                        "error": None}

    def test_the_owner_gets_the_text(self, sse_app):
        alice = _user("alice")
        reply = _public_failed_reply(alice)

        done = _done_event(_client(sse_app, alice), reply.id)
        assert done["status"] == "failed"
        assert done["error"] == ERROR

    def test_the_owner_is_who_asked_for_the_reply(self, sse_app):
        alice = _user("alice")
        bob = _user("bob")
        reply = _failed_reply(_node(alice, privacy_level="public"), bob)

        assert _done_event(_client(sse_app, alice), reply.id)["error"] is None
        assert _done_event(_client(sse_app, bob), reply.id)["error"] == ERROR


class TestCancelledRead:
    """A Read withdrawn because its owner hit the spend cap, as
    llm_completion._withdraw_batch_reply leaves it (test_read_batch_poll
    checks the task writes this)."""

    NEUTRAL = "This read was cancelled."
    REASON = ("This read was cancelled before it ran: the monthly spend "
              "cap was reached while it was queued at the provider, so the "
              "request was withdrawn and nothing was billed.")

    def _cancelled_read(self, alice):
        reply = _reply(_node(alice, privacy_level="public"), alice,
                       content=self.NEUTRAL)
        reply.privacy_level = "public"
        reply.llm_task_status = "cancelled"
        reply.llm_task_error = self.REASON
        reply.tool_calls_meta = json.dumps([{
            "name": "_batch", "batch_id": "batch_1", "status": "cancelled",
            "cancel_reason": "spend_cap", "cancel_outcome": "not_processed"}])
        _db.session.commit()
        return reply

    def test_another_viewer_sees_the_neutral_text_only(self, app):  # noqa: F811
        alice = _user("alice")
        reply = self._cancelled_read(alice)
        bob = _user("bob")
        _db.session.commit()
        bobs = _client(app, bob)

        status = bobs.get(f"/api/nodes/{reply.id}/llm-status")
        assert status.get_json()["status"] == "cancelled"
        assert status.get_json()["content"] == self.NEUTRAL
        assert status.get_json()["error"] is None
        node = bobs.get(f"/api/nodes/{reply.id}")
        assert node.get_json()["content"] == self.NEUTRAL
        for resp in (status, node):
            assert resp.status_code == 200, resp.get_json()
            body = resp.get_data(as_text=True)
            assert "spend" not in body
            assert "billed" not in body

    def test_the_owner_gets_the_reason(self, app):  # noqa: F811
        alice = _user("alice")
        reply = self._cancelled_read(alice)

        data = _client(app, alice).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()
        assert data["content"] == self.NEUTRAL
        assert data["error"] == self.REASON


class TestTranscriptionStatus:
    def test_a_failed_recordings_error_goes_to_its_author_only(
            self, app):  # noqa: F811
        alice = _user("alice")
        recording = _node(alice, privacy_level="public", content="")
        recording.transcription_status = "failed"
        recording.transcription_error = TRANSCRIPTION_ERROR
        bob = _user("bob")
        _db.session.commit()

        theirs = _client(app, bob).get(
            f"/api/nodes/{recording.id}/transcription-status")
        assert theirs.status_code == 200, theirs.get_json()
        assert theirs.get_json()["status"] == "failed"
        assert theirs.get_json()["error"] is None
        mine = _client(app, alice).get(
            f"/api/nodes/{recording.id}/transcription-status")
        assert mine.get_json()["error"] == TRANSCRIPTION_ERROR


def test_owner_helper(app):  # noqa: F811
    alice = _user("alice")
    bob = _user("bob")
    entry = _node(alice, privacy_level="public")
    reply = _reply(entry, bob)
    assert is_node_owner(entry, alice.id)
    assert not is_node_owner(entry, bob.id)
    assert is_node_owner(reply, bob.id)
    assert not is_node_owner(reply, alice.id)
    assert not is_node_owner(reply, None)
    # A node with no human owner recorded falls back to its author.
    reply.human_owner_id = None
    assert is_node_owner(reply, reply.user_id)
