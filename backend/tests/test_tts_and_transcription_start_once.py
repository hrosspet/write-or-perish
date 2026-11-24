"""Speech and transcription batches are queued once per start.

- POST /api/nodes/<id>/tts, /api/profile/<id>/tts and
  /api/external/items/<id>/tts claim the start with one conditional UPDATE
  (utils/audio_storage.claim_tts_start): of requests at the same moment
  one queues the task; the others answer with the run in progress. A
  'failed' run, or none, can be started again; a profile's start whose
  task never ended stops holding it after PROFILE_TTS_START_HELD_FOR.
- The chunk route and /transcribe-remaining move each chunk from 'stored'
  to 'processing' before it is queued (routes/drafts._queue_stored_chunks),
  so a chunk read by two requests at once is queued once.
- A batch task that fails leaves the chunks another run already turned
  into text as they are.

Same harness as test_storage_ids_and_route_scope.py; a second request "at
the same moment" is sent from inside the first one, between its read and
its write.
"""
import sys
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from backend.tests.test_storage_ids_and_route_scope import (  # noqa: F401
    app, data, fake_tasks, _call, _node, _db)
from backend.models import (
    Draft, ExternalItem, Node, NodeTranscriptChunk, UserProfile)
from backend.utils.audio_storage import (
    PROFILE_TTS_START_HELD_FOR, claim_tts_start)

STARTED = "TTS generation started"
IN_PROGRESS = "TTS generation already in progress"


def _profile(user):
    profile = UserProfile(user_id=user.id, generated_by="user",
                          tokens_used=0, ai_usage="chat")
    profile.set_content("ALICE PROFILE")
    _db.session.add(profile)
    _db.session.commit()
    return profile


# ── Speech ───────────────────────────────────────────────────────────────

def test_repeated_profile_speech_requests_queue_one_task(
        app, data, fake_tasks):  # noqa: F811
    profile = _profile(data.alice)
    answers = [_call(app, data.alice, "POST",
                     f"/api/profile/{profile.id}/tts") for _ in range(10)]

    assert [a.status_code for a in answers] == [202] * 10
    assert [a.get_json()["message"] for a in answers] \
        == [STARTED] + [IN_PROGRESS] * 9
    assert fake_tasks.tts.generate_tts_audio_for_profile.delay.call_count \
        == 1
    profile = _db.session.get(UserProfile, profile.id)
    assert profile.tts_task_status == "pending"
    assert profile.tts_task_id == "task-1"


def test_a_profile_start_whose_task_never_ended_can_start_again(
        app, data, fake_tasks):  # noqa: F811
    profile = _profile(data.alice)
    url = f"/api/profile/{profile.id}/tts"
    assert _call(app, data.alice, "POST", url).status_code == 202
    profile = _db.session.get(UserProfile, profile.id)
    profile.tts_task_status = "processing"
    profile.tts_requested_at = datetime.utcnow() - PROFILE_TTS_START_HELD_FOR \
        - timedelta(minutes=1)
    _db.session.commit()

    again = _call(app, data.alice, "POST", url)

    assert again.get_json()["message"] == STARTED
    assert fake_tasks.tts.generate_tts_audio_for_profile.delay.call_count \
        == 2


def test_a_failed_profile_run_can_start_again(app, data, fake_tasks):  # noqa: F811
    profile = _profile(data.alice)
    url = f"/api/profile/{profile.id}/tts"
    assert _call(app, data.alice, "POST", url).status_code == 202
    profile = _db.session.get(UserProfile, profile.id)
    profile.tts_task_status = "failed"
    _db.session.commit()

    assert _call(app, data.alice, "POST", url).get_json()["message"] \
        == STARTED
    assert fake_tasks.tts.generate_tts_audio_for_profile.delay.call_count \
        == 2


def test_two_node_speech_requests_at_once_queue_one_task(
        app, data, fake_tasks, monkeypatch):  # noqa: F811
    import backend.routes.nodes as nodes_mod
    entry = _node(data.alice, "ALICE CHAT ENTRY", ai_usage="chat")
    url = f"/api/nodes/{entry.id}/tts"
    state = {"n": 0, "second": None}

    def key(cfg):
        # After the route read tts_task_status, before its write.
        state["n"] += 1
        if state["n"] == 1:
            state["second"] = _call(app, data.alice, "POST", url)
        return "sk-test"
    monkeypatch.setattr(nodes_mod, "get_openai_chat_key", key)
    data.alice.voice_mode_enabled = True
    _db.session.commit()

    first = _call(app, data.alice, "POST", url)

    assert (first.status_code, state["second"].status_code) == (202, 202)
    assert sorted([first.get_json()["message"],
                   state["second"].get_json()["message"]]) \
        == sorted([STARTED, IN_PROGRESS])
    assert fake_tasks.tts.generate_tts_audio.delay.call_count == 1


