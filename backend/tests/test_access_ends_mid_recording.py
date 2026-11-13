"""Payments on (#438): a recording in progress when the account's access
ends is kept and transcribed; only the AI reply is skipped.

Peter, 2026-11-13: "User's recorded message should be stored on Loore's
servers, possibly even transcribed (transcription is cheap). Just further
AI use should be blocked." And, on the scope: "Voice mode shouldn't start
with a subscription expired. I was talking about the edge case when
subscription expires during user's voice recording."

So an account without AI replies starts nothing (402 before anything is
stored, the controls below), while a dictation, an upload or a recording
accepted while it had them goes on to the end whatever happens to the
subscription meanwhile: its chunks are stored and transcribed, it is
finalized, and it is saved as an entry with its transcript. The Voice
side (the reply skipped, its message, the spoken sentence) is in
test_access_ends_mid_voice_turn.py.

Most tests here pin what already held when this was written (no route or
task after the start asks for the subscription); each says so. Same
harnesses as test_recording_ai_usage_none.py (routes),
test_spend_cap_recording.py (the chunk transcription tasks) and
test_textmode_upload_agentic.py (the upload transcription task).
"""
import json
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from backend.tests.test_recording_ai_usage_none import (  # noqa: F401
    app, stub_tasks, alice, client, _audio_root, _files_under)
from backend.tests.test_spend_cap_recording import (  # noqa: F401
    st, _recording, FRAGMENT_BYTES)
from backend.tests.test_textmode_upload_agentic import tr  # noqa: F401
from backend.extensions import db as _db
from backend.models import Draft, Node, NodeTranscriptChunk


def _active(user):
    """A live subscription: the paid set, AI replies."""
    user.plan = "free"
    user.subscription_level = "standard"
    user.subscription_status = "active"
    user.subscription_ever_paid = True
    _db.session.commit()
    return user


def _lapse(user, reason="ended"):
    """Access ends, the way each reason ends it (the account is lapsed,
    let in, with no AI replies)."""
    now = datetime.utcnow()
    user.plan = "free"
    user.subscription_level = "standard"
    user.subscription_ever_paid = True
    if reason == "ended":
        user.subscription_status = "ended"
    elif reason == "past_due":
        user.subscription_status = "past_due"
    elif reason == "free_month":
        user.subscription_status = "none"
        user.subscription_ever_paid = False
        user.alpha_free_granted_at = now - timedelta(days=40)
        user.alpha_free_start = now - timedelta(days=40)
        user.alpha_free_until = now - timedelta(days=10)
    else:   # withdrawn after a dispute: "active" at Stripe, gives nothing
        user.subscription_status = "active"
        user.subscription_revoked = "dispute"
    _db.session.commit()
    from backend.utils.subscriptions import ai_replies_allowed, access_state
    assert not ai_replies_allowed(user)
    assert access_state(user) == "lapsed"
    return user


