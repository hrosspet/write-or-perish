"""Each recording device has its own key (#388).

A recording session left behind by its device can be continued on
another device ("Continue recording"). The first device may still be
alive (an iPhone with a paused recording in the background, a laptop
closed mid-recording). Both then uploaded chunks with the same indexes,
and the server kept whichever copy arrived first: part of the recording
was lost, and neither device said so.

Covers:
- init stores the device's key; a client older than #388 sends none.
- a claim makes the continuing device the writer, stamps the session,
  and names the next chunk index (one past the highest stored chunk);
  it is refused while another device records, or after the session ended.
- chunks, finalize, release and the transcription stream sent with the
  old key get 409 / an event `session_taken_over`; the old device's
  chunk never replaces the new device's chunk with the same index.
- requests without a key behave as before.
"""

import io
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-test-")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    User, Draft, NodeTranscriptChunk,
)
import backend.models as _real_backend_models    # noqa: E402

KEY_A = "device-a-0123456789"   # the device that started the recording
KEY_B = "device-b-9876543210"   # the device that continues it

LIVE = timedelta(seconds=5)       # stamped 5 s ago: live
STALE = timedelta(minutes=2)      # stamped 2 min ago: left behind


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.drafts import drafts_bp
    from backend.routes.sse import sse_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(sse_bp, url_prefix="/api/sse")

    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def finalize_task():
    """Stand-in for the lazily imported Celery task module."""
    key = "backend.tasks.streaming_transcription"
    saved = sys.modules.get(key)
    mod = MagicMock()
    mod.finalize_draft_streaming.delay.return_value = MagicMock(id="task-1")
    sys.modules[key] = mod
    yield mod
    if saved is None:
        del sys.modules[key]
    else:
        sys.modules[key] = saved


def _setup(app):
    client = app.test_client()
    alice = User(username="alice", approved=True, plan="alpha")
    _db.session.add(alice)
    _db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(alice.id)
        sess["_fresh"] = True
    return client, alice


def _make_session(user, key=KEY_A, heartbeat_age=STALE, status="recording",
                  chunk_indexes=(0, 1)):
    d = Draft(
        user_id=user.id,
        session_id=str(uuid.uuid4()),
        streaming_status=status,
        streaming_completed_chunks=0,
        privacy_level="private",
        ai_usage="chat",
        streaming_heartbeat_at=(
            None if heartbeat_age is None
            else datetime.utcnow() - heartbeat_age
        ),
        streaming_device_key=key,
    )
    d.set_content("words so far")
    _db.session.add(d)
    _db.session.flush()
    for i in chunk_indexes:
        _db.session.add(NodeTranscriptChunk(
            session_id=d.session_id, chunk_index=i, status="stored",
        ))
    _db.session.commit()
    _chunk_dir(user, d)
    return d


def _chunk_dir(user, draft):
    from backend.routes import drafts as drafts_module
    path = (drafts_module.AUDIO_STORAGE_ROOT
            / f"drafts/{user.id}/{draft.session_id}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _post_chunk(client, draft, index, key=None, payload=b"\x00" * 64):
    data = {
        "chunk": (io.BytesIO(payload), f"chunk_{index}.webm"),
        "chunk_index": str(index),
        "mime_type": "audio/webm",
    }
    if key is not None:
        data["device_key"] = key
    return client.post(
        f"/api/drafts/streaming/{draft.session_id}/audio-chunk",
        data=data,
        content_type="multipart/form-data",
    )


def _claim(client, draft, key=KEY_B):
    return client.post(
        f"/api/drafts/streaming/{draft.session_id}/claim",
        json={"device_key": key} if key is not None else {},
    )


def _fresh(draft):
    _db.session.expire_all()
    return Draft.query.get(draft.id)


def _events(resp):
    """[(event, data)] of an SSE body."""
    out = []
    for block in resp.get_data(as_text=True).strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines.get("event"), json.loads(lines["data"])))
    return out


