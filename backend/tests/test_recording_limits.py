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
  MAX_TRANSCRIBED_SECONDS of decoded audio: the audio past it is stored but
  not transcribed, the text ends with a note, and the transcription stream
  tells the apps why;
- a recording that holds audio is never refused more for quiet time or
  the usage limit within a day of its last activity (a pause, a phone
  call, time offline, an app killed and reopened later); one left
  untouched longer is a new start when it takes audio again;
- an account holds at most MAX_OPEN_RECORDINGS open recordings with audio,
  and the refusal's words match what recovery offers; recordings without
  audio are bounded apart (MAX_EMPTY_STARTS), one older than
  EMPTY_SESSION_EXPIRES_AFTER is a new start at its first audio, and the
  daily clean-up removes them after EMPTY_SESSION_REMOVED_AFTER; a burst of
  Voice turns never reaches the count;
- starts at the same moment are counted again under the account's lock;
- lengths and the transcription cost come from the decoded audio (a
  measured short turn counts its length); a chunk decodes to at most
  MAX_CHUNK_AUDIO_SECONDS, past which its part is not re-encoded or sent;
  audio that cannot be decoded is re-encoded on the server and only that
  is sent and counted (at least 15 s a chunk), and audio that cannot be
  re-encoded either is kept, not sent, with a note;
- a task run's local audio work has one budget, the provider's time not
  counted; parts it does not reach go to a follow-up run;
- a recording is active through its start and its audio: opening the
  transcription stream marks it live for the takeover between devices, not
  for these limits;
- a recording of realistic size and length passes untouched (controls).

Chunked file uploads (``/api/nodes/upload/...``): the init's size and piece
count, each piece's index and size, and the finalize's total are held to
what the init announced; a refused finalize deletes the pieces. A new
upload is refused while MAX_UPLOADS_IN_PROGRESS of the account's uploads
with activity within UPLOAD_COUNTS_FOR wait for their transcript (deleted
ones not counted); an upload takes pieces within UPLOAD_COUNTS_FOR of its
last activity, one the app reports failed stops counting, one accepted at
its start is transcribed, and an uploaded file is cut at
MAX_UPLOAD_TRANSCRIBED_SECONDS before it is sent, measured or not; the cost
is recorded from the pieces sent.

The data download (``/api/export/threads``): one at a time per account and
EXPORTS_PER_HOUR an hour.

