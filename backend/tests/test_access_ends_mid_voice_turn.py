"""Payments on (#438): a Voice turn whose account loses its access while
the turn is recorded is kept and transcribed; only the reply is skipped,
the entry says why, and the Voice screen says it aloud.

Peter, 2026-11-13: "Voice mode shouldn't start with a subscription
expired. I was talking about the edge case when subscription expires
during user's voice recording." Starting stays refused (402, nothing
written: the controls below); a turn that started with access ends as a
recording kept without its reply, in every way the clients finish it:

- the finalize task (the usual way): the entry carries the "recording"
  text of subscriptions.paused_message, and the draft's warning names
  its spoken sentence (``warning_code``, utils/voice_refusals);
- the fallback reply request (POST /voice with the recording's
  session_id): the turn is saved and the 402 carries ``user_node_id``
  and the sentence's ``code``, as the 409 of a thread that grew too long
  does;
- a reply already queued when the access ended: the completion task
  refuses it with the same text and the code (llm-status
  ``error_code``), tested in test_no_replies_for_none_task.py.

The chunks and the dictation/upload paths are in
test_access_ends_mid_recording.py.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import uuid

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, st, _user, _client, _voice_draft, _node, _reply,
    _llm_count,
)
from backend.extensions import db as _db
from backend.models import Draft, Node
from backend.utils.voice_refusals import (
    SPOKEN_REFUSALS, SUBSCRIPTION_CODES, skipped_reply_code,
)

TEXTS = {
    "ended": ("Your subscription has ended, so Loore doesn't reply. "
              "Your recording is saved."),
    "past_due": ("Your last payment didn't go through, so Loore doesn't "
                 "reply. Your recording is saved."),
    "free_month": ("Your free month has ended, so Loore doesn't reply. "
                   "Your recording is saved."),
    "none": ("Loore doesn't reply on an account without a subscription. "
             "Your recording is saved."),
}


def _lapse(user, reason="ended"):
    now = datetime.utcnow()
    user.plan = "free"
    user.subscription_level = "standard"
    user.subscription_ever_paid = True
    if reason == "free_month":
        user.subscription_status = "none"
        user.subscription_ever_paid = False
        user.alpha_free_granted_at = now - timedelta(days=40)
        user.alpha_free_start = now - timedelta(days=40)
        user.alpha_free_until = now - timedelta(days=10)
    elif reason == "none":
        user.subscription_status = "active"
        user.subscription_revoked = "dispute"
    else:
        user.subscription_status = reason
    _db.session.commit()
    return user


# ── The texts and the codes ──────────────────────────────────────────────

def test_one_text_per_reason_said_as_the_screen_shows_it():
    from backend.utils.subscriptions import PAUSED_REASONS, paused_message
    assert sorted(SUBSCRIPTION_CODES) == sorted(PAUSED_REASONS)
    for reason, code in SUBSCRIPTION_CODES.items():
        assert paused_message(reason, "recording") == TEXTS[reason]
        assert SPOKEN_REFUSALS[code] == TEXTS[reason]
        assert skipped_reply_code(TEXTS[reason]) == code
    # The other kinds keep their words.
    assert paused_message("ended") == (
        "Your subscription has ended, so Loore doesn't reply. "
        "Your writing is saved.")


# ── The finalize task ────────────────────────────────────────────────────

class TestFinalize:
    def _finalize(self, st, user, session_id, parent=None, model="gpt-5"):  # noqa: F811
        return st.finalize_draft_streaming(
            MagicMock(), session_id, 1, label="Voice", user_id=user.id,
            parent_id=parent.id if parent else None, model=model)

    @pytest.mark.parametrize("reason", sorted(TEXTS))
    def test_kept_with_its_text_no_reply_and_its_sentence(self, st, reason):  # noqa: F811
        st.flask_app.config["PAYMENTS_ENABLED"] = True
        user = _user()
        _voice_draft(user, "sess-lapsed", "chat", text="spoken words")
        _lapse(user, reason)

        result = self._finalize(st, user, "sess-lapsed")

        assert result["status"] == "completed"
        draft = Draft.query.filter_by(session_id="sess-lapsed").one()
        assert draft.streaming_status == "completed"
        assert draft.llm_node_id is None
        assert draft.streaming_warning == TEXTS[reason]
        assert skipped_reply_code(draft.streaming_warning) == \
            SUBSCRIPTION_CODES[reason]
        entry = Node.query.filter_by(node_type="user").filter(
            Node.parent_id.isnot(None)).one()
        assert entry.get_content() == "spoken words"
        assert TEXTS[reason] in (entry.llm_task_warnings or "")
        assert _llm_count() == 0
        st._fake_llm.generate_llm_response.si.assert_not_called()

    def test_a_continued_thread_keeps_the_turn_under_it(self, st):  # noqa: F811
        st.flask_app.config["PAYMENTS_ENABLED"] = True
        user = _user()
        root = _node(user)
        reply = _reply(root, user)
        _voice_draft(user, "sess-thread", "chat", text="more words",
                     parent=reply)
        _lapse(user)

        self._finalize(st, user, "sess-thread", parent=reply)

        draft = Draft.query.filter_by(session_id="sess-thread").one()
        assert draft.streaming_warning == TEXTS["ended"]
        entry = Node.query.filter_by(parent_id=reply.id).one()
        assert entry.get_content() == "more words"
        assert _llm_count() == 1     # the earlier reply only

    @pytest.mark.parametrize("lapsed", [True, False])
    def test_no_cache_warm_for_a_reply_that_will_not_come(self, st, lapsed):  # noqa: F811
        # Pins what held already; the active account is the control.
        st.flask_app.config["PAYMENTS_ENABLED"] = True
        user = _user()
        user.plan = "free"
        user.subscription_level = "standard"
        user.subscription_status = "active"
        _voice_draft(user, "sess-warm", "chat", text="x" * 600)
        draft = Draft.query.filter_by(session_id="sess-warm").one()
        draft.set_content("x" * 600)
        _db.session.commit()
        if lapsed:
            _lapse(user)

        self._finalize(st, user, "sess-warm", model="claude-test")

        warmed = st._fake_llm.prewarm_anthropic_cache.delay.called
        assert warmed is (not lapsed)

    def test_payments_off_the_turn_gets_its_reply(self, st):  # noqa: F811
        # The control: the switch off, an account whose subscription ended
        # is on the old plan check, and alpha replies.
        st.flask_app.config["PAYMENTS_ENABLED"] = False
        user = _user()
        _voice_draft(user, "sess-off", "chat", text="spoken words")
        user.subscription_status = "ended"
        _db.session.commit()

        self._finalize(st, user, "sess-off")

        draft = Draft.query.filter_by(session_id="sess-off").one()
        assert draft.streaming_warning is None
        assert draft.llm_node_id is not None


# ── The routes ───────────────────────────────────────────────────────────

@pytest.fixture
def on(app):  # noqa: F811
    app.config["PAYMENTS_ENABLED"] = True
    return app


def _recorded_session(user):
    """A Voice recording streaming init accepted (its draft), finished."""
    session_id = str(uuid.uuid4())
    draft = Draft(user_id=user.id, session_id=session_id, label="Voice",
                  streaming_status="completed", ai_usage="chat",
                  privacy_level="private", streaming_mime_type="audio/webm")
    draft.set_content("spoken words")
    _db.session.add(draft)
    _db.session.commit()
    return session_id


class TestTheStatus:
    @pytest.mark.parametrize("reason", sorted(TEXTS))
    def test_the_streaming_status_names_the_sentence(self, on, reason):
        user = _lapse(_user(), reason)
        draft = Draft(user_id=user.id, session_id=f"sess-{reason}",
                      streaming_status="completed", label="Voice",
                      privacy_level="private", ai_usage="chat",
                      streaming_warning=TEXTS[reason])
        draft.set_content("spoken words")
        _db.session.add(draft)
        _db.session.commit()

        data = _client(on, user).get(
            f"/api/drafts/streaming/sess-{reason}/status").get_json()

        assert data["warning"] == TEXTS[reason]
        assert data["warning_code"] == SUBSCRIPTION_CODES[reason]

    def test_a_refused_reply_names_its_sentence(self, on):
        # The completion task's refusal (test_no_replies_for_none_task)
        # reaches the Voice screen as llm-status ``error_code``.
        from backend.utils.thread_size import REFUSED_MARKER
        from backend.utils.tool_meta import update_tool_meta
        user = _lapse(_user())
        entry = _node(user)
        reply = _reply(entry, user)
        reply.llm_task_status = "failed"
        reply.llm_task_error = TEXTS["ended"]
        update_tool_meta(reply, REFUSED_MARKER,
                         {"code": SUBSCRIPTION_CODES["ended"]},
                         append_if_missing=True)
        _db.session.commit()

        data = _client(on, user).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()

        assert data["status"] == "failed"
        assert data["error"] == TEXTS["ended"]
        assert data["error_code"] == SUBSCRIPTION_CODES["ended"]


class TestTheFallbackReplyRequest:
    @pytest.mark.parametrize("reason", sorted(TEXTS))
    def test_a_recorded_turn_is_kept_and_the_402_says_why(
            self, on, task_mod, reason):  # noqa: F811
        user = _user()
        _db.session.commit()
        session_id = _recorded_session(user)
        _lapse(user, reason)

        resp = _client(on, user).post("/api/voice/", json={
            "content": "spoken words", "session_id": session_id})

        assert resp.status_code == 402, resp.get_data(as_text=True)
        data = resp.get_json()
        assert data["error"] == "subscription_required"
        assert data["message"] == TEXTS[reason]
        assert data["code"] == SUBSCRIPTION_CODES[reason]
        entry = Node.query.get(data["user_node_id"])
        assert entry.get_content() == "spoken words"
        assert entry.streaming_transcription is True
        assert TEXTS[reason] in entry.llm_task_warnings
        # The draft moved to the entry (its audio with it).
        assert Draft.query.filter_by(session_id=session_id).count() == 0
        assert _llm_count() == 0
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_a_continued_thread_keeps_the_turn_under_it(self, on, task_mod):  # noqa: F811
        user = _user()
        root = _node(user)
        reply = _reply(root, user)
        _db.session.commit()
        session_id = _recorded_session(user)
        _lapse(user)

        resp = _client(on, user).post("/api/voice/", json={
            "content": "more words", "session_id": session_id,
            "parent_id": reply.id})

        assert resp.status_code == 402, resp.get_data(as_text=True)
        entry = Node.query.get(resp.get_json()["user_node_id"])
        assert entry.parent_id == reply.id
        assert _llm_count() == 1     # the earlier reply only

    @pytest.mark.parametrize("session", ["none", "unknown", "someone else's"])
    def test_a_turn_without_its_recording_writes_nothing(
            self, on, task_mod, session):  # noqa: F811
        # The control: Voice does not start without AI replies.
        user = _user()
        other = _user("bob")
        _db.session.commit()
        body = {"content": "spoken words"}
        if session == "unknown":
            body["session_id"] = str(uuid.uuid4())
        elif session == "someone else's":
            body["session_id"] = _recorded_session(other)
        _lapse(user)
        before = Node.query.count()

        resp = _client(on, user).post("/api/voice/", json=body)

        assert resp.status_code == 402, resp.get_data(as_text=True)
        assert resp.get_json()["error"] == "subscription_required"
        assert "user_node_id" not in resp.get_json()
        assert "code" not in resp.get_json()
        assert Node.query.count() == before

    @pytest.mark.parametrize("payments", [True, False])
    def test_otherwise_the_turn_gets_its_reply(self, app, task_mod, payments):  # noqa: F811
        # The controls: payments on with a live subscription, and off.
        app.config["PAYMENTS_ENABLED"] = payments
        user = _user()
        if payments:
            user.plan = "free"
            user.subscription_level = "standard"
            user.subscription_status = "active"
        _db.session.commit()
        session_id = _recorded_session(user)

        resp = _client(app, user).post("/api/voice/", json={
            "content": "spoken words", "session_id": session_id})

        assert resp.status_code == 202, resp.get_data(as_text=True)
        assert resp.get_json()["llm_node_id"]
        task_mod.generate_llm_response.delay.assert_called_once()

    def test_the_start_is_still_refused(self, on):
        # The control: streaming init answers 402 before any draft.
        user = _lapse(_user())
        resp = _client(on, user).post("/api/drafts/streaming/init", json={
            "label": "Voice", "ai_usage": "chat"})
        assert resp.status_code == 402
        assert resp.get_json()["error"] == "subscription_required"
        assert "code" not in resp.get_json()
        assert Draft.query.count() == 0