class TestInit:
    def test_init_stores_the_device_key(self, app):
        client, _ = _setup(app)
        resp = client.post("/api/drafts/streaming/init",
                           json={"device_key": KEY_A})
        assert resp.status_code == 201
        draft = Draft.query.filter_by(
            session_id=resp.get_json()["session_id"]).one()
        assert draft.streaming_device_key == KEY_A

    def test_an_older_client_sends_no_key(self, app):
        client, _ = _setup(app)
        resp = client.post("/api/drafts/streaming/init", json={})
        assert resp.status_code == 201
        draft = Draft.query.filter_by(
            session_id=resp.get_json()["session_id"]).one()
        assert draft.streaming_device_key is None

    @pytest.mark.parametrize("bad", ["", "x" * 65, 12345])
    def test_an_invalid_key_is_refused(self, app, bad):
        client, _ = _setup(app)
        resp = client.post("/api/drafts/streaming/init",
                           json={"device_key": bad})
        assert resp.status_code == 400
        assert Draft.query.count() == 0


class TestClaim:
    def test_claim_makes_the_device_the_writer(self, app):
        client, alice = _setup(app)
        d = _make_session(alice)
        updated_before = d.updated_at

        resp = _claim(client, d)

        assert resp.status_code == 200
        assert resp.get_json()["next_chunk_index"] == 2
        fresh = _fresh(d)
        assert fresh.streaming_device_key == KEY_B
        # Live again: no other view offers it for recovery.
        assert fresh.streaming_heartbeat_at > datetime.utcnow() - LIVE
        assert fresh.updated_at == updated_before
        assert client.get("/api/drafts/interrupted").get_json() == []

    def test_next_index_skips_a_hole(self, app):
        # Chunk 1 never arrived: the count (2) is an index already taken.
        client, alice = _setup(app)
        d = _make_session(alice, chunk_indexes=(0, 2))
        assert _claim(client, d).get_json()["next_chunk_index"] == 3

    def test_claim_of_a_session_with_no_chunk_starts_at_zero(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, chunk_indexes=())
        assert _claim(client, d).get_json()["next_chunk_index"] == 0

    def test_a_released_session_can_be_claimed(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=None)
        assert _claim(client, d).status_code == 200

    def test_a_session_an_older_client_started_can_be_claimed(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=None)
        assert _claim(client, d).status_code == 200
        assert _fresh(d).streaming_device_key == KEY_B

    def test_refused_while_another_device_records(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE)

        resp = _claim(client, d)

        assert resp.status_code == 409
        assert resp.get_json()["code"] == "session_live"
        assert _fresh(d).streaming_device_key == KEY_A

    def test_the_writer_may_claim_again(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE)
        assert _claim(client, d, key=KEY_A).status_code == 200

    def test_refused_once_the_session_ended(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, status="finalizing")
        resp = _claim(client, d)
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "session_not_active"

    def test_missing_session(self, app):
        client, _ = _setup(app)
        ghost = Draft(session_id=str(uuid.uuid4()))
        resp = _claim(client, ghost)
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "session_not_found"

    def test_another_users_session_is_not_found(self, app):
        client, _ = _setup(app)
        bob = User(username="bob", approved=True, plan="alpha")
        _db.session.add(bob)
        _db.session.commit()
        d = _make_session(bob)
        assert _claim(client, d).status_code == 404
        assert _fresh(d).streaming_device_key == KEY_A

    def test_a_claim_needs_a_key(self, app):
        client, alice = _setup(app)
        d = _make_session(alice)
        assert _claim(client, d, key=None).status_code == 400
        assert _fresh(d).streaming_device_key == KEY_A


