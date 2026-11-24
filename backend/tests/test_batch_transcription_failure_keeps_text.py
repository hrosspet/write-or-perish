"""A transcription batch task that fails marks its chunks failed, but not
a chunk another run of the same chunks already turned into text
(BatchTranscriptionTask.on_failure, as the task's own except block): the
finalize builds the entry from the 'completed' chunks, so that text stays
in it.

Same harness as test_spend_cap_recording.py (the task module with the
celery glue stubbed).
"""
import sys
import uuid
from unittest.mock import MagicMock

import pytest

from backend.tests.test_spend_cap_recording import st  # noqa: F401
from backend.extensions import db as _db
from backend.models import Draft, NodeTranscriptChunk


@pytest.fixture
def plain_task_base(monkeypatch):
    """The task module imported with a plain class as celery's Task, so
    its task classes keep their own methods."""
    celery_stub = MagicMock()
    celery_stub.Task = object
    monkeypatch.setitem(sys.modules, "celery", celery_stub)


def test_a_failed_batch_keeps_the_chunks_already_transcribed(
        plain_task_base, st):  # noqa: F811
    sid = str(uuid.uuid4())
    draft = Draft(user_id=st._test_user.id, session_id=sid,
                  streaming_status="finalizing")
    draft.set_content("")
    _db.session.add(draft)
    done = NodeTranscriptChunk(session_id=sid, chunk_index=0,
                               status="completed")
    done.set_text("spoken words")
    waiting = NodeTranscriptChunk(session_id=sid, chunk_index=1,
                                  status="processing")
    _db.session.add_all([done, waiting])
    _db.session.commit()

    st.BatchTranscriptionTask.on_failure(
        None, FileNotFoundError("chunk file gone"), "task-2", (),
        {"session_id": sid, "chunk_indices": [0, 1]}, None)

    _db.session.expire_all()
    rows = {c.chunk_index: c for c in NodeTranscriptChunk.query.filter_by(
        session_id=sid)}
    assert rows[0].status == "completed"
    assert rows[0].get_text() == "spoken words"
    assert rows[1].status == "failed"
