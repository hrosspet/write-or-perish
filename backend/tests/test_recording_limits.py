"""Limits on recording uploads and on transcription per recording
(utils/recording_limits).

Streaming recordings (``/api/drafts/streaming/<session>/audio-chunk``, and
the older ``/api/nodes/<id>/audio-chunk``):
- a chunk larger than any the apps send, a chunk index no recording needs,
  and a chunk that takes the recording past the bytes one recording may
  store are refused; nothing of them is stored and the chunks before stay;
- a copy of a stored chunk and a re-sent failed chunk are not counted
  twice;
- the finalize takes only a chunk count a recording can have;
- a recording accepted at its start is transcribed to its end, also when
  the monthly usage limit is reached meanwhile (#341), up to
  MAX_TRANSCRIBED_SECONDS: the audio past it is stored but not
  transcribed, the text ends with a note, and the transcription stream
  tells the apps why;
- a new recording is refused while MAX_RECORDINGS_IN_PROGRESS of the
  account's recordings are in progress (one left behind stops counting),
  or while it holds MAX_UNFINISHED_RECORDINGS unfinished ones (one quiet
  for a day, or without audio, does not count);
- a recording that stopped keeping going and takes audio again (a chunk,
  a claim, its transcription stream) is checked as a start is; the real
  flows (an app killed mid-recording, relaunched, continued or discarded)
  still work;
- starts at the same moment are counted again under the account's lock;
- a recording of realistic size and length passes untouched (controls).

Chunked file uploads (``/api/nodes/upload/...``): the init's size and piece
count, each piece's index and size, and the finalize's total are held to
what the init announced; a refused finalize deletes the pieces. A new
upload is refused while MAX_UPLOADS_IN_PROGRESS of the account's uploads
wait for their transcript; an upload older than UPLOAD_COUNTS_FOR takes
no more pieces, and one the app reports failed stops counting.

The data download (``/api/export/threads``): one at a time per account and
EXPORTS_PER_HOUR an hour.

Each test either fails on main (where nothing of this is checked) or is a
control, marked so.
"""
import io
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ.setdefault(
    "AUDIO_STORAGE_PATH", tempfile.mkdtemp(prefix="wop-audio-limits-test-"))

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

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Draft, Node, NodeTranscriptChunk, User,
)
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import recording_limits as limits  # noqa: E402
from backend.utils.spend import current_month  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ios_fmp4"
# The sizes measured from the apps (utils/recording_limits): a 15 s web
# chunk, and the largest web chunk seen (two slices in one).
WEB_CHUNK_BYTES = 242_498
LARGEST_APP_CHUNK_BYTES = 485_787
# A chunk past 0 without its own stream header (see test_resume_subsessions).
FRAGMENT = b"\x00\x00\x01\x10moofdata-no-init-here"