def test_a_claim_is_taken_once_and_again_after_a_failed_run(
        app, data):  # noqa: F811
    # On each model: free, then held; free again once failed; never for a
    # row whose speech exists.
    entry = _node(data.alice, "ENTRY", ai_usage="chat")
    profile = _profile(data.alice)
    item = ExternalItem(user_id=data.alice.id, source="twitter",
                        external_id=str(uuid.uuid4())[:32], content="")
    _db.session.add(item)
    _db.session.commit()
    for model, row in ((Node, entry), (UserProfile, profile),
                       (ExternalItem, item)):
        assert claim_tts_start(model, row.id) is True
        assert claim_tts_start(model, row.id) is False
        model.query.filter_by(id=row.id).update({"tts_task_status": "failed"})
        _db.session.commit()
        assert claim_tts_start(model, row.id) is True
        model.query.filter_by(id=row.id).update(
            {"tts_task_status": "completed", "audio_tts_url": "/media/x.mp3"})
        _db.session.commit()
        assert claim_tts_start(model, row.id) is False


# ── Transcription batches ────────────────────────────────────────────────

def _stored_recording(user, n, live=False):
    d = Draft(user_id=user.id, session_id=str(uuid.uuid4()),
              streaming_status="recording", streaming_completed_chunks=0,
              privacy_level="private", ai_usage="chat",
              streaming_heartbeat_at=(datetime.utcnow() if live else
                                      datetime.utcnow()
                                      - timedelta(minutes=5)))
    d.set_content("")
    _db.session.add(d)
    _db.session.flush()
    for i in range(n):
        _db.session.add(NodeTranscriptChunk(
            session_id=d.session_id, chunk_index=i, status="stored"))
    _db.session.commit()
    return d


def _batch_task(monkeypatch, on_delay=None):
    queued = []
    mod = MagicMock()

    def delay(session_id, chunk_indices, **kw):
        queued.append(list(chunk_indices))
        if on_delay is not None and len(queued) == 1:
            on_delay()
        return MagicMock(id=f"task-{len(queued)}")
    mod.transcribe_chunk_batch.delay.side_effect = delay
    monkeypatch.setitem(
        sys.modules, "backend.tasks.streaming_transcription", mod)
    return queued


def test_two_transcribe_remaining_requests_at_once_queue_the_chunks_once(
        app, data, monkeypatch):  # noqa: F811
    d = _stored_recording(data.alice, 3)
    url = f"/api/drafts/streaming/{d.session_id}/transcribe-remaining"
    state = {}
    queued = _batch_task(monkeypatch, on_delay=lambda: state.update(
        second=_call(app, data.alice, "POST", url)))

    first = _call(app, data.alice, "POST", url)

    assert first.status_code == 202
    assert state["second"].status_code == 200
    assert state["second"].get_json()["chunk_count"] == 0
    assert queued == [[0, 1, 2]]
    rows = NodeTranscriptChunk.query.filter_by(session_id=d.session_id)
    assert {(c.status, c.task_id) for c in rows} == {("processing", "task-1")}


def test_a_chunk_upload_at_the_moment_a_batch_is_queued_does_not_queue_it_again(
        app, data, monkeypatch):  # noqa: F811
    from backend.utils.recording_limits import BATCH_CHUNKS
    d = _stored_recording(data.alice, BATCH_CHUNKS - 1, live=True)
    folder = app.audio_root / f"drafts/{data.alice.id}/{d.session_id}"
    folder.mkdir(parents=True, exist_ok=True)
    url = f"/api/drafts/streaming/{d.session_id}/audio-chunk"
    fragment = b"\x00\x00\x01\x10moofdata-no-init-here" * 50

    def chunk(index):
        return _call(app, data.alice, "POST", url, data={
            "chunk": (BytesIO(fragment), "c.webm"),
            "chunk_index": str(index), "mime_type": "audio/webm"},
            content_type="multipart/form-data")
    state = {}
    queued = _batch_task(monkeypatch, on_delay=lambda: state.update(
        second=chunk(BATCH_CHUNKS)))

    first = chunk(BATCH_CHUNKS - 1)

    assert (first.status_code, state["second"].status_code) == (202, 202)
    assert queued == [list(range(BATCH_CHUNKS))]
    last = NodeTranscriptChunk.query.filter_by(
        session_id=d.session_id, chunk_index=BATCH_CHUNKS).one()
    assert last.status == "stored"


def test_a_failed_queue_call_puts_the_chunks_back(
        app, data, monkeypatch):  # noqa: F811
    d = _stored_recording(data.alice, 3)
    mod = MagicMock()
    mod.transcribe_chunk_batch.delay.side_effect = RuntimeError("no broker")
    monkeypatch.setitem(
        sys.modules, "backend.tasks.streaming_transcription", mod)

    with pytest.raises(RuntimeError):
        _call(app, data.alice, "POST",
              f"/api/drafts/streaming/{d.session_id}/transcribe-remaining")

    _db.session.expire_all()
    rows = NodeTranscriptChunk.query.filter_by(session_id=d.session_id)
    assert {c.status for c in rows} == {"stored"}
