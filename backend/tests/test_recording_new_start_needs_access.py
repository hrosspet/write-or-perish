"""Payments on (#438): audio that counts as a new start of a recording
needs what a recording's init needs, an account that may make a billed
call (utils/recording_limits.audio_refusal).

A new start is the first audio of a recording without audio once its
EMPTY_SESSION_EXPIRES_AFTER has passed, or audio of a recording left
untouched for OPEN_COUNTS_FOR. Without the paid set such audio (a chunk,
a claim) answers 402 ``subscription_required`` and nothing of it is
stored or queued. A recording under way, and an empty one within its
EMPTY_SESSION_EXPIRES_AFTER, go on to the end after access ends (Peter,
2026-11-13), and nothing changes with payments off.

Same harnesses as test_access_ends_mid_recording.py.
"""
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock

from backend.tests.test_recording_ai_usage_none import (  # noqa: F401
    app, stub_tasks, alice, client, _audio_root, _files_under)
from backend.tests.test_spend_cap_recording import FRAGMENT_BYTES
from backend.tests.test_access_ends_mid_recording import on  # noqa: F401
from backend.extensions import db as _db
from backend.models import Draft, NodeTranscriptChunk
from backend.utils import recording_limits
from backend.utils.subscriptions import access_state, ai_replies_allowed


def _trial(user):
    user.plan = "free"
    user.subscription_level = "standard"
    user.subscription_status = "trialing"
    user.subscription_ever_paid = False
    user.subscription_cancelled_by_owner = False
    user.subscription_trial_start = datetime.utcnow() - timedelta(days=13)
    _db.session.commit()
    assert ai_replies_allowed(user)


def _trial_cancelled_and_over(user):
    user.subscription_status = "canceled"
    user.subscription_cancelled_by_owner = True
    _db.session.commit()
    assert not ai_replies_allowed(user)
    assert access_state(user) == "lapsed"


def _chunk(client, session_id, index):  # noqa: F811
    return client.post(
        f"/drafts/streaming/{session_id}/audio-chunk",
        data={"chunk": (BytesIO(FRAGMENT_BYTES), "chunk.webm"),
              "chunk_index": str(index), "mime_type": "audio/webm"},
        content_type="multipart/form-data")


def _start(client, n, age=None):  # noqa: F811
    sessions = []
    for _ in range(n):
        resp = client.post("/drafts/streaming/init", json={"ai_usage": "chat"})
        assert resp.status_code == 201, resp.get_data(as_text=True)
        sessions.append(resp.get_json()["session_id"])
    if age is not None:
        old = datetime.utcnow() - age
        Draft.query.filter(Draft.session_id.in_(sessions)).update(
            {Draft.created_at: old, Draft.streaming_heartbeat_at: old},
            synchronize_session=False)
        _db.session.commit()
    return sessions


def _subscription_required(resp):
    assert resp.status_code == 402, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "subscription_required"


def _session_files(alice, session_id):
    return [p for p in _files_under(
        _audio_root() / f"drafts/{alice.id}/{session_id}")
        if p.name.startswith("chunk_")]


def test_empty_recordings_started_with_access_take_no_audio_days_later(
        on, client, alice, stub_tasks):  # noqa: F811
    _transcription, streaming = stub_tasks
    _trial(alice)
    sessions = _start(client, recording_limits.MAX_EMPTY_STARTS,
                      age=timedelta(days=5))
    _trial_cancelled_and_over(alice)

    batch = recording_limits.BATCH_CHUNKS
    for sid in sessions:
        # Indexes from 5: chunk 0 must carry a WebM header, which the
        # fragment these tests use does not.
        for i in range(5, 5 + batch):
            _subscription_required(_chunk(client, sid, i))
        assert _session_files(alice, sid) == []

    assert NodeTranscriptChunk.query.count() == 0
    streaming.transcribe_chunk_batch.delay.assert_not_called()


def test_a_claim_of_an_empty_recording_days_later_needs_access(
        on, client, alice):  # noqa: F811
    _trial(alice)
    sid = _start(client, 1, age=timedelta(days=5))[0]
    _trial_cancelled_and_over(alice)

    _subscription_required(client.post(
        f"/drafts/streaming/{sid}/claim", json={"device_key": "phone-2"}))
    assert Draft.query.filter_by(
        session_id=sid).one().streaming_device_key != "phone-2"


def test_a_recording_untouched_for_a_day_takes_no_more_audio_without_access(
        on, client, alice):  # noqa: F811
    _trial(alice)
    sid = _start(client, 1)[0]
    assert _chunk(client, sid, 5).status_code == 202
    old = datetime.utcnow() - recording_limits.OPEN_COUNTS_FOR \
        - timedelta(hours=1)
    Draft.query.filter_by(session_id=sid).update(
        {Draft.created_at: old, Draft.streaming_heartbeat_at: old})
    NodeTranscriptChunk.query.update({NodeTranscriptChunk.created_at: old})
    _db.session.commit()
    _trial_cancelled_and_over(alice)

    _subscription_required(_chunk(client, sid, 6))
    assert NodeTranscriptChunk.query.filter_by(session_id=sid).count() == 1


def test_a_recording_under_way_goes_on_after_access_ends(
        on, client, alice):  # noqa: F811
    # Control: audio within the day, and an empty recording within its
    # EMPTY_SESSION_EXPIRES_AFTER, are taken as before.
    _trial(alice)
    under_way, fresh = _start(client, 2)
    assert _chunk(client, under_way, 5).status_code == 202
    _trial_cancelled_and_over(alice)

    assert _chunk(client, under_way, 6).status_code == 202
    assert _chunk(client, fresh, 5).status_code == 202
    assert NodeTranscriptChunk.query.count() == 3


def test_payments_off_an_old_empty_recording_takes_audio_as_before(
        app, client, alice, stub_tasks):  # noqa: F811
    _transcription, streaming = stub_tasks
    streaming.transcribe_chunk_batch.delay.return_value = MagicMock(id="b")
    alice.plan = "free"
    alice.subscription_status = "canceled"
    alice.subscription_cancelled_by_owner = True
    _db.session.commit()
    sid = _start(client, 1, age=timedelta(days=5))[0]

    assert _chunk(client, sid, 5).status_code == 202
    assert NodeTranscriptChunk.query.filter_by(session_id=sid).count() == 1