@pytest.fixture
def on(app):  # noqa: F811
    app.config["PAYMENTS_ENABLED"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"}}
    return app


def _subscription_required(resp):
    assert resp.status_code == 402, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "subscription_required"


# ── Controls: nothing starts without AI replies (unchanged) ─────────────

class TestNothingStartsWithoutAccess:
    @pytest.mark.parametrize("label", [None, "Voice"])
    def test_a_dictation_or_voice_recording(self, on, client, alice, label):
        _lapse(alice)
        body = {"ai_usage": "chat"}
        if label:
            body["label"] = label
        _subscription_required(client.post("/drafts/streaming/init",
                                           json=body))
        assert Draft.query.count() == 0

    def test_a_chunked_upload(self, on, client, alice):
        _lapse(alice)
        _subscription_required(client.post("/nodes/upload/init", json={
            "filename": "talk.m4a", "filesize": 10, "total_chunks": 1,
            "upload_id": "up-1", "ai_usage": "chat"}))
        assert Node.query.count() == 0
        assert not (_audio_root() / f"chunks/{alice.id}/up-1").exists()

    def test_a_multipart_upload(self, on, client, alice, stub_tasks):
        _lapse(alice)
        _subscription_required(client.post("/nodes/", data={
            "audio_file": (BytesIO(b"fake audio"), "talk.m4a"),
            "ai_usage": "chat"}, content_type="multipart/form-data"))
        assert Node.query.count() == 0
        stub_tasks[0].transcribe_audio.delay.assert_not_called()

    def test_a_legacy_node_recording(self, on, client, alice):
        _lapse(alice)
        _subscription_required(client.post("/nodes/streaming/init",
                                           json={"ai_usage": "chat"}))
        assert Node.query.count() == 0

    def test_payments_off_a_free_account_records_as_before(
            self, app, client, alice):  # noqa: F811
        alice.plan = "free"
        alice.subscription_status = "ended"
        _db.session.commit()
        resp = client.post("/drafts/streaming/init", json={"ai_usage": "chat"})
        assert resp.status_code == 201, resp.get_data(as_text=True)


# ── A recording that started with access goes on to the end ─────────────

class TestARecordingInProgressGoesOn:
    def _start(self, client, alice, label=None):
        _active(alice)
        body = {"ai_usage": "chat"}
        if label:
            body["label"] = label
        resp = client.post("/drafts/streaming/init", json=body)
        assert resp.status_code == 201, resp.get_data(as_text=True)
        return resp.get_json()["session_id"]

    def _chunk(self, client, session_id, index=5):
        return client.post(
            f"/drafts/streaming/{session_id}/audio-chunk",
            data={"chunk": (BytesIO(FRAGMENT_BYTES), "chunk.webm"),
                  "chunk_index": str(index), "mime_type": "audio/webm"},
            content_type="multipart/form-data")

    @pytest.mark.parametrize("label", [None, "Voice"])
    @pytest.mark.parametrize("reason", ["ended", "past_due", "free_month",
                                        "none"])
    def test_its_chunks_are_stored_and_it_is_finalized(
            self, on, client, alice, stub_tasks, label, reason):
        # Pins what held already: the chunk and finalize routes never ask
        # for the subscription.
        _transcription, streaming = stub_tasks
        streaming.finalize_draft_streaming.delay.return_value = MagicMock(
            id="fin")
        session_id = self._start(client, alice, label)
        _lapse(alice, reason)

        resp = self._chunk(client, session_id)
        assert resp.status_code == 202, resp.get_data(as_text=True)
        chunk = NodeTranscriptChunk.query.filter_by(
            session_id=session_id, chunk_index=5).one()
        assert chunk.status == "stored"
        audio = _files_under(_audio_root() / f"drafts/{alice.id}/{session_id}")
        assert audio, "the chunk's audio is on disk"

        body = {"total_chunks": 6}
        if label:
            body.update(label=label, model="gpt-5")
        resp = client.post(f"/drafts/streaming/{session_id}/finalize",
                           json=body)
        assert resp.status_code == 202, resp.get_data(as_text=True)
        streaming.finalize_draft_streaming.delay.assert_called_once()
        assert Draft.query.filter_by(
            session_id=session_id).one().streaming_status == "finalizing"

    def test_a_dictation_is_saved_as_an_entry_without_its_reply(
            self, on, client, alice):
        # Pins what held already: save-as-node keeps the writing and says
        # why no reply follows (subscription_required fields).
        session_id = self._start(client, alice)
        draft = Draft.query.filter_by(session_id=session_id).one()
        draft.streaming_status = "completed"
        draft.set_content("dictated words")
        _db.session.commit()
        _lapse(alice)

        resp = client.post(
            f"/drafts/streaming/{session_id}/save-as-node",
            json={"auto_generate": True, "model": "gpt-5",
                  "ai_usage": "chat"})

        assert resp.status_code in (200, 201), resp.get_data(as_text=True)
        data = resp.get_json()
        assert data["subscription_required"] is True
        assert "subscription has ended" in data["subscription_message"]
        assert "llm_node_id" not in data
        entry = Node.query.get(data["id"])
        assert entry.get_content() == "dictated words"
        assert Node.query.filter_by(node_type="llm").count() == 0


class TestAnUploadInProgressGoesOn:
    def _init(self, client, upload_id="up-1", **extra):
        resp = client.post("/nodes/upload/init", json={
            "filename": "talk.m4a", "filesize": 10, "total_chunks": 1,
            "upload_id": upload_id, "ai_usage": "chat", **extra})
        assert resp.status_code == 201, resp.get_data(as_text=True)
        return resp.get_json()["node_id"]

    @pytest.mark.parametrize("reply", [False, True])
    def test_chunk_and_finalize_store_and_queue_it(
            self, on, client, alice, stub_tasks, reply):
        # Pins what held already: neither route asks for the
        # subscription; the reply it asked for is skipped by the
        # transcription task (TestTheUploadIsTranscribed below).
        transcription, _streaming = stub_tasks
        _active(alice)
        extra = {"auto_generate": True, "model": "gpt-5"} if reply else {}
        node_id = self._init(client, **extra)
        _lapse(alice)

        resp = client.post("/nodes/upload/chunk", data={
            "chunk": (BytesIO(b"0123456789"), "blob"),
            "chunk_index": "0", "upload_id": "up-1",
            "node_id": str(node_id)}, content_type="multipart/form-data")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        resp = client.post("/nodes/upload/finalize", json={
            "upload_id": "up-1", "node_id": node_id})

        assert resp.status_code == 200, resp.get_data(as_text=True)
        node = Node.query.get(node_id)
        assert node.audio_original_url
        assert node.transcription_status == "pending"
        audio = _files_under(_audio_root() / f"user/{alice.id}/node/{node_id}")
        assert [p.read_bytes() for p in audio] == [b"0123456789"]
        transcription.transcribe_audio.delay.assert_called_once()
        kwargs = transcription.transcribe_audio.delay.call_args.kwargs
        assert kwargs == ({"auto_reply_model": "gpt-5"} if reply else {})


# ── The transcription tasks ──────────────────────────────────────────────

def _lapsed_task_user(st):  # noqa: F811
    user = st._test_user
    user.spend_blocked_month = None     # the fixture's user starts capped
    _db.session.commit()
    st.flask_app.config["PAYMENTS_ENABLED"] = True
    return _lapse(user)


class TestTheRecordingIsTranscribed:
    def test_a_chunk_is_transcribed(self, st):  # noqa: F811
        # Pins what held already: the chunk tasks never ask for it.
        user = _lapsed_task_user(st)
        _recording(user, "sess-1", [3], st._test_root)
        path = st._test_root / f"drafts/{user.id}/sess-1/chunk_0003.webm"

        st.transcribe_draft_chunk(MagicMock(), "sess-1", 3, str(path))

        chunk = NodeTranscriptChunk.query.filter_by(
            session_id="sess-1", chunk_index=3).one()
        assert chunk.status == "completed"
        assert chunk.get_text() == "spoken words"

    def test_the_remaining_chunks_are_transcribed(self, st):  # noqa: F811
        user = _lapsed_task_user(st)
        _recording(user, "sess-2", [0, 1, 2], st._test_root)

        st.transcribe_chunk_batch(MagicMock(), "sess-2", [0, 1, 2])

        chunks = NodeTranscriptChunk.query.filter_by(session_id="sess-2").all()
        assert chunks and all(c.status == "completed" for c in chunks)

    def test_a_dictation_is_finalized_with_its_text(self, st):  # noqa: F811
        user = _lapsed_task_user(st)
        draft = _recording(user, "sess-3", [], st._test_root)
        chunk = NodeTranscriptChunk(session_id="sess-3", chunk_index=0,
                                    status="completed",
                                    completed_at=datetime.utcnow())
        chunk.set_text("dictated words")
        _db.session.add(chunk)
        _db.session.commit()

        result = st.finalize_draft_streaming(MagicMock(), "sess-3", 1)

        assert result["status"] == "completed"
        _db.session.refresh(draft)
        assert draft.streaming_status == "completed"
        assert "dictated words" in draft.get_content()


class TestTheUploadIsTranscribed:
    def test_kept_with_its_text_and_told_why_there_is_no_reply(self, tr):  # noqa: F811
        # Pins what held already: the access ends while the file is being
        # transcribed; the transcript is written, the reply it asked for
        # is skipped, and the entry says why.
        from backend.models import User
        user_id, entry = tr._t["user"].id, tr._t["entry"]
        tr.flask_app.config["PAYMENTS_ENABLED"] = True

        def transcribe_then_lapse(**kwargs):
            user = _db.session.get(User, user_id)
            user.plan = "free"
            user.subscription_level = "standard"
            user.subscription_status = "ended"
            user.subscription_ever_paid = True
            return type("R", (), {"text": "uploaded words"})()
        tr.OpenAI.return_value.audio.transcriptions.create.side_effect = (
            transcribe_then_lapse)

        tr.transcribe_audio(MagicMock(), entry.id, tr._t["audio"],
                            "talk.m4a", auto_reply_model="gpt-5")

        _db.session.refresh(entry)
        assert entry.transcription_status == "completed"
        assert entry.get_content() == "uploaded words"
        assert Node.query.filter_by(node_type="llm").count() == 0
        tr._t["llm"].generate_llm_response.delay.assert_not_called()
        assert json.loads(entry.llm_task_warnings) == [
            "Your subscription has ended, so Loore doesn't reply. "
            "Your writing is saved."]