class TestChunksAfterATakeover:
    def test_the_issue_two_devices_one_session(self, app):
        # A records chunks 0 and 1, then goes quiet (paused in the
        # background). B continues the recording; A comes back.
        client, alice = _setup(app)
        d = _make_session(alice)
        start = _claim(client, d).get_json()["next_chunk_index"]
        assert start == 2

        assert _post_chunk(client, d, 2, key=KEY_B,
                           payload=b"B" * 64).status_code == 202
        late = _post_chunk(client, d, 2, key=KEY_A, payload=b"A" * 64)

        assert late.status_code == 409
        assert late.get_json()["code"] == "session_taken_over"
        # B's chunk is the one stored, on disk too.
        chunk_file = _chunk_dir(alice, d) / "chunk_0002.webm"
        assert chunk_file.read_bytes() == b"B" * 64
        # A's next chunk index is refused as well, and stores nothing.
        assert _post_chunk(client, d, 3, key=KEY_A).status_code == 409
        assert NodeTranscriptChunk.query.filter_by(
            session_id=d.session_id).count() == 3

    def test_a_refused_chunk_does_not_stamp_the_session(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=None)

        assert _post_chunk(client, d, 2, key=KEY_A).status_code == 409

        assert _fresh(d).streaming_heartbeat_at is None
        assert not [p for p in _chunk_dir(alice, d).iterdir()
                    if p.name.startswith("upload-")]

    def test_the_writers_chunks_are_stored(self, app):
        client, alice = _setup(app)
        d = _make_session(alice)
        assert _post_chunk(client, d, 2, key=KEY_A).status_code == 202

    def test_a_chunk_without_a_key_is_stored_as_before(self, app):
        # A client older than #388 sends no key.
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B)
        assert _post_chunk(client, d, 2).status_code == 202

    def test_a_chunk_into_a_session_without_a_writer_is_stored(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=None)
        assert _post_chunk(client, d, 2, key=KEY_A).status_code == 202

    def test_a_claim_during_the_upload_refuses_the_chunk(self, app, monkeypatch):
        # The claim lands after the first check: the check under the row
        # lock refuses the chunk before its file is put in place.
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B)
        from backend.routes import drafts as drafts_module
        monkeypatch.setattr(drafts_module, "is_taken_over", lambda *a: False)

        resp = _post_chunk(client, d, 2, key=KEY_A)

        assert resp.status_code == 409
        assert resp.get_json()["code"] == "session_taken_over"
        assert NodeTranscriptChunk.query.filter_by(
            session_id=d.session_id, chunk_index=2).count() == 0
        assert not (_chunk_dir(alice, d) / "chunk_0002.webm").exists()
        assert not [p for p in _chunk_dir(alice, d).iterdir()
                    if p.name.startswith("upload-")]

    def test_a_busy_row_lock_stores_nothing(self, app, monkeypatch):
        # The lock wait ran out (lock_timeout): 503, which the recorders
        # retry, and no file is left or put in place.
        client, alice = _setup(app)
        d = _make_session(alice)
        from backend.utils import recording_device

        def busy(_draft_id):
            raise recording_device.SessionBusy()
        monkeypatch.setattr(recording_device, "lock_writer_key", busy)

        resp = _post_chunk(client, d, 2, key=KEY_A)

        assert resp.status_code == 503
        assert resp.get_json()["code"] == "session_busy"
        assert NodeTranscriptChunk.query.filter_by(
            session_id=d.session_id, chunk_index=2).count() == 0
        assert list(_chunk_dir(alice, d).iterdir()) == []