def _payload(size):
    return (FRAGMENT * (size // len(FRAGMENT) + 1))[:size]


def _cap(user):
    user.spend_blocked_month = current_month(datetime.utcnow())
    _db.session.commit()


# ── Routes ───────────────────────────────────────────────────────────────

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
    from backend.routes.nodes import nodes_bp
    from backend.routes.sse import sse_bp
    from backend.routes.export_data import export_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(sse_bp, url_prefix="/api/sse")
    app.register_blueprint(export_bp, url_prefix="/api")
    return app


@pytest.fixture
def tasks():
    """The task modules the routes import lazily, stubbed: a queued batch
    or finalize is only a recorded call."""
    keys = ("backend.tasks.streaming_transcription",
            "backend.tasks.transcription")
    saved = {k: sys.modules.get(k) for k in keys}
    mod = MagicMock()
    mod.transcribe_chunk_batch.delay.return_value = MagicMock(id="batch-1")
    mod.finalize_draft_streaming.delay.return_value = MagicMock(id="fin-1")
    mod.transcribe_chunk.delay.return_value = MagicMock(id="chunk-1")
    mod.transcribe_audio.delay.return_value = MagicMock(id="audio-1")
    for k in keys:
        sys.modules[k] = mod
    yield mod
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


@pytest.fixture
def app(tasks, tmp_path, monkeypatch):
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
    import backend.routes.drafts as drafts_mod
    import backend.routes.nodes as nodes_mod
    monkeypatch.setattr(drafts_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(nodes_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    app.media_root = tmp_path
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
def alice(app):
    u = User(username="alice", twitter_id="alice-tw", approved=True,
             plan="alpha", default_ai_usage="chat")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def client(app, alice):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(alice.id)
        sess["_fresh"] = True
    return c


def _session(app, user, session_id="sess-1", status="recording"):
    draft = Draft(user_id=user.id, session_id=session_id,
                  streaming_status=status, ai_usage="chat",
                  streaming_mime_type="audio/webm",
                  streaming_heartbeat_at=datetime.utcnow())
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    folder = app.media_root / f"drafts/{user.id}/{session_id}"
    folder.mkdir(parents=True, exist_ok=True)
    return draft, folder


def _chunk(client, session_id, index, payload):
    return client.post(
        f"/api/drafts/streaming/{session_id}/audio-chunk",
        data={"chunk": (io.BytesIO(payload), f"chunk_{index}.webm"),
              "chunk_index": str(index), "mime_type": "audio/webm"},
        content_type="multipart/form-data")


def _rows(session_id):
    return NodeTranscriptChunk.query.filter_by(session_id=session_id).count()


class TestStreamingChunkLimits:
    def test_a_chunk_larger_than_any_the_apps_send_is_refused(
            self, app, client, alice):
        _draft, folder = _session(app, alice)

        resp = _chunk(client, "sess-1", 3,
                      _payload(limits.MAX_STREAMING_CHUNK_BYTES + 1))

        assert resp.status_code == 413, resp.get_data(as_text=True)
        assert resp.get_json()["code"] == "chunk_too_large"
        assert _rows("sess-1") == 0
        assert list(folder.iterdir()) == []

    def test_a_large_chunk_without_a_content_length_is_measured_once_stored(
            self, app, client, alice, monkeypatch):
        # A request may carry no Content-Length (a chunked body): the
        # stored upload is measured instead.
        _draft, folder = _session(app, alice)
        monkeypatch.setattr(limits, "body_too_large", lambda _max: False)

        resp = _chunk(client, "sess-1", 3,
                      _payload(limits.MAX_STREAMING_CHUNK_BYTES + 1))

        assert resp.status_code == 413
        assert resp.get_json()["code"] == "chunk_too_large"
        assert _rows("sess-1") == 0
        assert list(folder.iterdir()) == []

    @pytest.mark.parametrize("index", [999_999, limits.MAX_STREAMING_CHUNKS,
                                       -1])
    def test_a_chunk_index_no_recording_needs_is_refused(
            self, app, client, alice, index):
        _draft, folder = _session(app, alice)

        resp = _chunk(client, "sess-1", index, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "chunk_index_out_of_range"
        assert _rows("sess-1") == 0
        assert list(folder.iterdir()) == []

    def test_past_the_bytes_of_one_recording_further_audio_is_refused(
            self, app, client, alice, monkeypatch):
        monkeypatch.setattr(limits, "MAX_STREAMING_SESSION_BYTES",
                            3 * WEB_CHUNK_BYTES)
        _draft, folder = _session(app, alice)
        for index in (1, 2, 3):
            assert _chunk(client, "sess-1", index,
                          _payload(WEB_CHUNK_BYTES)).status_code == 202

        resp = _chunk(client, "sess-1", 4, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 413
        assert resp.get_json()["code"] == "recording_too_large"
        # The audio accepted before stays, and nothing of the refused chunk.
        assert sorted(p.name for p in folder.iterdir()) == [
            "chunk_0001.webm", "chunk_0002.webm", "chunk_0003.webm"]
        assert _rows("sess-1") == 3

    def test_a_copy_of_a_stored_chunk_is_answered_and_not_counted(
            self, app, client, alice, monkeypatch):
        # Control (passes on main too): retries and the hidden page's
        # second copy (#88). A recording at its limit still gets "already
        # uploaded", not a refusal.
        monkeypatch.setattr(limits, "MAX_STREAMING_SESSION_BYTES",
                            2 * WEB_CHUNK_BYTES)
        _draft, folder = _session(app, alice)
        for index in (1, 2):
            assert _chunk(client, "sess-1", index,
                          _payload(WEB_CHUNK_BYTES)).status_code == 202

        resp = _chunk(client, "sess-1", 2, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Chunk already uploaded"
        assert _rows("sess-1") == 2

    def test_a_re_sent_failed_chunk_replaces_its_copy_and_is_not_counted_twice(
            self, app, client, alice, monkeypatch):
        # Control (passes on main too): a retry keeps working at the limit.
        monkeypatch.setattr(limits, "MAX_STREAMING_SESSION_BYTES",
                            2 * WEB_CHUNK_BYTES)
        _draft, folder = _session(app, alice)
        for index in (1, 2):
            assert _chunk(client, "sess-1", index,
                          _payload(WEB_CHUNK_BYTES)).status_code == 202
        row = NodeTranscriptChunk.query.filter_by(
            session_id="sess-1", chunk_index=2).one()
        row.status = "failed"
        _db.session.commit()

        resp = _chunk(client, "sess-1", 2, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 202, resp.get_data(as_text=True)
        assert row.status == "stored"
        assert (folder / "chunk_0002.webm").stat().st_size == WEB_CHUNK_BYTES

    def test_control_realistic_chunks_of_a_long_recording_pass(
            self, app, client, alice, tasks):
        # Control (passes on main too): the apps' chunk sizes, two full
        # batches, the largest chunk measured, and the last chunk index of
        # a six-hour recording at 15 s a chunk.
        _draft, folder = _session(app, alice)
        for index in range(1, 41):
            resp = _chunk(client, "sess-1", index, _payload(WEB_CHUNK_BYTES))
            assert resp.status_code == 202, (index, resp.get_data(as_text=True))
        assert _chunk(client, "sess-1", 41, _payload(
            LARGEST_APP_CHUNK_BYTES)).status_code == 202
        six_hours = limits.MAX_TRANSCRIBED_SECONDS // \
            limits.STREAMING_CHUNK_SECONDS
        assert _chunk(client, "sess-1", six_hours - 1,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202
        assert tasks.transcribe_chunk_batch.delay.call_count == 2

    def test_the_bounds_hold_a_six_hour_recording_with_room(self):
        # Control: the numbers themselves, against the measured apps.
        six_hours = 6 * 60 * 60
        assert limits.MAX_TRANSCRIBED_SECONDS >= six_hours
        assert limits.MAX_STREAMING_CHUNKS >= 2 * six_hours // 15
        # Twelve hours of the web's measured chunks.
        assert limits.MAX_STREAMING_SESSION_BYTES >= \
            2 * six_hours // 15 * WEB_CHUNK_BYTES
        assert limits.MAX_STREAMING_CHUNK_BYTES >= 16 * LARGEST_APP_CHUNK_BYTES


class TestFinalizeCount:
    @pytest.mark.parametrize("total", ["5", 10**9, -1, 2.5, True])
    def test_a_count_no_recording_can_have_is_refused(
            self, app, client, alice, tasks, total):
        draft, _folder = _session(app, alice)

        resp = client.post("/api/drafts/streaming/sess-1/finalize",
                           json={"total_chunks": total})

        assert resp.status_code == 400
        assert Draft.query.get(draft.id).streaming_status == "recording"
        tasks.finalize_draft_streaming.delay.assert_not_called()

    def test_control_a_real_count_finalizes(self, app, client, alice, tasks):
        _session(app, alice)

        resp = client.post("/api/drafts/streaming/sess-1/finalize",
                           json={"total_chunks": 1440})

        assert resp.status_code == 202, resp.get_data(as_text=True)
        tasks.finalize_draft_streaming.delay.assert_called_once()


class TestOlderNodePath:
    def _node(self, app, user):
        node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                    streaming_transcription=True,
                    streaming_session_id="legacy-1", ai_usage="chat")
        node.set_content("[Voice note – streaming transcription in progress]")
        _db.session.add(node)
        _db.session.commit()
        (app.media_root / f"streaming/{user.id}/legacy-1").mkdir(parents=True)
        return node

    def _post(self, client, node, index, payload):
        return client.post(
            f"/api/nodes/{node.id}/audio-chunk",
            data={"chunk": (io.BytesIO(payload), "chunk.webm"),
                  "chunk_index": str(index), "session_id": "legacy-1"},
            content_type="multipart/form-data")

    def test_control_a_capped_user_s_started_recording_goes_on(
            self, app, client, alice, tasks):
        # Control (passes on main too): #341, the cap is checked at init.
        node = self._node(app, alice)
        _cap(alice)

        resp = self._post(client, node, 0, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 202, resp.get_data(as_text=True)
        tasks.transcribe_chunk.delay.assert_called_once()

    def test_an_absurd_index_is_refused(self, app, client, alice, tasks):
        node = self._node(app, alice)

        resp = self._post(client, node, 999_999, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 400
        tasks.transcribe_chunk.delay.assert_not_called()

    def test_control_a_normal_chunk_is_queued(self, app, client, alice, tasks):
        node = self._node(app, alice)

        resp = self._post(client, node, 0, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 202, resp.get_data(as_text=True)
        tasks.transcribe_chunk.delay.assert_called_once()


class TestTranscriptionStreamSaysWhy:
    def _events(self, resp):
        out = []
        for block in resp.get_data(as_text=True).strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines())
            out.append((lines.get("event"), json.loads(lines["data"])))
        return out

    def test_a_chunk_kept_without_its_text_carries_the_reason(
            self, app, client, alice):
        draft, _folder = _session(app, alice, status="completed")
        kept = NodeTranscriptChunk(session_id="sess-1", chunk_index=0,
                                   status="failed")
        failed = NodeTranscriptChunk(session_id="sess-1", chunk_index=1,
                                     status="failed", error="Transcription "
                                     "failed")
        _db.session.add_all([kept, failed])
        limits.mark_untranscribed([kept])
        _db.session.commit()

        events = self._events(client.get(
            "/api/sse/drafts/sess-1/transcription-stream"))

        errors = [data for name, data in events if name == "chunk_error"]
        assert errors[0]["code"] == limits.TOO_LONG
        assert "code" not in errors[1]   # a failure stays a failure


class TestRecordingsInProgress:
    def _init(self, client):
        return client.post("/api/drafts/streaming/init",
                           json={"ai_usage": "chat"})

    def _sessions(self, alice):
        return Draft.query.filter(Draft.user_id == alice.id,
                                  Draft.session_id.isnot(None)).count()

    def test_a_new_recording_waits_while_three_are_in_progress(
            self, app, client, alice):
        for _ in range(limits.MAX_RECORDINGS_IN_PROGRESS):
            assert self._init(client).status_code == 201

        resp = self._init(client)

        assert resp.status_code == 429
        assert resp.get_json() == {"error": limits.TOO_MANY_RECORDINGS_TEXT,
                                   "code": "too_many_recordings"}
        assert self._sessions(alice) == limits.MAX_RECORDINGS_IN_PROGRESS

    def test_finalizing_ones_count_until_their_transcript_is_in(
            self, app, client, alice):
        for n in range(limits.MAX_RECORDINGS_IN_PROGRESS):
            _session(app, alice, f"fin-{n}", status="finalizing")
        assert self._init(client).status_code == 429

        # One is done: a new recording starts.
        Draft.query.filter_by(session_id="fin-0").one() \
            .streaming_status = "completed"
        _db.session.commit()
        assert self._init(client).status_code == 201

    def test_control_left_behind_and_stuck_ones_do_not_count(
            self, app, client, alice):
        # Control (passes on main too): a crashed app's session (no sign of
        # life for 45 s), a released one, and a finalize that died long ago
        # never keep the user from recording.
        long_ago = datetime.utcnow() - timedelta(hours=2)
        for n in range(3):
            draft, _ = _session(app, alice, f"stale-{n}")
            draft.streaming_heartbeat_at = long_ago
        for n in range(3):
            draft, _ = _session(app, alice, f"released-{n}")
            draft.streaming_heartbeat_at = None
        for n in range(3):
            draft, _ = _session(app, alice, f"stuck-{n}", status="finalizing")
            draft.updated_at = long_ago
        _db.session.commit()
        Draft.query.filter(Draft.session_id.like("stuck-%")).update(
            {Draft.updated_at: long_ago}, synchronize_session=False)
        _db.session.commit()

        assert self._init(client).status_code == 201

    def test_control_another_account_s_recordings_do_not_count(
            self, app, client, alice):
        bob = User(username="bob", twitter_id="bob-tw", approved=True,
                   plan="alpha", default_ai_usage="chat")
        _db.session.add(bob)
        _db.session.commit()
        for n in range(3):
            _session(app, bob, f"bob-{n}")

        assert self._init(client).status_code == 201


def _waiting_uploads(user, n, status="pending", created_at=None):
    for _ in range(n):
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", transcription_status=status,
                    ai_usage="chat")
        if created_at is not None:
            node.created_at = created_at
        node.set_content("[Voice note – transcription pending]")
        _db.session.add(node)
    _db.session.commit()


class TestUploadsInProgress:

    def test_a_new_upload_waits_while_three_wait_for_their_transcript(
            self, app, client, alice):
        for n in range(limits.MAX_UPLOADS_IN_PROGRESS):
            resp = _init_upload(client, 8, 2, upload_id=f"up-{n}")
            assert resp.status_code == 201, resp.get_data(as_text=True)
        nodes = Node.query.count()

        resp = _init_upload(client, 8, 2, upload_id="up-x")
        assert resp.status_code == 429
        assert resp.get_json() == {"error": limits.TOO_MANY_UPLOADS_TEXT,
                                   "code": "too_many_uploads"}
        # The same for a file sent in one request, and the older path.
        resp = client.post("/api/nodes/", data={
            "audio_file": (io.BytesIO(b"fake audio"), "talk.m4a"),
            "ai_usage": "chat"}, content_type="multipart/form-data")
        assert resp.status_code == 429
        assert client.post("/api/nodes/streaming/init",
                           json={"ai_usage": "chat"}).status_code == 429
        assert Node.query.count() == nodes
        assert not (app.media_root / f"chunks/{alice.id}/up-x").exists()

        # One transcript is in: the next upload starts.
        Node.query.filter_by(user_id=alice.id).first() \
            .transcription_status = "completed"
        _db.session.commit()
        assert _init_upload(client, 8, 2, upload_id="up-y").status_code == 201

    def test_control_old_failed_and_finished_ones_do_not_count(
            self, app, client, alice):
        # Control (passes on main too): uploads never finished that started
        # longer ago than the window, failed ones and transcribed ones.
        _waiting_uploads(alice, 3, created_at=datetime.utcnow()
                         - limits.UPLOAD_COUNTS_FOR - timedelta(hours=1))
        _waiting_uploads(alice, 3, status="failed")
        _waiting_uploads(alice, 3, status="completed")

        assert _init_upload(client, 8, 2).status_code == 201

    def test_a_piece_for_an_upload_that_no_longer_counts_is_refused(
            self, app, client, alice):
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        node = Node.query.get(node_id)
        node.created_at = datetime.utcnow() - limits.UPLOAD_COUNTS_FOR \
            - timedelta(minutes=1)
        _db.session.commit()

        resp = _send_piece(client, node_id, 0, b"abcd")

        assert resp.status_code == 410
        assert resp.get_json()["code"] == "upload_expired"

    def test_control_a_slow_upload_within_the_window_keeps_going(
            self, app, client, alice):
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        node = Node.query.get(node_id)
        node.created_at = datetime.utcnow() - timedelta(hours=5)
        _db.session.commit()

        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200

    def test_an_older_path_recording_that_no_longer_counts_takes_no_audio(
            self, app, client, alice, tasks):
        node = TestOlderNodePath()._node(app, alice)
        node.created_at = datetime.utcnow() - limits.UPLOAD_COUNTS_FOR \
            - timedelta(minutes=1)
        _db.session.commit()

        resp = TestOlderNodePath()._post(client, node, 0,
                                         _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 410
        tasks.transcribe_chunk.delay.assert_not_called()

    def test_an_upload_the_app_reports_failed_stops_counting(
            self, app, client, alice):
        for n in range(limits.MAX_UPLOADS_IN_PROGRESS):
            assert _init_upload(client, 8, 2,
                                upload_id=f"up-{n}").status_code == 201
        assert _init_upload(client, 8, 2,
                            upload_id="up-x").status_code == 429

        assert client.post("/api/nodes/upload/cleanup",
                           json={"upload_id": "up-0"}).status_code == 200

        failed = Node.query.filter_by(transcription_status="failed").one()
        assert failed.transcription_error == limits.UPLOAD_FAILED_TEXT
        assert _init_upload(client, 8, 2, upload_id="up-y").status_code == 201


KEY = "device-b-9876543210"


def _left_behind(app, user, session_id, quiet, chunks=2):
    """A recording its app left (killed mid-recording): its last sign of
    life *quiet* ago (None: released), with *chunks* chunks stored."""
    draft, folder = _session(app, user, session_id)
    draft.streaming_heartbeat_at = (
        None if quiet is None else datetime.utcnow() - quiet)
    draft.streaming_device_key = KEY    # the device that recorded it
    for idx in range(chunks):
        _db.session.add(NodeTranscriptChunk(
            session_id=session_id, chunk_index=idx, status="stored"))
        (folder / f"chunk_{idx:04d}.webm").write_bytes(FRAGMENT)
    _db.session.commit()
    return draft


def _init_recording(client):
    return client.post("/api/drafts/streaming/init", json={"ai_usage": "chat"})


class TestUnfinishedRecordings:
    def test_a_new_recording_waits_while_six_are_unfinished(
            self, app, client, alice):
        for n in range(limits.MAX_UNFINISHED_RECORDINGS):
            _left_behind(app, alice, f"left-{n}", timedelta(hours=1))

        resp = _init_recording(client)

        assert resp.status_code == 429
        assert resp.get_json() == {"error": limits.TOO_MANY_UNFINISHED_TEXT,
                                   "code": "too_many_unfinished"}
        # Discarding one (the recovery banner) lets the next one start.
        assert client.delete(
            "/api/drafts/streaming/left-0/discard").status_code == 200
        assert _init_recording(client).status_code == 201

    def test_control_quiet_for_a_day_or_without_audio_they_do_not_count(
            self, app, client, alice):
        # Control (passes on main too): a crashed app never locks a user
        # out for longer than a day, and a recording left before its first
        # chunk never counts.
        for n in range(limits.MAX_UNFINISHED_RECORDINGS):
            _left_behind(app, alice, f"old-{n}", timedelta(hours=25))
        for n in range(limits.MAX_UNFINISHED_RECORDINGS):
            _left_behind(app, alice, f"empty-{n}", timedelta(hours=1),
                         chunks=0)

        assert _init_recording(client).status_code == 201


class TestTakingAudioAgain:
    """A recording that stopped keeping going (quiet longer than
    CONTINUES_WITHIN, or released) is checked as a start is when it takes
    audio again; one that kept going is not (#341)."""

    def _claim(self, client, session_id):
        return client.post(f"/api/drafts/streaming/{session_id}/claim",
                           json={"device_key": KEY})

    def test_at_the_usage_limit_a_stopped_recording_is_not_continued(
            self, app, client, alice):
        _left_behind(app, alice, "stopped", timedelta(minutes=30))
        _cap(alice)

        resp = _chunk(client, "stopped", 2, _payload(WEB_CHUNK_BYTES))
        assert resp.status_code == 402
        assert resp.get_json()["error"] == "monthly_spend_limit_reached"
        assert _rows("stopped") == 2
        resp = self._claim(client, "stopped")
        assert resp.status_code == 402

    def test_at_the_usage_limit_a_released_recording_is_not_continued(
            self, app, client, alice):
        _left_behind(app, alice, "released", None)
        _cap(alice)

        assert _chunk(client, "released", 2,
                      _payload(WEB_CHUNK_BYTES)).status_code == 402
        assert _rows("released") == 2

    def test_control_a_capped_recording_that_kept_going_takes_its_audio(
            self, app, client, alice):
        # Control (passes on main too): #341, also across a dropped
        # connection of a few minutes.
        _left_behind(app, alice, "going", timedelta(minutes=5))
        _cap(alice)

        assert _chunk(client, "going", 2,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202

    def test_a_stopped_recording_counts_against_the_bounds_again(
            self, app, client, alice):
        _left_behind(app, alice, "stopped", timedelta(minutes=30))
        for _ in range(limits.MAX_RECORDINGS_IN_PROGRESS):
            assert _init_recording(client).status_code == 201

        resp = _chunk(client, "stopped", 2, _payload(WEB_CHUNK_BYTES))
        assert resp.status_code == 429
        assert resp.get_json()["code"] == "too_many_recordings"
        assert self._claim(client, "stopped").status_code == 429
        assert _rows("stopped") == 2

    def test_the_transcription_stream_does_not_make_it_live_when_refused(
            self, app, client, alice):
        draft = _left_behind(app, alice, "stopped", timedelta(minutes=30))
        quiet_since = draft.streaming_heartbeat_at
        _cap(alice)

        resp = client.get("/api/sse/drafts/stopped/transcription-stream")
        resp.close()

        _db.session.expire_all()
        assert Draft.query.filter_by(session_id="stopped").one() \
            .streaming_heartbeat_at == quiet_since

    def test_app_killed_mid_recording_relaunched_and_continued(
            self, app, client, alice):
        # Control (passes before this change too): the real flow. The app
        # is killed mid-recording; half an hour later it is opened again,
        # sends the chunk it still held, and the user continues the
        # recording, then stops.
        _left_behind(app, alice, "killed", timedelta(minutes=30), chunks=4)

        assert _chunk(client, "killed", 4,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202
        resp = self._claim(client, "killed")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["next_chunk_index"] == 5
        assert _chunk(client, "killed", 5,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202
        resp = client.post("/api/drafts/streaming/killed/finalize",
                           json={"total_chunks": 6, "device_key": KEY})
        assert resp.status_code == 202, resp.get_data(as_text=True)

    def test_app_killed_mid_recording_relaunched_and_discarded(
            self, app, client, alice):
        # Control (passes before this change too).
        _left_behind(app, alice, "killed", timedelta(minutes=30))

        assert client.delete(
            "/api/drafts/streaming/killed/discard").status_code == 200
        assert _init_recording(client).status_code == 201

    def test_control_a_capped_user_s_left_recording_is_still_transcribed(
            self, app, client, alice, tasks):
        # Control (passes on main too): its audio was accepted, so the
        # recovery banner's "transcribe the rest" still runs (#341).
        _left_behind(app, alice, "killed", timedelta(minutes=30))
        _cap(alice)

        resp = client.post("/api/drafts/streaming/killed/transcribe-remaining")
        assert resp.status_code == 202, resp.get_data(as_text=True)
        tasks.transcribe_chunk_batch.delay.assert_called_once()


class TestStartsAtOnce:
    """Two starts at the same moment: the count is made again under the
    account's lock, right before the insert (on SQLite the lock itself is a
    no-op; here it stands in for another start that committed meanwhile)."""

    def test_recordings_are_counted_again_under_the_lock(
            self, app, client, alice, monkeypatch):
        locked = []

        def others_started_meanwhile(user_id):
            locked.append(user_id)
            for n in range(limits.MAX_RECORDINGS_IN_PROGRESS):
                _session(app, alice, f"meanwhile-{n}")
        monkeypatch.setattr(limits, "lock_account", others_started_meanwhile)
        before = Draft.query.count()

        resp = _init_recording(client)

        assert locked == [alice.id]
        assert resp.status_code == 429
        assert Draft.query.count() == before + limits.MAX_RECORDINGS_IN_PROGRESS

    def test_uploads_are_counted_again_under_the_lock(
            self, app, client, alice, monkeypatch):
        def others_started_meanwhile(user_id):
            _waiting_uploads(alice, limits.MAX_UPLOADS_IN_PROGRESS)
        monkeypatch.setattr(limits, "lock_account", others_started_meanwhile)

        resp = _init_upload(client, 8, 2)

        assert resp.status_code == 429
        assert resp.get_json()["code"] == "too_many_uploads"
        assert Node.query.count() == limits.MAX_UPLOADS_IN_PROGRESS

    def test_a_busy_lock_answers_plainly(self, app, client, alice,
                                         monkeypatch):
        def busy(user_id):
            raise limits.RecordingsBusy()
        monkeypatch.setattr(limits, "lock_account", busy)

        resp = _init_recording(client)

        assert resp.status_code == 503
        assert resp.get_json()["code"] == "recordings_busy"
        assert Draft.query.count() == 0


# ── Chunked file uploads ─────────────────────────────────────────────────

def _init_upload(client, filesize, total_chunks, upload_id="up-1",
                 filename="talk.m4a"):
    return client.post("/api/nodes/upload/init", json={
        "filename": filename, "filesize": filesize,
        "total_chunks": total_chunks, "upload_id": upload_id,
        "ai_usage": "chat"})


def _send_piece(client, node_id, index, payload, upload_id="up-1"):
    return client.post("/api/nodes/upload/chunk", data={
        "chunk": (io.BytesIO(payload), "blob"), "chunk_index": str(index),
        "upload_id": upload_id, "node_id": str(node_id),
    }, content_type="multipart/form-data")


def _finalize_upload(client, node_id, upload_id="up-1"):
    return client.post("/api/nodes/upload/finalize",
                       json={"upload_id": upload_id, "node_id": node_id})


def _upload(client, data, piece=5 * 1024 * 1024, upload_id="up-1"):
    """The apps' upload: 5 MB pieces, then finalize."""
    total = -(-len(data) // piece)
    resp = _init_upload(client, len(data), total, upload_id)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    node_id = resp.get_json()["node_id"]
    for index in range(total):
        resp = _send_piece(client, node_id, index,
                           data[index * piece:(index + 1) * piece], upload_id)
        assert resp.status_code == 200, resp.get_data(as_text=True)
    return node_id, _finalize_upload(client, node_id, upload_id)


class TestUploadInit:
    @pytest.mark.parametrize("filesize,total", [
        (20 * 1024 * 1024, 10_000),     # more pieces than any upload has
        (20 * 1024 * 1024, 1),          # too few to hold the file
        (100, 150),                     # more pieces than the file has bytes
        ("20000000", 4),                # not a number of bytes
        (20 * 1024 * 1024, "4"),
        (20 * 1024 * 1024, 2.5),
        (-5, 1),
    ])
    def test_a_size_or_count_no_upload_can_have_is_refused(
            self, app, client, alice, filesize, total):
        resp = _init_upload(client, filesize, total)

        assert resp.status_code == 400, resp.get_data(as_text=True)
        assert resp.get_json()["code"] == "invalid_upload_size"
        assert Node.query.count() == 0
        assert not (app.media_root / f"chunks/{alice.id}/up-1").exists()

    def test_a_file_over_the_largest_upload_is_refused(self, app, client,
                                                       alice):
        # Control (passes on main too): the size limit that was there.
        resp = _init_upload(client, 200 * 1024 * 1024 + 1, 41)

        assert resp.status_code == 413
        assert Node.query.count() == 0

    def test_control_the_largest_upload_in_5_mb_pieces_starts(
            self, app, client, alice):
        resp = _init_upload(client, 200 * 1024 * 1024, 40)

        assert resp.status_code == 201, resp.get_data(as_text=True)


class TestUploadPieces:
    def _start(self, client, filesize, total):
        resp = _init_upload(client, filesize, total)
        assert resp.status_code == 201, resp.get_data(as_text=True)
        return resp.get_json()["node_id"]

    def test_a_piece_its_init_did_not_announce_is_refused(
            self, app, client, alice):
        node_id = self._start(client, 8, 2)

        resp = _send_piece(client, node_id, 5, b"abcd")

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "chunk_index_out_of_range"
        folder = app.media_root / f"chunks/{alice.id}/up-1"
        assert sorted(p.name for p in folder.iterdir()) == ["metadata.json"]

    def test_a_piece_larger_than_the_apps_send_is_refused(
            self, app, client, alice):
        node_id = self._start(client, 20 * 1024 * 1024, 4)

        resp = _send_piece(client, node_id, 0,
                           b"x" * (limits.MAX_UPLOAD_CHUNK_BYTES + 1))

        assert resp.status_code == 413
        assert resp.get_json()["code"] == "chunk_too_large"

    def test_pieces_past_the_announced_size_are_refused(
            self, app, client, alice):
        node_id = self._start(client, 8, 2)
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200

        resp = _send_piece(client, node_id, 1, b"efghijkl")

        assert resp.status_code == 413
        assert resp.get_json()["code"] == "upload_too_large"
        folder = app.media_root / f"chunks/{alice.id}/up-1"
        assert not (folder / "chunk_0001").exists()

    def test_a_re_sent_piece_replaces_its_copy_and_is_not_counted_twice(
            self, app, client, alice, tasks):
        # Control (passes on main too): a retried piece keeps working.
        node_id = self._start(client, 8, 2)
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        assert _send_piece(client, node_id, 1, b"efgh").status_code == 200

        resp = _finalize_upload(client, node_id)

        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_a_piece_for_another_entry_is_refused(self, app, client, alice):
        node_id = self._start(client, 8, 2)
        other = Node(user_id=alice.id, human_owner_id=alice.id,
                     node_type="user")
        other.set_content("another entry")
        _db.session.add(other)
        _db.session.commit()

        resp = _send_piece(client, other.id, 0, b"abcd")

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "upload_mismatch"
        assert node_id != other.id


class TestUploadFinalize:
    def test_pieces_short_of_the_announced_size_are_refused_and_removed(
            self, app, client, alice, tasks):
        resp = _init_upload(client, 8, 2)
        node_id = resp.get_json()["node_id"]
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        assert _send_piece(client, node_id, 1, b"ef").status_code == 200

        resp = _finalize_upload(client, node_id)

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "upload_size_mismatch"
        assert not (app.media_root / f"chunks/{alice.id}/up-1").exists()
        assert not list(app.media_root.rglob("original.*"))
        node = Node.query.get(node_id)
        assert node.transcription_status == "failed"
        assert node.audio_original_url is None
        tasks.transcribe_audio.delay.assert_not_called()

    def test_control_the_iphone_fixture_recording_uploads(
            self, app, client, alice, tasks):
        # Control: a real recording made by the iPhone app (two fMP4
        # chunks), uploaded as a file in one piece.
        data = b"".join((FIXTURES / name).read_bytes()
                        for name in ("chunk_0000.mp4", "chunk_0001.mp4"))

        node_id, resp = _upload(client, data)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        stored = app.media_root / f"user/{alice.id}/node/{node_id}"
        assert (stored / "original.m4a").read_bytes() == data

    def test_control_a_large_file_in_5_mb_pieces_uploads(
            self, app, client, alice, tasks):
        data = os.urandom(12 * 1024 * 1024 + 4321)   # three pieces

        node_id, resp = _upload(client, data)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        stored = app.media_root / f"user/{alice.id}/node/{node_id}"
        assert (stored / "original.m4a").stat().st_size == len(data)


# ── The data download ────────────────────────────────────────────────────

@pytest.fixture
def redis(monkeypatch):
    from backend.tests.fake_redis import install
    return install(monkeypatch)


class TestExportLimit:
    def _get(self, client):
        resp = client.get("/api/export/threads")
        if resp.status_code == 200:
            resp.get_data()
        return resp

    def test_a_second_download_waits_for_the_first(self, app, client, alice,
                                                   redis):
        first = client.get("/api/export/threads")
        try:
            assert first.status_code == 200

            second = client.get("/api/export/threads")
            assert second.status_code == 429
            assert second.get_json()["code"] == "export_running"
            assert client.head("/api/export/threads").status_code == 429
        finally:
            first.get_data()
            first.close()
        third = self._get(client)
        assert third.status_code == 200
        third.close()

    def test_a_small_number_an_hour(self, app, client, alice, redis):
        from backend.routes.export_data import EXPORTS_PER_HOUR
        for _ in range(EXPORTS_PER_HOUR):
            resp = self._get(client)
            assert resp.status_code == 200
            resp.close()

        resp = client.get("/api/export/threads")
        assert resp.status_code == 429
        assert resp.get_json()["code"] == "export_limit_reached"
        assert client.head("/api/export/threads").status_code == 429

        redis.advance(3601)
        resp = self._get(client)
        assert resp.status_code == 200
        resp.close()

    def test_the_check_before_a_download_counts_as_none(
            self, app, client, alice, redis):
        # Control (passes on main too): the web's HEAD check, many times.
        for _ in range(30):
            assert client.head("/api/export/threads").status_code == 200
        resp = self._get(client)
        assert resp.status_code == 200
        resp.close()

    def test_control_without_redis_downloads_go_ahead(self, app, client,
                                                      alice):
        # Control: no Redis (here, the test app), no limit: as before.
        for _ in range(12):
            resp = self._get(client)
            assert resp.status_code == 200
            resp.close()


# ── The transcription task ───────────────────────────────────────────────

@pytest.fixture
def st(monkeypatch, tmp_path):
    """backend.tasks.streaming_transcription with the celery glue stubbed,
    so task bodies run directly against an in-memory app; the provider,
    the merge and the duration probe are stand-ins."""
    _GLUE = ("backend.celery_app", "backend.tasks.streaming_transcription",
             "backend.tasks.llm_completion")
    saved = {k: sys.modules.get(k) for k in _GLUE}
    fake_celery_app = MagicMock()

    def _passthrough_task(*args, **kwargs):
        def deco(f):
            return f
        return deco
    fake_celery_app.celery.task = _passthrough_task
    sys.modules["backend.celery_app"] = fake_celery_app
    sys.modules.pop("backend.tasks.streaming_transcription", None)
    fake_llm = MagicMock()
    sys.modules["backend.tasks.llm_completion"] = fake_llm
    import backend.tasks.streaming_transcription as module

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }
    _db.init_app(app)
    fake_celery_app.flask_app = app
    module.flask_app = app
    module._fake_llm = fake_llm
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))

    monkeypatch.setattr(module, "compress_audio_if_needed", lambda p, log: p)
    module._duration = 300.0     # a batch of 20 chunks of 15 s
    monkeypatch.setattr(module, "get_audio_duration",
                        lambda p, log: module._duration)
    monkeypatch.setattr(module, "get_openai_chat_key", lambda cfg: "key")
    merged = {"n": 0}

    def _merge(paths, init_segment_path=None, output_suffix=None,
               report=None):
        merged["n"] += 1
        out = tmp_path / f"merged-{merged['n']}{output_suffix}"
        out.write_bytes(b"".join(pathlib.Path(p).read_bytes() for p in paths))
        return str(out)
    monkeypatch.setattr(module, "concat_fragmented_media", _merge)
    fake_openai = MagicMock()
    fake_openai.return_value.audio.transcriptions.create.return_value = (
        type("R", (), {"text": "spoken words"})())
    monkeypatch.setattr(module, "OpenAI", fake_openai)
    module._provider = fake_openai.return_value.audio.transcriptions.create
    module._root = tmp_path

    with app.app_context():
        _db.create_all()
        user = User(username="talker", approved=True, plan="alpha",
                    default_ai_usage="chat")
        _db.session.add(user)
        _db.session.commit()
        module._user = user
        yield module
        _db.session.rollback()
        _db.drop_all()

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _recording(st, session_id, indices, label=None, parent=None):
    user = st._user
    draft = Draft.query.filter_by(session_id=session_id).first()
    if draft is None:
        draft = Draft(user_id=user.id, session_id=session_id, label=label,
                      parent_id=parent.id if parent else None,
                      streaming_status="recording", ai_usage="chat",
                      streaming_mime_type="audio/webm")
        draft.set_content("")
        _db.session.add(draft)
    folder = st._root / f"drafts/{user.id}/{session_id}"
    folder.mkdir(parents=True, exist_ok=True)
    for idx in indices:
        _db.session.add(NodeTranscriptChunk(
            session_id=session_id, chunk_index=idx, status="stored"))
        (folder / f"chunk_{idx:04d}.webm").write_bytes(
            b"\x1aE\xdf\xa3-webm-head" if idx == 0 else FRAGMENT)
    _db.session.commit()
    return draft, folder


def _batch(st, session_id, indices):
    st.transcribe_chunk_batch(MagicMock(), session_id, list(indices))


def _statuses(session_id):
    return {c.chunk_index: c.status for c in NodeTranscriptChunk.query.filter_by(
        session_id=session_id)}


class TestUsageLimitMidRecording:
    def test_control_a_started_recording_is_transcribed_to_its_end(self, st):
        # Control (passes on main too): #341. The cap is checked when a
        # recording starts; once accepted, it is transcribed to its end.
        _recording(st, "sess-cap", range(0, 20))
        _batch(st, "sess-cap", range(0, 20))
        _cap(st._user)                      # the limit is reached here
        _recording(st, "sess-cap", range(20, 40))
        _batch(st, "sess-cap", range(20, 40))

        assert set(_statuses("sess-cap").values()) == {"completed"}
        assert st._provider.call_count == 2

    def test_a_capped_recording_stops_being_transcribed_at_the_length_limit(
            self, st):
        # ... up to the length one recording may have transcribed.
        _recording(st, "sess-cap-long", range(0, 20))
        _batch(st, "sess-cap-long", range(0, 20))
        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-cap-long", chunk_index=0).one()
        head.audio_seconds = limits.MAX_TRANSCRIBED_SECONDS
        _db.session.commit()
        _cap(st._user)
        _recording(st, "sess-cap-long", range(20, 40))
        _batch(st, "sess-cap-long", range(20, 40))

        statuses = _statuses("sess-cap-long")
        assert {statuses[i] for i in range(20, 40)} == {"failed"}
        assert st._provider.call_count == 1

    def test_a_voice_turn_past_the_length_limit_keeps_its_transcript(
            self, st):
        _recording(st, "sess-voice", range(0, 20), label="Voice")
        _batch(st, "sess-voice", range(0, 20))
        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-voice", chunk_index=0).one()
        head.audio_seconds = limits.MAX_TRANSCRIBED_SECONDS
        _db.session.commit()
        _cap(st._user)
        folder = st._root / f"drafts/{st._user.id}/sess-voice"
        _recording(st, "sess-voice", range(20, 40))
        _batch(st, "sess-voice", range(20, 40))

        st.finalize_draft_streaming(
            MagicMock(), "sess-voice", 40, label="Voice",
            user_id=st._user.id, parent_id=None, model="gpt-5")

        draft = Draft.query.filter_by(session_id="sess-voice").one()
        assert draft.streaming_status == "completed"
        assert draft.llm_node_id is None
        # The reply is skipped for the cap, with its usual warning.
        assert draft.streaming_warning == st.VOICE_REPLY_SKIPPED_SPEND_CAP
        entry = Node.query.filter_by(node_type="user").filter(
            Node.streaming_transcription.is_(True)).one()
        assert entry.get_content() == "spoken words\n\n" + limits.NOTE_PART
        st._fake_llm.generate_llm_response.si.assert_not_called()
        # The audio went with the entry.
        moved = st._root / f"nodes/{st._user.id}/{entry.id}"
        assert len([p for p in moved.iterdir()
                    if p.name.startswith("batch_")]) == 2
        assert not folder.exists()

    def test_control_an_uncapped_recording_is_transcribed_in_full(self, st):
        # Control (passes on main too).
        _recording(st, "sess-ok", range(0, 40))
        _batch(st, "sess-ok", range(0, 20))
        _batch(st, "sess-ok", range(20, 40))

        st.finalize_draft_streaming(MagicMock(), "sess-ok", 40)

        assert set(_statuses("sess-ok").values()) == {"completed"}
        content = Draft.query.filter_by(session_id="sess-ok").one() \
            .get_content()
        assert "*Note" not in content
        assert st._provider.call_count == 2


class TestLengthLimit:
    def test_audio_past_the_length_limit_is_kept_and_not_transcribed(self, st):
        _recording(st, "sess-long", range(0, 20))
        _batch(st, "sess-long", range(0, 20))
        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-long", chunk_index=0).one()
        assert head.audio_seconds == 300.0
        # Five minutes short of the limit transcribed so far ...
        head.audio_seconds = limits.MAX_TRANSCRIBED_SECONDS - 300
        _db.session.commit()
        _recording(st, "sess-long", range(20, 40))
        _batch(st, "sess-long", range(20, 40))   # exactly at the limit
        _recording(st, "sess-long", range(40, 41))
        st._duration = 15.0
        _batch(st, "sess-long", [40])             # past it

        statuses = _statuses("sess-long")
        assert statuses[39] == "completed"
        assert statuses[40] == "failed"
        kept = NodeTranscriptChunk.query.filter_by(
            session_id="sess-long", chunk_index=40).one()
        assert limits.untranscribed_code(kept.error) == limits.TOO_LONG
        assert st._provider.call_count == 2

        st.finalize_draft_streaming(MagicMock(), "sess-long", 41)
        content = Draft.query.filter_by(session_id="sess-long").one() \
            .get_content()
        assert content.endswith(limits.NOTE_PART)

    def test_control_an_hour_long_recording_is_transcribed(self, st):
        # Control (passes on main too): twelve 5-minute batches.
        for n in range(12):
            indices = range(20 * n, 20 * n + 20)
            _recording(st, "sess-hour", indices)
            _batch(st, "sess-hour", indices)
        assert set(_statuses("sess-hour").values()) == {"completed"}
        assert st._provider.call_count == 12
