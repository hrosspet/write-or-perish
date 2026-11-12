"""Merge train 2026-11-12 (2): two reasons to refuse a new Voice turn.

- A change the AI proposed in the thread waits for the user's approval:
  409 ``approval_pending`` (Peter, 2026-11-12).
- The thread is too long for another reply: 409 ``thread_too_long``
  (#648).

When both apply, the waiting change answers first: it needs a decision
before anything else. Once it is decided, the next Voice turn meets the
length. Each alone keeps its own answer. A recording finished while both
apply is saved without a reply, for the waiting change.

Same harness as test_thread_size (minimal app, completion task stubbed, the
finalize task body run with the celery glue stubbed), so nothing reaches a
model.
"""
from unittest.mock import MagicMock

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, st, _user, _client, _voice_draft,
)
from backend.tests.test_thread_size import LIMIT, _thread
from backend.extensions import db as _db
from backend.models import Draft, Node

TOO_LONG = "thread_too_long"
WAITING = "approval_pending"
OVER = 9_700     # a measured thread at the limit (LIMIT 10,000)
UNDER = 2_000    # a thread far under the notice


@pytest.fixture
def limited(app):  # noqa: F811
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    app.config["THREAD_PROMPT_NOTICE_FRACTION"] = 0.9
    return app


def _wait_on(reply, user):
    """A change the AI proposed in *reply* waits for *user*'s approval
    (the row the task holds a write call in)."""
    waiting = Draft(user_id=user.id, parent_id=reply.id, label="tool_pending")
    waiting.set_content("{}")
    _db.session.add(waiting)
    _db.session.commit()
    return waiting


def _decide(waiting):
    _db.session.delete(waiting)
    _db.session.commit()


def _code(resp):
    assert resp.status_code == 409, resp.get_data(as_text=True)
    return resp.get_json()["code"]


def _init(client, parent):
    return client.post("/api/drafts/streaming/init", json={
        "label": "Voice", "ai_usage": "chat", "parent_id": parent.id})


def _sessions():
    return Draft.query.filter(Draft.session_id.isnot(None)).count()


class TestBothRefusals:
    def test_streaming_init_answers_the_waiting_change_first(self, limited):
        alice = _user()
        _root, reply, entry = _thread(alice, measured=OVER)
        waiting = _wait_on(reply, alice)
        client = _client(limited, alice)

        data = _init(client, entry).get_json()
        assert data["code"] == WAITING
        assert data["pending"] == 1
        assert _sessions() == 0

        _decide(waiting)
        assert _code(_init(client, entry)) == TOO_LONG
        assert _sessions() == 0

    def test_the_voice_reply_route_answers_the_waiting_change_first(
            self, limited, task_mod):  # noqa: F811
        alice = _user()
        _root, reply, entry = _thread(alice, measured=OVER)
        waiting = _wait_on(reply, alice)
        client = _client(limited, alice)
        before = Node.query.count()

        assert _code(client.post("/api/voice/", json={
            "content": "spoken", "parent_id": entry.id})) == WAITING
        _decide(waiting)
        assert _code(client.post("/api/voice/", json={
            "content": "spoken", "parent_id": entry.id})) == TOO_LONG
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_voice_from_an_entry_opens_ready_then_meets_the_length(
            self, limited, task_mod):  # noqa: F811
        # The thread page's Voice button on an entry: while the change
        # waits, the Voice screen opens ready (its dialog), and no reply is
        # made; once decided, the same press is refused for the length.
        alice = _user()
        _root, reply, entry = _thread(alice, measured=OVER)
        waiting = _wait_on(reply, alice)
        client = _client(limited, alice)

        resp = client.post(f"/api/voice/from-node/{entry.id}", json={})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["mode"] == "ready"
        assert Node.query.filter_by(node_type="llm").count() == 1   # the reply only

        _decide(waiting)
        before = Node.query.count()
        assert _code(client.post(f"/api/voice/from-node/{entry.id}",
                                 json={})) == TOO_LONG
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()


class TestEachAlone:
    def test_the_length_alone(self, limited):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=OVER)
        assert _code(_init(_client(limited, alice), entry)) == TOO_LONG

    def test_the_waiting_change_alone(self, limited):
        alice = _user()
        _root, reply, entry = _thread(alice, measured=UNDER)
        waiting = _wait_on(reply, alice)
        client = _client(limited, alice)
        assert _code(_init(client, entry)) == WAITING
        _decide(waiting)
        assert _init(client, entry).status_code == 201

    def test_neither(self, limited):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=UNDER)
        assert _init(_client(limited, alice), entry).status_code == 201

    def test_dictation_records_through_both(self, limited):
        alice = _user()
        _root, reply, entry = _thread(alice, measured=OVER)
        _wait_on(reply, alice)
        resp = _client(limited, alice).post(
            "/api/drafts/streaming/init",
            json={"ai_usage": "chat", "parent_id": entry.id})
        assert resp.status_code == 201, resp.get_json()


class TestFinalize:
    def _finalize(self, st, user, session_id, parent):  # noqa: F811
        return st.finalize_draft_streaming(
            MagicMock(), session_id, 1, label="Voice", user_id=user.id,
            parent_id=parent.id, model="gpt-5")

    def test_both_the_recording_is_kept_for_the_waiting_change(self, st):  # noqa: F811
        from backend.utils.thread_size import skipped_reply_code
        st.flask_app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
        alice = _user()
        _root, reply, entry = _thread(alice, measured=OVER)
        waiting = _wait_on(reply, alice)
        _voice_draft(alice, "sess-both", "chat", text="spoken words",
                     parent=entry)

        self._finalize(st, alice, "sess-both", entry)

        draft = Draft.query.filter_by(session_id="sess-both").one()
        assert draft.llm_node_id is None
        assert draft.streaming_warning == st.VOICE_REPLY_SKIPPED_APPROVAL
        # Not the length's warning: the clients don't say the length aloud.
        assert skipped_reply_code(draft.streaming_warning) is None
        kept = Node.query.filter_by(parent_id=entry.id).one()
        assert kept.get_content() == "spoken words"
        st._fake_llm.generate_llm_response.si.assert_not_called()
        st._fake_llm.prewarm_anthropic_cache.assert_not_called()

        # Decided: the next recording there is kept for the length.
        _decide(waiting)
        _voice_draft(alice, "sess-next", "chat", text="more words",
                     parent=kept)
        self._finalize(st, alice, "sess-next", kept)
        draft = Draft.query.filter_by(session_id="sess-next").one()
        assert draft.llm_node_id is None
        assert skipped_reply_code(draft.streaming_warning) == TOO_LONG
        st._fake_llm.generate_llm_response.si.assert_not_called()

    def test_no_prewarm_while_either_applies(self, st):  # noqa: F811
        st.flask_app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
        alice = _user()
        _root, reply, entry = _thread(alice, measured=OVER)
        waiting = _wait_on(reply, alice)
        assert st._voice_approval_skip(alice.id, entry.id) is not None
        assert st._voice_thread_too_long(alice.id, entry.id, "gpt-5")
        _decide(waiting)
        assert st._voice_approval_skip(alice.id, entry.id) is None
        assert st._voice_thread_too_long(alice.id, entry.id, "gpt-5")