class TestFinalize:
    def _finalize(self, client, d, key):
        body = {"total_chunks": 2}
        if key is not None:
            body["device_key"] = key
        return client.post(
            f"/api/drafts/streaming/{d.session_id}/finalize", json=body)

    def test_the_old_device_cannot_finalize(self, app, finalize_task):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)

        resp = self._finalize(client, d, KEY_A)

        assert resp.status_code == 409
        assert resp.get_json()["code"] == "session_taken_over"
        assert _fresh(d).streaming_status == "recording"
        finalize_task.finalize_draft_streaming.delay.assert_not_called()

    def test_the_writer_finalizes(self, app, finalize_task):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)
        assert self._finalize(client, d, KEY_B).status_code == 202
        assert _fresh(d).streaming_status == "finalizing"

    @pytest.mark.parametrize("key", [KEY_B, None])
    def test_two_finalizes_queue_one_task(self, app, finalize_task, key):
        # A retried finalize (or one from each of two devices): one task,
        # one entry, one reply.
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)

        first = self._finalize(client, d, key)
        second = self._finalize(client, d, key)

        assert first.status_code == 202
        assert second.status_code == 400
        assert "not in recording state" in second.get_json()["error"]
        assert finalize_task.finalize_draft_streaming.delay.call_count == 1

    @pytest.mark.parametrize("key", [KEY_B, None])
    def test_a_finalize_overtaken_by_another_queues_nothing(
            self, app, finalize_task, monkeypatch, key):
        # The other finalize moves the session on after this one read it
        # as 'recording': only one of them may queue the task.
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)
        from backend.routes import drafts as drafts_module
        real_parent_error = drafts_module._live_parent_error

        def other_finalize_commits(parent_id):
            Draft.query.filter_by(id=d.id).update(
                {"streaming_status": "finalizing"}, synchronize_session=False)
            _db.session.commit()
            return real_parent_error(parent_id)
        monkeypatch.setattr(drafts_module, "_live_parent_error",
                            other_finalize_commits)

        resp = self._finalize(client, d, key)

        assert resp.status_code == 400
        assert "not in recording state" in resp.get_json()["error"]
        finalize_task.finalize_draft_streaming.delay.assert_not_called()

    def test_a_finalize_without_a_key_works_as_before(self, app, finalize_task):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)
        assert self._finalize(client, d, None).status_code == 202


class TestRelease:
    def _release(self, client, d, key):
        url = f"/api/drafts/streaming/{d.session_id}/release"
        if key is not None:
            url += f"?device_key={key}"
        return client.post(url)

    def test_the_old_device_cannot_release_the_new_ones_recording(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)

        resp = self._release(client, d, KEY_A)

        assert resp.status_code == 409
        assert _fresh(d).streaming_heartbeat_at is not None
        assert client.get("/api/drafts/interrupted").get_json() == []

    def test_the_writer_releases(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)
        assert self._release(client, d, KEY_B).get_json() == {"released": True}
        assert _fresh(d).streaming_heartbeat_at is None

    def test_a_release_without_a_key_works_as_before(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=LIVE)
        assert self._release(client, d, None).get_json() == {"released": True}


class TestTranscriptionStream:
    def _stream(self, client, d, key):
        url = f"/api/sse/drafts/{d.session_id}/transcription-stream"
        if key is not None:
            url += f"?device_key={key}"
        return client.get(url)

    def test_the_old_device_is_told_and_does_not_stamp(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, heartbeat_age=None)

        events = _events(self._stream(client, d, KEY_A))

        assert [e for e, _ in events] == ["session_taken_over"]
        assert events[0][1]["code"] == "session_taken_over"
        assert _fresh(d).streaming_heartbeat_at is None

    def test_the_old_device_does_not_take_the_completion(self, app):
        # all_complete deletes a finished turn's draft: the old device's
        # stream must not take it from the device that finished it.
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, status="completed")

        events = _events(self._stream(client, d, KEY_A))

        assert [e for e, _ in events] == ["session_taken_over"]
        assert _fresh(d) is not None

    @pytest.mark.parametrize("key", [KEY_B, None])
    def test_the_writer_and_an_older_client_get_the_completion(self, app, key):
        client, alice = _setup(app)
        d = _make_session(alice, key=KEY_B, status="completed")
        events = _events(self._stream(client, d, key))
        assert [e for e, _ in events][-1] == "all_complete"
