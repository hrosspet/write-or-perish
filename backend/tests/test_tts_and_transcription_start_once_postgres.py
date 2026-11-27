"""Speech and transcription batch starts on PostgreSQL, with requests at
the same moment (see test_tts_and_transcription_start_once.py for the
rules).

Runs only against a throwaway Postgres database named by
LOORE_TEST_POSTGRES_URL (it creates and drops every table in it); skipped
otherwise, like test_payments_postgres.py. The threads wait for each other
right before their UPDATE, then go on together.
"""
import os
import sys
import threading
import uuid
from unittest.mock import MagicMock

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from flask import Flask  # noqa: E402

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Draft, Node, NodeTranscriptChunk, User, UserProfile)
from backend.utils.audio_storage import claim_tts_start  # noqa: E402

N = 8


@pytest.fixture
def pg_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = PG_URL
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": N + 4,
                                              "max_overflow": 4}
    app.config["TESTING"] = True
    _db.init_app(app)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        user = User(username="alice", approved=True)
        _db.session.add(user)
        _db.session.commit()
        app.user_id = user.id
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()


def _at_once(app, work, n=N):
    barrier = threading.Barrier(n, timeout=30)
    results = []

    def run():
        with app.app_context():
            barrier.wait()
            results.append(work())
            _db.session.remove()

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.parametrize("model", [Node, UserProfile])
def test_speech_starts_at_the_same_moment_claim_once(pg_app, model):
    with pg_app.app_context():
        if model is Node:
            row = Node(user_id=pg_app.user_id, human_owner_id=pg_app.user_id,
                       node_type="user", privacy_level="private",
                       ai_usage="chat")
        else:
            row = UserProfile(user_id=pg_app.user_id, generated_by="user",
                              tokens_used=0, ai_usage="chat")
        row.set_content("words")
        _db.session.add(row)
        _db.session.commit()
        row_id = row.id

    claims = _at_once(pg_app, lambda: claim_tts_start(model, row_id))

    assert sorted(claims) == [False] * (N - 1) + [True]


def test_stored_chunks_queued_at_the_same_moment_are_queued_once(
        pg_app, monkeypatch):
    with pg_app.app_context():
        sid = str(uuid.uuid4())
        draft = Draft(user_id=pg_app.user_id, session_id=sid,
                      streaming_status="recording")
        draft.set_content("")
        _db.session.add(draft)
        for i in range(25):
            _db.session.add(NodeTranscriptChunk(session_id=sid,
                                                chunk_index=i,
                                                status="stored"))
        _db.session.commit()

    queued = []
    lock = threading.Lock()
    mod = MagicMock()

    def delay(session_id, chunk_indices, **kw):
        with lock:
            queued.append(list(chunk_indices))
            return MagicMock(id=f"task-{len(queued)}")
    mod.transcribe_chunk_batch.delay.side_effect = delay
    monkeypatch.setitem(
        sys.modules, "backend.tasks.streaming_transcription", mod)
    from backend.routes.drafts import _queue_stored_chunks

    def queue_all():
        chunks = NodeTranscriptChunk.query.filter_by(
            session_id=sid, status="stored").order_by(
            NodeTranscriptChunk.chunk_index).all()
        return _queue_stored_chunks(sid, chunks)

    _at_once(pg_app, queue_all)

    indices = sorted(i for batch in queued for i in batch)
    assert indices == list(range(25))
    with pg_app.app_context():
        rows = NodeTranscriptChunk.query.filter_by(session_id=sid).all()
        assert {r.status for r in rows} == {"processing"}
        assert all(r.task_id for r in rows)
