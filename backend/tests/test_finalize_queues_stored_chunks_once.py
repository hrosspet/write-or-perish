"""The finalize task queues a recording's remaining stored chunks through
the same claim as the chunk route and /transcribe-remaining
(tasks/streaming_transcription.queue_batches,
utils/audio_storage.claim_and_queue_chunks): a chunk one of them queued
after the task read it is not queued again.

Same harness as test_spend_cap_recording.py (the task module with the
celery glue stubbed).
"""
import uuid
from unittest.mock import MagicMock

from backend.tests.test_spend_cap_recording import st  # noqa: F401
from backend.extensions import db as _db
from backend.models import Draft, NodeTranscriptChunk
from backend.utils.audio_storage import claim_and_queue_chunks


def _recording(st, n):  # noqa: F811
    sid = str(uuid.uuid4())
    draft = Draft(user_id=st._test_user.id, session_id=sid,
                  streaming_status="finalizing")
    draft.set_content("")
    _db.session.add(draft)
    for i in range(n):
        _db.session.add(NodeTranscriptChunk(session_id=sid, chunk_index=i,
                                            status="stored"))
    _db.session.commit()
    return sid


def _batch_task(monkeypatch, st):  # noqa: F811
    queued = []
    task = MagicMock()

    def delay(session_id, chunk_indices, **kw):
        queued.append(list(chunk_indices))
        return MagicMock(id=f"task-{len(queued)}")
    task.delay.side_effect = delay
    monkeypatch.setattr(st, "transcribe_chunk_batch", task)
    return task, queued


def _stored(sid):
    return NodeTranscriptChunk.query.filter_by(
        session_id=sid, status="stored").order_by(
        NodeTranscriptChunk.chunk_index).all()


def test_chunks_a_route_queued_after_the_finalize_read_are_not_queued_again(
        st, monkeypatch):  # noqa: F811
    sid = _recording(st, 3)
    task, queued = _batch_task(monkeypatch, st)
    # The finalize task read its rows ...
    rows = _stored(sid)
    # ... a route (the chunk upload, /transcribe-remaining) claims and
    # queues them ...
    claim_and_queue_chunks(sid, _stored(sid), task)
    # ... and the task's own queueing finds them taken.
    assert st.queue_batches(sid, rows) == []

    assert queued == [[0, 1, 2]]
    assert {(c.status, c.task_id) for c in NodeTranscriptChunk.query
            .filter_by(session_id=sid)} == {("processing", "task-1")}


def test_the_finalize_task_queues_what_is_still_stored(st, monkeypatch):  # noqa: F811
    # Control: 25 stored chunks, two batches, each chunk once.
    sid = _recording(st, 25)
    _task, queued = _batch_task(monkeypatch, st)

    assert st.queue_batches(sid, _stored(sid)) == ["task-1", "task-2"]

    assert queued == [list(range(20)), list(range(20, 25))]
    assert _stored(sid) == []