Each test either fails on main (where nothing of this is checked) or is a
control, marked so.
"""
import io
import json
import math
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


KEY = "device-b-9876543210"
IOS_CHUNK_0 = (FIXTURES / "chunk_0000.mp4").read_bytes()


def _left_behind(app, user, session_id, quiet, chunks=2, status="recording"):
    """A recording with *chunks* chunks stored, the last of them *quiet* ago
    (an app paused, on a call, offline, or killed mid-recording). Its last
    sign of life is then too; ``quiet=None`` also released it."""
    since = datetime.utcnow() - (quiet or timedelta(minutes=1))
    draft, folder = _session(app, user, session_id, status=status)
    draft.created_at = since - timedelta(minutes=5)
    draft.streaming_heartbeat_at = None if quiet is None else since
    draft.streaming_device_key = KEY    # the device that recorded it
    if status == "finalizing":
        draft.streaming_total_chunks = chunks + 2
    for idx in range(chunks):
        _db.session.add(NodeTranscriptChunk(
            session_id=session_id, chunk_index=idx, status="stored",
            created_at=since))
        (folder / f"chunk_{idx:04d}.webm").write_bytes(FRAGMENT)
    _db.session.commit()
    return draft


def _empty(app, user, session_id, age):
    """A recording with no audio stored, started *age* ago."""
    draft, _ = _session(app, user, session_id)
    draft.created_at = datetime.utcnow() - age
    draft.streaming_heartbeat_at = draft.created_at
    _db.session.commit()
    return draft


def _init_recording(client, label=None):
    body = {"ai_usage": "chat"}
    if label:
        body["label"] = label
    return client.post("/api/drafts/streaming/init", json=body)


def _claim(client, session_id):
    return client.post(f"/api/drafts/streaming/{session_id}/claim",
                       json={"device_key": KEY})


def _recording_with_audio(client):
    """A recording started and holding its first chunk; its session id."""
    sid = _init_recording(client).get_json()["session_id"]
    assert _chunk(client, sid, 1, _payload(WEB_CHUNK_BYTES)).status_code == 202
    return sid


class TestOpenRecordings:
    def test_a_new_recording_waits_while_six_are_open(self, app, client,
                                                      alice):
        sids = [_recording_with_audio(client)
                for _ in range(limits.MAX_OPEN_RECORDINGS)]

        resp = _init_recording(client)

        # All six are recording (none offered for recovery): the words
        # say to wait, not to save or discard.
        assert resp.status_code == 429
        assert resp.get_json() == {
            "error": limits.RECORDINGS_IN_PROGRESS_TEXT,
            "code": "too_many_recordings"}
        # Discarding one lets the next one start.
        assert client.delete(
            f"/api/drafts/streaming/{sids[0]}/discard").status_code == 200
        assert _init_recording(client).status_code == 201

    def test_recordings_left_behind_with_audio_count_for_a_day(
            self, app, client, alice):
        for n in range(limits.MAX_OPEN_RECORDINGS):
            _left_behind(app, alice, f"left-{n}", timedelta(hours=20))

        resp = _init_recording(client)

        # Recovery offers them: the words say to save, finish or discard.
        assert resp.status_code == 429
        assert resp.get_json()["error"] == limits.TOO_MANY_RECORDINGS_TEXT

    def test_starts_without_audio_do_not_count(self, app, client, alice):
        # A microphone that failed to start, many times in a row.
        for _ in range(limits.MAX_OPEN_RECORDINGS + 4):
            assert _init_recording(client).status_code == 201

        assert _recording_with_audio(client)

    def test_control_after_a_day_or_without_audio_past_their_time_they_do_not_count(
            self, app, client, alice):
        # Control (passes on main too): a crashed app never holds a place
        # for longer than a day, a recording left before its first chunk
        # no longer than EMPTY_SESSION_EXPIRES_AFTER, and a finished one
        # not at all.
        for n in range(limits.MAX_OPEN_RECORDINGS):
            _left_behind(app, alice, f"old-{n}", timedelta(hours=25))
            _empty(app, alice, f"empty-{n}", timedelta(minutes=16))
            draft, _ = _session(app, alice, f"done-{n}", status="completed")

        assert _init_recording(client).status_code == 201

    def test_control_twenty_voice_turns_in_ten_minutes(
            self, app, client, alice, tasks):
        # Control (passes on main too): a turn frees its place as soon as
        # its last chunk is in, before its transcript.
        for turn in range(20):
            resp = _init_recording(client, label="Voice")
            assert resp.status_code == 201, (turn, resp.get_json())
            sid = resp.get_json()["session_id"]
            resp = client.post(
                f"/api/drafts/streaming/{sid}/audio-chunk",
                data={"chunk": (io.BytesIO(IOS_CHUNK_0), "chunk_0.mp4"),
                      "chunk_index": "0", "mime_type": "audio/mp4"},
                content_type="multipart/form-data")
            assert resp.status_code == 202, (turn, resp.get_json())
            resp = client.post(f"/api/drafts/streaming/{sid}/finalize",
                               json={"total_chunks": 1, "label": "Voice",
                                     "model": "gpt-5"})
            assert resp.status_code == 202, (turn, resp.get_json())
        assert tasks.finalize_draft_streaming.delay.call_count == 20


class TestAudioIsNeverRefusedForQuietOrTheLimit:
    """A recording that holds audio takes more whatever the gap, also at the
    monthly usage limit (#341): a pause, a phone call, time offline, an
    app killed and reopened later. Controls (they pass on main, where
    nothing of this was refused), except
    test_once_finalizing_only_the_announced_chunks_are_taken."""

    @pytest.mark.parametrize("quiet", [timedelta(minutes=11),
                                       timedelta(minutes=15),
                                       timedelta(hours=1),
                                       timedelta(hours=3), None])
    def test_after_a_gap_at_the_limit_the_rest_is_stored(
            self, app, client, alice, quiet):
        _left_behind(app, alice, "rec", quiet, chunks=4)
        _cap(alice)

        codes = [_chunk(client, "rec", i, _payload(WEB_CHUNK_BYTES)).status_code
                 for i in range(4, 10)]

        assert codes == [202] * 6
        assert _rows("rec") == 10

    def test_app_reopened_hours_later_at_the_limit_continues_and_finishes(
            self, app, client, alice, tasks):
        _left_behind(app, alice, "killed", timedelta(hours=3), chunks=4)
        _cap(alice)

        assert _chunk(client, "killed", 4,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202
        resp = _claim(client, "killed")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["next_chunk_index"] == 5
        assert _chunk(client, "killed", 5,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202
        resp = client.post("/api/drafts/streaming/killed/finalize",
                           json={"total_chunks": 6, "device_key": KEY})
        assert resp.status_code == 202, resp.get_data(as_text=True)

    def test_a_late_chunk_after_stop_at_the_limit_is_stored(
            self, app, client, alice):
        _left_behind(app, alice, "fin", timedelta(minutes=11), chunks=4,
                     status="finalizing")
        _cap(alice)

        assert _chunk(client, "fin", 4,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202

    def test_once_finalizing_only_the_announced_chunks_are_taken(
            self, app, client, alice):
        _left_behind(app, alice, "fin", None, chunks=4, status="finalizing")

        resp = _chunk(client, "fin", 6, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "chunk_index_out_of_range"

    def test_control_a_chunk_in_flight_when_the_page_left_is_stored(
            self, app, client, alice):
        # Control (passes on main too): the page left (release beacon) with
        # its first chunk still on the way.
        _session(app, alice, "rel")
        assert client.post(
            "/api/drafts/streaming/rel/release").status_code == 200
        _cap(alice)

        assert _chunk(client, "rel", 1,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202


class TestRecordingsWithoutAudio:
    def test_past_its_time_the_first_audio_is_a_new_start_at_the_limit(
            self, app, client, alice):
        _empty(app, alice, "late", timedelta(minutes=16))
        _cap(alice)

        resp = _chunk(client, "late", 0, _payload(WEB_CHUNK_BYTES))
        assert resp.status_code == 402
        assert resp.get_json()["error"] == "monthly_spend_limit_reached"
        assert _rows("late") == 0
        assert _claim(client, "late").status_code == 402

    def test_past_its_time_the_first_audio_must_fit_the_count(
            self, app, client, alice):
        _empty(app, alice, "late", timedelta(minutes=16))
        for n in range(limits.MAX_OPEN_RECORDINGS):
            _left_behind(app, alice, f"open-{n}", timedelta(hours=1))

        resp = _chunk(client, "late", 1, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 429
        assert _rows("late") == 0

    def test_control_within_its_time_it_takes_audio_at_the_limit(
            self, app, client, alice):
        # Control (passes on main too): a start the limit was checked for.
        _empty(app, alice, "fresh", timedelta(minutes=5))
        _cap(alice)

        assert _chunk(client, "fresh", 1,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202

    def test_control_past_its_time_under_the_limit_it_takes_audio(
            self, app, client, alice):
        # Control (passes on main too): started, then offline a long time
        # before the first chunk got through.
        _empty(app, alice, "late", timedelta(hours=2))

        assert _chunk(client, "late", 1,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202

    def test_starts_without_audio_are_bounded_on_their_own(
            self, app, client, alice):
        started = [_init_recording(client).get_json()["session_id"]
                   for _ in range(limits.MAX_EMPTY_STARTS)]

        resp = _init_recording(client)

        assert resp.status_code == 429
        assert resp.get_json() == {"error": limits.TOO_MANY_STARTS_TEXT,
                                   "code": "too_many_starts"}
        # Once their time has passed they no longer count.
        Draft.query.update({Draft.created_at: datetime.utcnow()
                            - limits.EMPTY_SESSION_EXPIRES_AFTER
                            - timedelta(minutes=1)})
        _db.session.commit()
        assert _init_recording(client).status_code == 201
        assert len(started) == limits.MAX_EMPTY_STARTS

    def test_prepared_recordings_cannot_get_past_the_count(
            self, app, client, alice):
        started = [_init_recording(client).get_json()["session_id"]
                   for _ in range(limits.MAX_OPEN_RECORDINGS + 3)]

        codes = [_chunk(client, sid, 1, _payload(WEB_CHUNK_BYTES)).status_code
                 for sid in started]

        assert codes == ([202] * limits.MAX_OPEN_RECORDINGS + [429] * 3)

    def test_a_recording_left_untouched_for_a_day_is_a_new_start(
            self, app, client, alice, tasks):
        _left_behind(app, alice, "old", timedelta(hours=25), chunks=4)
        _cap(alice)

        resp = _chunk(client, "old", 4, _payload(WEB_CHUNK_BYTES))
        assert resp.status_code == 402
        assert _claim(client, "old").status_code == 402
        assert _rows("old") == 4
        # Its audio is kept and can be saved.
        resp = client.post("/api/drafts/streaming/old/finalize",
                           json={"total_chunks": 4, "device_key": KEY})
        assert resp.status_code == 202, resp.get_data(as_text=True)

    def test_a_recording_left_untouched_for_a_day_must_fit_the_count(
            self, app, client, alice):
        _left_behind(app, alice, "older", timedelta(hours=26))
        for n in range(limits.MAX_OPEN_RECORDINGS):
            _left_behind(app, alice, f"open-{n}", timedelta(hours=1))

        resp = _chunk(client, "older", 2, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 429

    def test_control_under_the_limit_it_takes_audio_again(
            self, app, client, alice):
        _left_behind(app, alice, "old", timedelta(hours=25))

        assert _chunk(client, "old", 2,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202


class TestTheTranscriptionStreamIsNotActivity:
    """Opening the transcription stream marks a recording live for the
    takeover between devices; for the limits a recording is active through
    its start and its audio only."""

    @pytest.fixture(autouse=True)
    def _quick_stream(self, app, monkeypatch):
        # The stream's clock runs fast, so its first heartbeat comes at once
        # (it would come after 15 s). After ``app``: it imports the routes
        # afresh.
        import itertools
        import types
        sse = sys.modules["backend.routes.sse"]
        ticks = itertools.count()
        monkeypatch.setattr(sse, "time", types.SimpleNamespace(
            time=lambda: next(ticks) * 20.0, sleep=lambda s: None))

    def _open_stream(self, client, session_id):
        resp = client.get(f"/api/sse/drafts/{session_id}/transcription-stream"
                          f"?device_key={KEY}", buffered=False)
        resp.close()
        return resp.status_code

    def test_a_stream_does_not_keep_recordings_left_behind_open(
            self, app, client, alice):
        for n in range(limits.MAX_OPEN_RECORDINGS):
            _left_behind(app, alice, f"old-{n}", timedelta(hours=25))
            assert self._open_stream(client, f"old-{n}") == 200

        assert _init_recording(client).status_code == 201

    def test_a_stream_does_not_take_up_a_recording_left_behind(
            self, app, client, alice):
        _left_behind(app, alice, "old", timedelta(hours=25))
        for n in range(limits.MAX_OPEN_RECORDINGS):
            _left_behind(app, alice, f"open-{n}", timedelta(hours=1))
        assert self._open_stream(client, "old") == 200

        resp = _chunk(client, "old", 2, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 429
        assert resp.get_json()["code"] == "too_many_recordings"

    def test_control_a_stream_does_not_lengthen_a_recording_without_audio(
            self, app, client, alice):
        # Control: its time runs from its start.
        _empty(app, alice, "late", timedelta(minutes=16))
        assert self._open_stream(client, "late") == 200
        _cap(alice)

        resp = _chunk(client, "late", 0, _payload(WEB_CHUNK_BYTES))

        assert resp.status_code == 402

    def test_control_the_stream_still_marks_the_recording_live(
            self, app, client, alice):
        # Control (as on main): the takeover between devices reads it.
        draft = _left_behind(app, alice, "rec", timedelta(hours=2))

        assert self._open_stream(client, "rec") == 200

        _db.session.refresh(draft)
        assert draft.streaming_heartbeat_at > datetime.utcnow() - timedelta(
            minutes=1)


class TestBacklogInBatches:
    def test_transcribe_remaining_queues_batches_of_twenty(
            self, app, client, alice, tasks):
        _left_behind(app, alice, "back", None, chunks=45)

        resp = client.post("/api/drafts/streaming/back/transcribe-remaining")

        assert resp.status_code == 202, resp.get_data(as_text=True)
        sizes = [len(call.kwargs["chunk_indices"]) for call in
                 tasks.transcribe_chunk_batch.delay.call_args_list]
        assert sizes == [20, 20, 5]


class TestEmptyRecordingCleanup:
    """The daily clean-up (tasks/recording_cleanup) removes recordings that
    never received audio, a week old, and nothing else."""

    def _old(self, app, user, session_id, age=timedelta(days=8)):
        draft, folder = _session(app, user, session_id)
        draft.created_at = datetime.utcnow() - age
        _db.session.commit()
        return draft, folder

    def test_old_recordings_without_audio_are_removed(self, app, alice):
        _, folder = self._old(app, alice, "gone")

        removed = limits.remove_empty_recordings(root=app.media_root)

        assert removed == 1
        assert Draft.query.filter_by(session_id="gone").count() == 0
        assert not folder.exists()

    def test_nothing_with_audio_or_text_or_recent_is_touched(
            self, app, alice):
        _left_behind(app, alice, "audio", timedelta(days=8))
        Draft.query.filter_by(session_id="audio").one().created_at = (
            datetime.utcnow() - timedelta(days=9))
        _, folder = self._old(app, alice, "file")
        (folder / "init.webm").write_bytes(b"\x1aE\xdf\xa3")
        draft, _ = self._old(app, alice, "text")
        draft.set_content("typed words")
        self._old(app, alice, "recent", age=timedelta(days=1))
        done, _ = self._old(app, alice, "done")
        done.streaming_status = "completed"
        _db.session.commit()

        assert limits.remove_empty_recordings(root=app.media_root) == 0
        assert Draft.query.count() == 5
        assert (folder / "init.webm").exists()

    def test_a_run_removes_at_most_its_bound(self, app, alice):
        for n in range(5):
            self._old(app, alice, f"old-{n}")

        assert limits.remove_empty_recordings(root=app.media_root,
                                              limit=3) == 3
        assert Draft.query.count() == 2


# ── The usage limit at a start: audio not yet recorded as cost ─────────────

PRICE = {"gpt-4o-transcribe": {"price_per_minute_usd": 0.006}}


def _limit(app, user, usd, spent_usd=0.0):
    """A monthly limit of *usd*, with *spent_usd* already recorded."""
    from backend.models import APICostLog
    app.config["AUDIO_PRICING"] = PRICE
    user.monthly_spend_limit_usd = usd
    if spent_usd:
        _db.session.add(APICostLog(
            user_id=user.id, model_id="gpt-4o-transcribe",
            request_type="transcription",
            cost_microdollars=int(round(spent_usd * 1_000_000))))
    _db.session.commit()


def _cost(seconds):
    from backend.utils.cost import calculate_audio_cost_microdollars
    return calculate_audio_cost_microdollars("gpt-4o-transcribe", seconds)


def _voice_turn(client):
    """A Voice turn: started, one chunk, finalized. (status, session id)."""
    resp = _init_recording(client, label="Voice")
    if resp.status_code != 201:
        return resp.status_code, None
    sid = resp.get_json()["session_id"]
    resp = client.post(
        f"/api/drafts/streaming/{sid}/audio-chunk",
        data={"chunk": (io.BytesIO(IOS_CHUNK_0), "chunk_0.mp4"),
              "chunk_index": "0", "mime_type": "audio/mp4"},
        content_type="multipart/form-data")
    assert resp.status_code == 202, resp.get_json()
    resp = client.post(f"/api/drafts/streaming/{sid}/finalize",
                       json={"total_chunks": 1, "label": "Voice",
                             "model": "gpt-5"})
    assert resp.status_code == 202, resp.get_json()
    return 201, sid


def _transcribed(user, session_id, seconds=15.0):
    """What the transcription task commits at once for a turn: its chunk
    completed and its cost row."""
    from backend.models import APICostLog
    chunk = NodeTranscriptChunk.query.filter_by(session_id=session_id).one()
    chunk.status = "completed"
    chunk.audio_seconds = seconds
    _db.session.add(APICostLog(
        user_id=user.id, model_id="gpt-4o-transcribe",
        request_type="transcription", audio_duration_seconds=seconds,
        cost_microdollars=_cost(seconds)))
    _db.session.commit()


class TestUsageAtAStart:
    """A start counts the audio the account has stored or queued but not
    yet recorded as cost; a recording that holds audio is never refused for
    it."""

    def test_forty_starts_in_a_row_stop_at_the_limit(
            self, app, client, alice, tasks):
        _limit(app, alice, 0.10)

        codes = [_voice_turn(client)[0] for _ in range(40)]

        accepted = codes.count(201)
        assert codes == [201] * accepted + [402] * (40 - accepted)
        # Each turn waits for its transcript, counted at the most a chunk
        # is sent with until then: the starts stop at the limit, and what
        # they may send passes it by less than one recording's bound.
        per_turn = _cost(limits.MAX_CHUNK_AUDIO_SECONDS)
        assert accepted == -(-100_000 // per_turn)
        assert accepted * per_turn - 100_000 \
            < _cost(limits.MAX_TRANSCRIBED_SECONDS)

    def test_back_to_back_voice_turns_under_the_limit_are_counted_once(
            self, app, client, alice, tasks):
        # A person's turns, each transcribed before the next (the reply
        # needs the transcript), are never refused under the limit, and a
        # transcribed turn is not counted twice.
        _limit(app, alice, 0.10, spent_usd=0.05)

        for turn in range(20):
            code, sid = _voice_turn(client)
            assert code == 201, turn
            assert limits.pending_audio_seconds(alice.id) \
                == limits.MAX_CHUNK_AUDIO_SECONDS
            _transcribed(alice, sid)
            assert limits.pending_audio_seconds(alice.id) == 0

    def test_a_turn_still_waiting_for_its_transcript_counts_once(
            self, app, client, alice, tasks):
        _limit(app, alice, 0.10, spent_usd=0.05)
        assert _voice_turn(client)[0] == 201

        # The next start, before the transcript: counted at its estimate.
        assert _voice_turn(client)[0] == 201
        assert limits.pending_audio_seconds(alice.id) \
            == 2 * limits.MAX_CHUNK_AUDIO_SECONDS

    def test_an_upload_counts_from_its_start(self, app, client, alice):
        _limit(app, alice, 0.10)
        # 8 MB: about 1,000 s until measured, the whole limit.
        node_id = _init_upload(client, 8_000_000, 2).get_json()["node_id"]

        assert _init_recording(client).status_code == 402
        assert _init_upload(client, 8, 2, upload_id="up-2").status_code \
            == 402
        # Measured shorter by the transcription task.
        Node.query.get(node_id).audio_pending_seconds = 60.0
        _db.session.commit()
        assert _init_recording(client).status_code == 201

    def test_audio_with_its_cost_row_is_not_counted(self, app, client, alice):
        _limit(app, alice, 0.10)
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        Node.query.get(node_id).transcription_status = "completed"
        _db.session.commit()

        assert limits.pending_audio_seconds(alice.id) == 0

    def test_control_a_recording_with_audio_takes_more_past_the_limit(
            self, app, client, alice):
        # Control (#341): the pending audio never refuses a recording's
        # audio once it holds some.
        _limit(app, alice, 0.01)
        _left_behind(app, alice, "rec", timedelta(minutes=2), chunks=10)

        assert _chunk(client, "rec", 10,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202


class TestFirstAudioAtTheLimit:
    def test_control_the_recording_being_spoken_into_takes_its_first_audio(
            self, app, client, alice):
        # Control: a start, then the limit reached seconds later by the
        # previous turn's transcript, then its first chunk.
        sid = _init_recording(client).get_json()["session_id"]
        _cap(alice)

        assert _chunk(client, sid, 1,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202

    def test_only_the_newest_start_takes_its_first_audio_at_the_limit(
            self, app, client, alice):
        for n in range(5):
            _left_behind(app, alice, f"open-{n}", timedelta(hours=1))
        sids = [_init_recording(client).get_json()["session_id"]
                for _ in range(30)]
        _cap(alice)

        codes = []
        for sid in sids:
            resp = client.post(
                f"/api/drafts/streaming/{sid}/audio-chunk",
                data={"chunk": (io.BytesIO(IOS_CHUNK_0), "chunk_0.mp4"),
                      "chunk_index": "0", "mime_type": "audio/mp4"},
                content_type="multipart/form-data")
            codes.append(resp.status_code)
            if resp.status_code == 202:
                client.post(f"/api/drafts/streaming/{sid}/finalize",
                            json={"total_chunks": 1})

        assert codes == [402] * 29 + [202]


def _waiting_uploads(user, n, status="pending", activity=None, deleted=False,
                     handed=False):
    for _ in range(n):
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", transcription_status=status,
                    ai_usage="chat",
                    upload_activity_at=activity or datetime.utcnow())
        if handed:
            node.audio_original_url = "/media/user/x/original.m4a"
        if deleted:
            node.deleted_at = datetime.utcnow()
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

    def test_deleted_uploads_do_not_count(self, app, client, alice):
        _waiting_uploads(alice, limits.MAX_UPLOADS_IN_PROGRESS, deleted=True)

        assert _init_upload(client, 8, 2).status_code == 201

    def test_control_idle_failed_and_finished_ones_do_not_count(
            self, app, client, alice):
        # Control: uploads without activity for a day, failed ones and
        # transcribed ones.
        n = limits.MAX_UPLOADS_IN_PROGRESS
        _waiting_uploads(alice, n, activity=datetime.utcnow()
                         - limits.UPLOAD_COUNTS_FOR - timedelta(minutes=1))
        _waiting_uploads(alice, n, status="failed")
        _waiting_uploads(alice, n, status="completed")

        assert _init_upload(client, 8, 2).status_code == 201

    def test_ones_handed_to_transcription_count_until_their_transcript(
            self, app, client, alice):
        _waiting_uploads(alice, limits.MAX_UPLOADS_IN_PROGRESS, handed=True,
                         activity=datetime.utcnow() - timedelta(hours=2))

        assert _init_upload(client, 8, 2).status_code == 429

    def test_one_upload_left_behind_does_not_block(self, app, client, alice):
        # One upload stuck mid-way, as many presses as before.
        _waiting_uploads(alice, 1)
        for n in range(3):
            assert _init_upload(client, 8, 2,
                                upload_id=f"up-{n}").status_code == 201

    @pytest.mark.parametrize("asleep", [timedelta(minutes=31),
                                        timedelta(hours=5),
                                        timedelta(hours=23)])
    def test_an_upload_continues_after_the_device_slept(
            self, app, client, alice, asleep):
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        Node.query.get(node_id).upload_activity_at = (
            datetime.utcnow() - asleep)
        _db.session.commit()

        assert _send_piece(client, node_id, 1, b"efgh").status_code == 200

    def test_a_piece_after_a_day_without_activity_is_refused(
            self, app, client, alice):
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        Node.query.get(node_id).upload_activity_at = (
            datetime.utcnow() - limits.UPLOAD_COUNTS_FOR
            - timedelta(minutes=1))
        _db.session.commit()

        resp = _send_piece(client, node_id, 1, b"efgh")

        assert resp.status_code == 410
        assert resp.get_json() == {"error": limits.UPLOAD_EXPIRED_TEXT,
                                   "code": "upload_expired"}

    def test_control_a_slow_upload_keeps_going(self, app, client, alice):
        # Control (passes on main too): a piece every 25 minutes, for hours.
        node_id = _init_upload(client, 12, 3).get_json()["node_id"]
        for index, piece in enumerate((b"abcd", b"efgh", b"ijkl")):
            Node.query.get(node_id).upload_activity_at = (
                datetime.utcnow() - timedelta(minutes=25))
            Node.query.get(node_id).created_at = (
                datetime.utcnow() - timedelta(hours=3))
            _db.session.commit()
            assert _send_piece(client, node_id, index, piece).status_code == 200

    def test_an_older_path_recording_idle_for_a_day_takes_no_audio(
            self, app, client, alice, tasks):
        node = TestOlderNodePath()._node(app, alice)
        node.upload_activity_at = (datetime.utcnow()
                                   - limits.UPLOAD_COUNTS_FOR
                                   - timedelta(minutes=1))
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

    def test_control_every_upload_start_is_refused_at_the_limit(
            self, app, client, alice):
        # Control (as on main): the chunked init, a file in one request,
        # and the older node path.
        _cap(alice)

        assert _init_upload(client, 8, 2).status_code == 402
        assert client.post("/api/nodes/", data={
            "audio_file": (io.BytesIO(b"fake audio"), "talk.m4a"),
            "ai_usage": "chat"},
            content_type="multipart/form-data").status_code == 402
        assert client.post("/api/nodes/streaming/init",
                           json={"ai_usage": "chat"}).status_code == 402
        assert Node.query.count() == 0

    def test_an_upload_idle_for_a_day_is_not_finalized(
            self, app, client, alice, tasks):
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        assert _send_piece(client, node_id, 1, b"efgh").status_code == 200
        Node.query.get(node_id).upload_activity_at = (
            datetime.utcnow() - limits.UPLOAD_COUNTS_FOR
            - timedelta(minutes=1))
        _db.session.commit()

        resp = _finalize_upload(client, node_id)

        assert resp.status_code == 410
        assert resp.get_json()["code"] == "upload_expired"
        tasks.transcribe_audio.delay.assert_not_called()

    def test_an_upload_accepted_at_its_start_is_transcribed(
            self, app, client, alice, tasks, monkeypatch):
        # As a recording (#341): the limit reached while it was uploading.
        import backend.routes.nodes as nodes_routes
        monkeypatch.setattr(nodes_routes, "get_openai_chat_key",
                            lambda cfg: "key")
        node_id = _init_upload(client, 8, 2).get_json()["node_id"]
        assert _send_piece(client, node_id, 0, b"abcd").status_code == 200
        assert _send_piece(client, node_id, 1, b"efgh").status_code == 200
        _cap(alice)

        resp = _finalize_upload(client, node_id)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert "spend_capped" not in resp.get_json()
        node = Node.query.get(node_id)
        assert node.transcription_status == "pending"
        tasks.transcribe_audio.delay.assert_called_once()


class TestStartsAtOnce:
    """Two starts at the same moment: the count is made again under the
    account's lock, right before the insert (on SQLite the lock itself is a
    no-op; here it stands in for another start that committed meanwhile)."""

    def test_recordings_are_counted_again_under_the_lock(
            self, app, client, alice, monkeypatch):
        locked = []

        def others_started_meanwhile(user_id):
            locked.append(user_id)
            for n in range(limits.MAX_OPEN_RECORDINGS):
                _left_behind(app, alice, f"meanwhile-{n}",
                             timedelta(minutes=1))
        monkeypatch.setattr(limits, "lock_account", others_started_meanwhile)
        before = Draft.query.count()

        resp = _init_recording(client)

        assert locked == [alice.id]
        assert resp.status_code == 429
        assert Draft.query.count() == before + limits.MAX_OPEN_RECORDINGS

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
             "backend.tasks.transcription", "backend.tasks.llm_completion")
    saved = {k: sys.modules.get(k) for k in _GLUE}
    fake_celery_app = MagicMock()

    def _passthrough_task(*args, **kwargs):
        def deco(f):
            return f
        return deco
    fake_celery_app.celery.task = _passthrough_task
    sys.modules["backend.celery_app"] = fake_celery_app
    sys.modules.pop("backend.tasks.streaming_transcription", None)
    # The upload task too (the tr fixture), so it is imported again on the
    # stubbed celery app whatever imported it before.
    sys.modules.pop("backend.tasks.transcription", None)
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
    # What a container says (the tasks no longer read it).
    module._duration = 15.0
    monkeypatch.setattr(module, "get_audio_duration",
                        lambda p, log: module._duration, raising=False)
    # The decoded length (utils/audio_processing.decoded_audio_seconds): a
    # batch of 20 chunks of 15 s; None when it cannot be decoded.
    import backend.utils.audio_processing as audio_processing
    module._real_decoded = audio_processing.decoded_audio_seconds
    module._decoded = 300.0
    monkeypatch.setattr(audio_processing, "decoded_audio_seconds",
                        lambda p, *a, **k: module._decoded)
    monkeypatch.setattr(module, "decoded_audio_seconds",
                        lambda p, *a, **k: module._decoded, raising=False)
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
    # Follow-up runs of a batch (parts left for later): recorded, not queued.
    module._follow_ups = []

    def _follow_up(session_id, chunk_indices, follow_up=None):
        module._follow_ups.append((list(chunk_indices), follow_up))
        return f"follow-up-{len(module._follow_ups)}"
    monkeypatch.setattr(module, "_queue_follow_up", _follow_up, raising=False)

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


def _batch(st, session_id, indices, follow_up=0):
    if follow_up:
        st.transcribe_chunk_batch(MagicMock(), session_id, list(indices),
                                  follow_up=follow_up)
    else:
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
        st._decoded = 15.0
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


# ── The length, from the decoded audio ───────────────────────────────────

def _ffmpeg_with(*encoders):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True)
    return all(name in out.stdout for name in encoders)


def _sine(path, seconds, squeeze=None):
    """*seconds* of a tone in *path* (Opus in WebM); with *squeeze*, the
    same audio with timestamps that many times shorter."""
    import subprocess
    plain = path.with_name("plain-" + path.name) if squeeze else path
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-ac", "1",
                    "-c:a", "libopus", str(plain)], check=True)
    if squeeze:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(plain), "-c",
                        "copy", "-bsf:a", f"setts=ts=TS/{squeeze}", str(path)],
                       check=True)
    return path


def _container_seconds(path):
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout)


class TestDecodedLength:
    """The length bound and the transcription cost use the length of the
    decoded audio, not the container's timestamps; audio that cannot be
    decoded counts as the most its bytes can hold."""

    def test_the_cost_and_the_bound_use_the_decoded_length(self, st):
        from backend.models import APICostLog
        st._duration = 15.0      # what the container says
        st._decoded = 300.0      # what the audio holds
        _recording(st, "sess-d", range(0, 20))

        _batch(st, "sess-d", range(0, 20))

        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-d", chunk_index=0).one()
        assert head.audio_seconds == 300.0
        assert APICostLog.query.one().audio_duration_seconds == 300.0

    def test_a_short_turn_counts_its_measured_length(self, st):
        from backend.models import APICostLog
        st._decoded = 5.0        # a five-second Voice turn
        _recording(st, "sess-s", range(0, 1))

        _batch(st, "sess-s", range(0, 1))

        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-s", chunk_index=0).one()
        assert head.audio_seconds == 5.0
        assert APICostLog.query.one().audio_duration_seconds == 5.0

    def test_chunks_that_decode_past_their_bound_are_not_sent(
            self, st, monkeypatch):
        from backend.models import APICostLog
        reencoded = []
        monkeypatch.setattr(st, "split_audio_for_transcription",
                            lambda *a, **k: reencoded.append(a) or ([], False))
        st._decoded = math.inf   # the decode stopped at the bound
        _recording(st, "sess-x", range(0, 2))

        _batch(st, "sess-x", range(0, 2))

        assert st._provider.call_count == 0
        assert reencoded == []
        assert APICostLog.query.count() == 0
        kept = NodeTranscriptChunk.query.filter_by(session_id="sess-x").all()
        assert {limits.untranscribed_code(c.error) for c in kept} \
            == {limits.INVALID}
        st.finalize_draft_streaming(MagicMock(), "sess-x", 2)
        assert Draft.query.filter_by(session_id="sess-x").one() \
            .get_content().endswith(limits.NOTE_INVALID)

    @pytest.mark.skipif(not _ffmpeg_with("libopus"),
                        reason="needs ffmpeg with libopus")
    def test_a_chunk_of_minutes_of_quiet_is_refused_without_decoding_it_all(
            self, st, monkeypatch):
        import subprocess
        import time
        import backend.utils.audio_processing as audio_processing
        # Ten minutes of quiet in one chunk: small, and far more audio than
        # a chunk holds.
        _, folder = _recording(st, "sess-q", range(0, 1))
        quiet = folder / "chunk_0000.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=48000:cl=mono", "-t", "600", "-c:a",
                        "libopus", "-b:a", "6k", "-application", "voip",
                        "-dtx", "1", str(quiet)], check=True)
        monkeypatch.setattr(audio_processing, "decoded_audio_seconds",
                            st._real_decoded)
        monkeypatch.setattr(st, "decoded_audio_seconds", st._real_decoded)
        reencoded = []
        monkeypatch.setattr(st, "split_audio_for_transcription",
                            lambda *a, **k: reencoded.append(a) or ([], False))

        started = time.monotonic()
        _batch(st, "sess-q", range(0, 1))

        assert time.monotonic() - started < 5
        assert st._provider.call_count == 0
        assert reencoded == []
        chunk = NodeTranscriptChunk.query.filter_by(session_id="sess-q").one()
        assert limits.untranscribed_code(chunk.error) == limits.INVALID

    def test_audio_that_cannot_be_read_is_kept_and_not_sent(self, st):
        from backend.models import APICostLog
        st._duration = 15.0      # what the container says
        st._decoded = None       # neither measured nor re-encoded
        _, folder = _recording(st, "sess-u", range(0, 20))

        _batch(st, "sess-u", range(0, 20))

        assert st._provider.call_count == 0
        assert APICostLog.query.count() == 0
        kept = NodeTranscriptChunk.query.filter_by(session_id="sess-u").all()
        assert {c.status for c in kept} == {"failed"}
        assert {limits.untranscribed_code(c.error) for c in kept} \
            == {limits.UNREADABLE}
        assert any(p.name.startswith("batch_") for p in folder.iterdir())

        st.finalize_draft_streaming(MagicMock(), "sess-u", 20)
        content = Draft.query.filter_by(session_id="sess-u").one() \
            .get_content()
        assert content.endswith(limits.NOTE_UNREADABLE)
        assert "failed to transcribe" not in content

    @pytest.mark.skipif(not _ffmpeg_with("libopus", "libmp3lame"),
                        reason="needs ffmpeg with libopus, libmp3lame")
    def test_audio_that_cannot_be_measured_is_re_encoded_and_counted(
            self, st, monkeypatch):
        import subprocess
        import backend.utils.audio_processing as audio_processing
        from backend.models import APICostLog
        # A minute of quiet at the lowest bitrate: fewer bytes a second than
        # any rate an estimate could assume.
        _, folder = _recording(st, "sess-q", range(0, 2))
        quiet = folder / "chunk_0000.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=48000:cl=mono", "-t", "60", "-c:a",
                        "libopus", "-b:a", "6k", "-application", "voip",
                        "-dtx", "1", str(quiet)], check=True)
        assert quiet.stat().st_size < 60 * 6000 // 8
        (folder / "chunk_0001.webm").write_bytes(b"")

        def _decoded(p, *a, **k):
            # Measuring the merged file fails (or takes too long).
            if pathlib.Path(p).name.startswith("merged-"):
                return None
            return st._real_decoded(p, *a, **k)
        monkeypatch.setattr(audio_processing, "decoded_audio_seconds",
                            _decoded)
        monkeypatch.setattr(st, "decoded_audio_seconds", _decoded,
                            raising=False)

        _batch(st, "sess-q", range(0, 2))

        assert st._provider.call_count == 1
        sent_as = st._provider.call_args.kwargs["file"][0]
        assert sent_as == "audio.mp3"            # the re-encoded audio
        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-q", chunk_index=0).one()
        assert head.audio_seconds == pytest.approx(60, abs=0.2)
        assert APICostLog.query.one().audio_duration_seconds \
            == pytest.approx(60, abs=0.2)

    def test_measured_audio_replaces_the_estimate_while_it_is_sent(self, st):
        seen = []
        words = type("R", (), {"text": "words"})()

        def provider(**kwargs):
            seen.append(limits.pending_audio_seconds(st._user.id))
            return words
        st._provider.side_effect = provider
        st._decoded = 75.0
        _recording(st, "sess-pm", range(0, 20))
        assert limits.pending_audio_seconds(st._user.id) \
            == 20 * limits.MAX_CHUNK_AUDIO_SECONDS

        _batch(st, "sess-pm", range(0, 20))

        assert seen == [75.0]
        assert limits.pending_audio_seconds(st._user.id) == 0

    def test_audio_sent_before_a_failure_is_recorded(self, st):
        from backend.models import APICostLog
        _, folder = _recording(st, "sess-f", range(0, 10))
        # A resume at chunk 5: two sub-batches, two requests.
        (folder / "init.5.webm").write_bytes(b"\x1aE\xdf\xa3-webm-head")
        st._decoded = 75.0
        words = type("R", (), {"text": "words"})()
        st._provider.side_effect = [words, RuntimeError("provider")]

        with pytest.raises(RuntimeError):
            _batch(st, "sess-f", range(0, 10))

        assert st._provider.call_count == 2
        assert sum(row.audio_duration_seconds
                   for row in APICostLog.query) == 150.0

    def test_audio_that_cannot_be_measured_is_held_to_the_length_limit(
            self, st):
        _recording(st, "sess-n", range(0, 20))
        _batch(st, "sess-n", range(0, 20))
        head = NodeTranscriptChunk.query.filter_by(
            session_id="sess-n", chunk_index=0).one()
        head.audio_seconds = limits.MAX_TRANSCRIBED_SECONDS - 60
        _db.session.commit()
        st._duration = 15.0      # the container claims a short batch
        st._decoded = None
        _, folder = _recording(st, "sess-n", range(20, 40))
        for idx in range(20, 40):
            (folder / f"chunk_{idx:04d}.webm").write_bytes(
                _payload(WEB_CHUNK_BYTES))

        _batch(st, "sess-n", range(20, 40))

        statuses = _statuses("sess-n")
        assert {statuses[i] for i in range(20, 40)} == {"failed"}
        assert st._provider.call_count == 1

    @pytest.mark.skipif(not _ffmpeg_with("libopus"),
                        reason="needs ffmpeg with libopus")
    def test_decoded_seconds_count_the_audio_not_the_timestamps(self, tmp_path):
        from backend.utils.audio_processing import decoded_audio_seconds
        plain = _sine(tmp_path / "plain.webm", 20)
        moved = _sine(tmp_path / "moved.webm", 20, squeeze=20)
        assert _container_seconds(moved) < 2

        assert abs(decoded_audio_seconds(plain) - 20) < 0.1
        assert abs(decoded_audio_seconds(moved) - 20) < 0.1
        # The iPhone app's own fMP4 output.
        assert abs(decoded_audio_seconds(FIXTURES / "chunk_0000.mp4")
                   - 2.07) < 0.05

    @pytest.mark.skipif(not _ffmpeg_with("libopus"),
                        reason="needs ffmpeg with libopus")
    def test_the_decode_stops_past_its_bound(self, tmp_path):
        import subprocess
        import time
        from backend.utils.audio_processing import decoded_audio_seconds
        quiet = tmp_path / "quiet.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=48000:cl=mono", "-t", "1200", "-c:a",
                        "libopus", "-b:a", "6k", "-application", "voip",
                        "-dtx", "1", str(quiet)], check=True)

        started = time.monotonic()
        assert decoded_audio_seconds(quiet, stop_after=60) == math.inf
        assert time.monotonic() - started < 2
        assert decoded_audio_seconds(quiet, stop_after=1500) \
            == pytest.approx(1200, abs=0.1)

    def test_undecodable_bytes_give_none(self, tmp_path):
        from backend.utils.audio_processing import decoded_audio_seconds
        junk = tmp_path / "junk.webm"
        junk.write_bytes(_payload(WEB_CHUNK_BYTES))

        assert decoded_audio_seconds(junk) is None


def _parts(st, session_id, parts=3, size=5):
    """A recording of *parts* resumed parts of *size* chunks each: one
    batch of several sub-batches."""
    _, folder = _recording(st, session_id, range(0, parts * size))
    for start in range(size, parts * size, size):
        (folder / f"init.{start}.webm").write_bytes(
            b"\x1aE\xdf\xa3-webm-head")
    return folder


def _fake_clock(st, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(st, "_now", lambda: clock["t"], raising=False)
    return clock


def _run_with_follow_ups(st, session_id, indices, most=10, follow_up=0):
    """Run the batch, then each follow-up run it asks for; the runs."""
    runs = [list(indices)]
    _batch(st, session_id, indices, follow_up)
    while st._follow_ups:
        indices, follow_up = st._follow_ups.pop(0)
        runs.append(indices)
        assert len(runs) <= most
        _batch(st, session_id, indices, follow_up)
    return runs


class TestAudioWorkBudget:
    """A task run's local audio work (merge, measure, re-encode) has one
    budget; the time spent waiting for the provider is not counted. Parts
    not reached in it go to a follow-up run; only a part that cannot be done
    within a whole budget is kept as unreadable."""

    def test_a_slow_provider_does_not_cost_the_rest_of_the_recording(
            self, st, monkeypatch):
        clock = _fake_clock(st, monkeypatch)
        words = type("R", (), {"text": "words"})()

        def slow_provider(**kwargs):
            clock["t"] += 21 * 60      # the first request takes 21 minutes
            return words
        st._provider.side_effect = slow_provider
        st._decoded = 75.0
        _parts(st, "sess-p", parts=2)

        _batch(st, "sess-p", range(0, 10))

        assert set(_statuses("sess-p").values()) == {"completed"}
        assert st._provider.call_count == 2
        assert st._follow_ups == []

    def test_control_parts_not_reached_go_to_a_follow_up_run(
            self, st, monkeypatch):
        from backend.models import APICostLog
        clock = _fake_clock(st, monkeypatch)
        budget = getattr(st, "TASK_AUDIO_BUDGET_SECONDS", 20 * 60)
        measuring = 0.6 * budget       # each part measures slowly, but well

        def slow_decode(p, timeout_seconds=None, logger=None, **k):
            spent = min(measuring, timeout_seconds or measuring)
            clock["t"] += spent
            return 75.0 if spent >= measuring else None
        monkeypatch.setattr(st, "decode_timeout_seconds",
                            lambda p: budget, raising=False)
        monkeypatch.setattr(st, "decoded_audio_seconds", slow_decode)
        _parts(st, "sess-l", parts=3)

        runs = _run_with_follow_ups(st, "sess-l", range(0, 15))

        assert runs == [list(range(0, 15)), list(range(5, 15)),
                        list(range(10, 15))]
        assert set(_statuses("sess-l").values()) == {"completed"}
        assert st._provider.call_count == 3
        assert sum(r.audio_duration_seconds
                   for r in APICostLog.query) == 3 * 75.0

    def test_a_slow_decoder_cannot_hold_a_run_past_its_budget(
            self, st, monkeypatch):
        from backend.models import APICostLog
        clock = _fake_clock(st, monkeypatch)
        calls = []

        def slow_decode(p, timeout_seconds=None, logger=None, **k):
            # Runs to its limit, then fails.
            spent = 600.0 if timeout_seconds is None else timeout_seconds
            calls.append(("decode", spent))
            clock["t"] += spent
            return None

        def slow_split(path, piece_seconds, limit_seconds,
                       timeout_seconds=1800, logger=None, deadline=None):
            calls.append(("re-encode", timeout_seconds))
            clock["t"] += timeout_seconds
            return [], False

        monkeypatch.setattr(st, "decode_timeout_seconds", lambda p: 600.0,
                            raising=False)
        monkeypatch.setattr(st, "decoded_audio_seconds", slow_decode)
        monkeypatch.setattr(st, "split_audio_for_transcription", slow_split)
        merges = []
        merge = st.concat_fragmented_media

        def counted_merge(paths, **kwargs):
            merges.append(len(paths))
            return merge(paths, **kwargs)
        monkeypatch.setattr(st, "concat_fragmented_media", counted_merge)
        folder = _parts(st, "sess-slow", parts=3)
        budget = getattr(st, "TASK_AUDIO_BUDGET_SECONDS", 20 * 60)
        per_chunk = getattr(st, "REENCODE_SECONDS_PER_CHUNK", 30)

        _batch(st, "sess-slow", range(0, 15))

        # The first run: the first part fails within its own limits; the
        # second runs out of the budget and is left, with the third.
        assert clock["t"] <= budget
        assert all(spent <= 5 * per_chunk
                   for step, spent in calls if step == "re-encode")
        assert len(calls) == 3
        assert merges == [5, 5]
        assert st._follow_ups == [(list(range(5, 15)), 1)]
        statuses = _statuses("sess-slow")
        assert {statuses[i] for i in range(0, 5)} == {"failed"}
        assert {statuses[i] for i in range(5, 15)} == {"stored"}
        # The parts left are each done in a run of their own.
        indices, follow_up = st._follow_ups.pop(0)
        runs = 1 + len(_run_with_follow_ups(st, "sess-slow", indices,
                                            follow_up=follow_up))
        assert runs == 3
        assert st._provider.call_count == 0
        assert APICostLog.query.count() == 0
        kept = NodeTranscriptChunk.query.filter_by(
            session_id="sess-slow").all()
        assert {limits.untranscribed_code(c.error) for c in kept} \
            == {limits.UNREADABLE}
        assert any(p.name.startswith("batch_") for p in folder.iterdir())

    def test_control_a_long_resumed_batch_on_a_slow_worker_finishes(
            self, st, monkeypatch):
        # Control: a batch of 20 chunks, each begun by a resume (20 parts),
        # on a worker so slow that measuring one part takes an eighth of a
        # run, finishes within the follow-up runs allowed.
        clock = _fake_clock(st, monkeypatch)
        budget = getattr(st, "TASK_AUDIO_BUDGET_SECONDS", 20 * 60)

        def slow_decode(p, timeout_seconds=None, logger=None, **k):
            spent = budget / 8
            if timeout_seconds is not None and timeout_seconds < spent:
                clock["t"] += timeout_seconds
                return None
            clock["t"] += spent
            return 15.0
        monkeypatch.setattr(st, "decode_timeout_seconds", lambda p: budget,
                            raising=False)
        monkeypatch.setattr(st, "decoded_audio_seconds", slow_decode)
        _parts(st, "sess-r", parts=20, size=1)

        runs = _run_with_follow_ups(st, "sess-r", range(0, 20))

        assert len(runs) <= 1 + getattr(st, "MAX_FOLLOW_UP_RUNS", 3)
        assert set(_statuses("sess-r").values()) == {"completed"}
        assert st._provider.call_count == 20

    def test_follow_up_runs_stop_after_their_bound(self, st, monkeypatch):
        # Every part runs to the end of a whole budget and fails.
        clock = _fake_clock(st, monkeypatch)
        budget = getattr(st, "TASK_AUDIO_BUDGET_SECONDS", 20 * 60)

        def endless_decode(p, timeout_seconds=None, logger=None, **k):
            clock["t"] += budget if timeout_seconds is None \
                else timeout_seconds
            return None
        monkeypatch.setattr(st, "decode_timeout_seconds", lambda p: budget,
                            raising=False)
        monkeypatch.setattr(st, "decoded_audio_seconds", endless_decode)
        monkeypatch.setattr(st, "split_audio_for_transcription",
                            lambda *a, **k: ([], False))
        _parts(st, "sess-e", parts=10, size=1)

        runs = _run_with_follow_ups(st, "sess-e", range(0, 10), most=50)

        bound = getattr(st, "MAX_FOLLOW_UP_RUNS", None)
        assert bound is not None
        assert len(runs) == 1 + bound
        kept = NodeTranscriptChunk.query.filter_by(session_id="sess-e").all()
        assert {c.status for c in kept} == {"failed"}
        assert {limits.untranscribed_code(c.error) for c in kept} \
            == {limits.UNREADABLE}
        assert st._provider.call_count == 0

    def test_a_batch_is_worked_on_in_at_most_its_parts(self, st):
        # More parts than a batch of the routes can have (an older queued
        # task, say): the rest goes to the follow-up run.
        st._decoded = 15.0
        n = getattr(st, "MAX_PARTS_PER_BATCH", 20)
        _parts(st, "sess-m", parts=n + 5, size=1)

        _batch(st, "sess-m", range(0, n + 5))

        assert st._provider.call_count == n
        assert st._follow_ups == [(list(range(n, n + 5)), 1)]

    def test_a_merge_gets_only_the_time_left(self, tmp_path):
        import subprocess
        import time
        from backend.utils import webm_utils
        part = tmp_path / "chunk_0000.webm"
        part.write_bytes(b"\x1aE\xdf\xa3-webm-head")
        token = webm_utils.audio_work_deadline.set(time.monotonic() - 1)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                webm_utils.concat_fragmented_media([str(part)],
                                                   output_suffix=".webm")
        finally:
            webm_utils.audio_work_deadline.reset(token)


@pytest.fixture
def tr(st, monkeypatch):
    """backend.tasks.transcription (uploaded files) on the st app, with the
    provider stood in. The uploaded file's decoded length is ``_decoded``
    (None: it cannot be measured); other files (the pieces sent) are
    measured for real, unless ``_pieces_measured`` is False."""
    import backend.tasks.transcription as module
    import backend.utils.audio_processing as audio_processing
    real_decoded = st._real_decoded
    module.flask_app = st.flask_app
    monkeypatch.setattr(module, "compress_audio_if_needed", lambda p, log: p)
    monkeypatch.setattr(module, "get_audio_duration", lambda p, log: 15.0,
                        raising=False)
    monkeypatch.setattr(module, "get_openai_chat_key", lambda cfg: "key")
    module._decoded = None
    module._upload_path = None
    module._pieces_measured = True

    def _decoded(p, *a, **k):
        if str(p) == str(module._upload_path):
            return module._decoded
        return real_decoded(p, *a, **k) if module._pieces_measured else None
    monkeypatch.setattr(audio_processing, "decoded_audio_seconds", _decoded)
    monkeypatch.setattr(module, "decoded_audio_seconds", _decoded,
                        raising=False)
    fake_openai = MagicMock()
    fake_openai.return_value.audio.transcriptions.create.return_value = (
        type("R", (), {"text": "uploaded words"})())
    monkeypatch.setattr(module, "OpenAI", fake_openai)
    module._provider = fake_openai.return_value.audio.transcriptions.create
    return module


def _uploaded(st, tr, name="upload.m4a", audio=None):
    node = Node(user_id=st._user.id, human_owner_id=st._user.id,
                node_type="user", transcription_status="pending",
                ai_usage="chat")
    node.set_content("[Voice note – transcription pending]")
    _db.session.add(node)
    _db.session.commit()
    path = audio or st._root / name
    if audio is None:
        path.write_bytes(b"fake audio")
    tr._upload_path = path
    return node, path


@pytest.fixture
def short_bound(tr, monkeypatch):
    """The upload bound at 25 s in pieces of 10 s (sent whole up to 10 s),
    so a 30-second file stands for one past eight hours."""
    monkeypatch.setattr(tr, "MAX_UPLOAD_TRANSCRIBED_SECONDS", 25,
                        raising=False)
    monkeypatch.setattr(tr, "CHUNK_DURATION_SEC", 10, raising=False)
    monkeypatch.setattr(tr, "OPENAI_MAX_DURATION_SEC", 10)
    return tr


def _cost_seconds():
    from backend.models import APICostLog
    return sum(row.audio_duration_seconds for row in APICostLog.query)


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_with("libopus", "libmp3lame"),
                                  reason="needs ffmpeg with libopus, libmp3lame")


class TestUploadedFileLength:
    @needs_ffmpeg
    def test_a_file_past_the_bound_is_transcribed_to_it_with_a_note(
            self, st, short_bound):
        tr = short_bound
        node, path = _uploaded(st, tr, audio=_sine(st._root / "long.webm", 30))
        tr._decoded = 30.0

        tr.transcribe_audio(MagicMock(), node.id, str(path), "long.webm")

        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert tr._provider.call_count == 3      # 10 + 10 + 5 s
        assert _cost_seconds() == pytest.approx(25, abs=0.2)
        assert node.get_content().endswith(limits.UPLOAD_NOTE_PART)
        assert path.exists()                     # the whole file is kept

    @needs_ffmpeg
    def test_the_bound_holds_when_the_length_cannot_be_measured(
            self, st, short_bound):
        tr = short_bound
        # 30 s of audio whose container says 1.5 s; decoding it for the
        # length fails (or takes too long).
        audio = _sine(st._root / "squeezed.webm", 30, squeeze=20)
        assert _container_seconds(audio) < 2
        node, path = _uploaded(st, tr, audio=audio)
        tr._decoded = None

        tr.transcribe_audio(MagicMock(), node.id, str(path), "squeezed.webm")

        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert tr._provider.call_count == 3
        assert _cost_seconds() == pytest.approx(25, abs=0.2)
        assert node.get_content().endswith(limits.UPLOAD_NOTE_PART)

    @needs_ffmpeg
    def test_pieces_that_cannot_be_measured_count_at_their_most(
            self, st, short_bound):
        tr = short_bound
        node, path = _uploaded(st, tr, audio=_sine(st._root / "f.webm", 30))
        tr._decoded = None
        tr._pieces_measured = False

        tr.transcribe_audio(MagicMock(), node.id, str(path), "f.webm")

        # Three pieces, each counted as the most a piece can be.
        assert tr._provider.call_count == 3
        assert _cost_seconds() == pytest.approx(30)

    @needs_ffmpeg
    def test_audio_sent_before_a_failure_is_recorded(self, st, short_bound):
        tr = short_bound
        node, path = _uploaded(st, tr, audio=_sine(st._root / "f.webm", 20))
        tr._decoded = 20.0
        words = type("R", (), {"text": "words"})()
        tr._provider.side_effect = [words, RuntimeError("provider")]

        with pytest.raises(RuntimeError):
            tr.transcribe_audio(MagicMock(), node.id, str(path), "f.webm")

        _db.session.refresh(node)
        assert node.transcription_status == "failed"
        # Both pieces were sent; the failed request is counted too.
        assert _cost_seconds() == pytest.approx(20, abs=0.2)
        assert path.exists()

    @needs_ffmpeg
    def test_control_a_file_under_the_bound_is_transcribed_in_full(
            self, st, short_bound):
        # Control (passes on main too).
        tr = short_bound
        node, path = _uploaded(st, tr, audio=_sine(st._root / "s.webm", 20))
        tr._decoded = 20.0

        tr.transcribe_audio(MagicMock(), node.id, str(path), "s.webm")

        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert _cost_seconds() == pytest.approx(20, abs=0.2)
        assert "*Note" not in node.get_content()

    def test_the_cost_uses_the_decoded_length(self, st, tr):
        node, path = _uploaded(st, tr)
        tr._decoded = 600.0

        tr.transcribe_audio(MagicMock(), node.id, str(path), "upload.m4a")

        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert tr._provider.call_count == 1
        assert _cost_seconds() == 600.0

    def test_a_file_that_cannot_be_decoded_is_kept_and_not_sent(self, st, tr):
        node, path = _uploaded(st, tr)
        tr._decoded = None

        with pytest.raises(Exception):
            tr.transcribe_audio(MagicMock(), node.id, str(path), "upload.m4a")

        _db.session.refresh(node)
        assert node.transcription_status == "failed"
        assert tr._provider.call_count == 0
        assert _cost_seconds() == 0
        assert path.exists()
