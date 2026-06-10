"""Tests for resumed-recording subsession handling (#124).

A recording resumed after a refresh comes from a fresh MediaRecorder
whose stream is not byte-concat-compatible with earlier chunks. These
tests cover the pieces that make the fix: init-bearing detection,
suffixed init persistence, and sub-batch partitioning/init resolution
inside transcribe_chunk_batch (exercised with synthetic WebM/MP4-shaped
fixtures and a mocked transcription API — no ffmpeg, no OpenAI).
"""
import os
import pathlib
import sys
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.utils.webm_utils import (  # noqa: E402
    WEBM_MAGIC, chunk_is_init_bearing, init_segment_name,
)

# fMP4 chunk head: [4-byte size]["ftyp"]...; WebM: EBML magic first.
MP4_INIT_HEAD = b"\x00\x00\x00\x20ftypisom-rest-of-box"
WEBM_INIT_HEAD = WEBM_MAGIC + b"\x9fB\x86\x81\x01-rest"
FRAGMENT_HEAD = b"\x00\x00\x01\x10moofdata-no-init-here"


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ── Detection ────────────────────────────────────────────────────────────

def test_detects_mp4_and_webm_init_chunks(tmp_path):
    assert chunk_is_init_bearing(_write(tmp_path, "a.mp4", MP4_INIT_HEAD))
    assert chunk_is_init_bearing(_write(tmp_path, "a.webm", WEBM_INIT_HEAD))


def test_fragment_chunks_not_init_bearing(tmp_path):
    assert not chunk_is_init_bearing(
        _write(tmp_path, "b.mp4", FRAGMENT_HEAD))
    assert not chunk_is_init_bearing(_write(tmp_path, "tiny.webm", b"abc"))
    assert not chunk_is_init_bearing(tmp_path / "missing.webm")


def test_init_segment_name_suffixing():
    assert init_segment_name(".webm") == "init.webm"
    assert init_segment_name(".mp4", 7) == "init.7.mp4"


# ── Batch partitioning inside transcribe_chunk_batch ─────────────────────

def _run_batch(tmp_path, monkeypatch, chunk_indices, boundary_indices,
               ext=".webm"):
    """Run transcribe_chunk_batch against synthetic fixtures.

    Returns (concat_calls, stored_text) where concat_calls captures
    (paths, init_segment_path) per sub-batch merge.
    """
    from flask import Flask
    from backend.extensions import db as _db
    from backend.models import User, Draft, NodeTranscriptChunk

    _GLUE = ("backend.celery_app", "backend.tasks.streaming_transcription")
    saved = {k: sys.modules.get(k) for k in _GLUE}
    fake_celery_app = MagicMock()

    # @celery.task(...) must return the real function, not a MagicMock,
    # so the test can invoke the task body directly.
    def _passthrough_task(*args, **kwargs):
        def deco(f):
            return f
        return deco
    fake_celery_app.celery.task = _passthrough_task

    sys.modules["backend.celery_app"] = fake_celery_app
    sys.modules.pop("backend.tasks.streaming_transcription", None)
    import backend.tasks.streaming_transcription as st

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    fake_celery_app.flask_app = app
    st.flask_app = app

    session_id = "sess-124"
    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.flush()
        draft = Draft(user_id=user.id, session_id=session_id,
                      streaming_status='recording',
                      streaming_mime_type=(
                          "audio/mp4" if ext == ".mp4" else "audio/webm"))
        draft.set_content("")
        _db.session.add(draft)
        for idx in chunk_indices:
            chunk = NodeTranscriptChunk(
                session_id=session_id, chunk_index=idx, status='stored')
            _db.session.add(chunk)
        _db.session.commit()

        # Fixture files: plaintext chunks + suffixed inits at boundaries
        chunk_dir = tmp_path / f"drafts/{user.id}/{session_id}"
        chunk_dir.mkdir(parents=True)
        for idx in chunk_indices:
            head = (MP4_INIT_HEAD if ext == ".mp4" else WEBM_INIT_HEAD) \
                if (idx == 0 or idx in boundary_indices) else FRAGMENT_HEAD
            (chunk_dir / f"chunk_{idx:04d}{ext}").write_bytes(
                head + b"payload%d" % idx)
        (chunk_dir / f"init{ext}").write_bytes(b"INIT0")
        for b in boundary_indices:
            (chunk_dir / f"init.{b}{ext}").write_bytes(b"INIT%d" % b)

        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))

        concat_calls = []

        def fake_concat(paths, init_segment_path=None, output_suffix=None):
            concat_calls.append((list(paths), init_segment_path))
            out = tmp_path / f"merged_{len(concat_calls)}{output_suffix}"
            out.write_bytes(b"merged")
            return str(out)

        monkeypatch.setattr(st, "concat_fragmented_media", fake_concat)
        monkeypatch.setattr(st, "compress_audio_if_needed",
                            lambda p, log: p)
        monkeypatch.setattr(st, "get_audio_duration", lambda p, log: 15.0)
        monkeypatch.setattr(st, "get_openai_chat_key", lambda cfg: "key")

        class FakeTranscriptions:
            def __init__(self):
                self.n = 0

            def create(self, **kwargs):
                self.n += 1
                return type("R", (), {"text": f"part{self.n}"})()

        fake_openai = MagicMock()
        fake_openai.return_value.audio.transcriptions = FakeTranscriptions()
        monkeypatch.setattr(st, "OpenAI", fake_openai)

        # Call the task body directly (bind=True → self first)
        st.transcribe_chunk_batch(MagicMock(), session_id,
                                  list(chunk_indices))

        stored = NodeTranscriptChunk.query.filter_by(
            session_id=session_id,
            chunk_index=min(chunk_indices)).first()
        stored_text = stored.get_text() if stored else None
        _db.session.rollback()
        _db.drop_all()

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    return concat_calls, stored_text


def test_resumed_batch_splits_at_boundary(tmp_path, monkeypatch):
    concat_calls, stored = _run_batch(
        tmp_path, monkeypatch,
        chunk_indices=[0, 1, 2, 3], boundary_indices=[2])
    # Two independent merges: [0,1] with no init (chunk 0 self-carries),
    # [2,3] with the suffixed subsession init.
    assert len(concat_calls) == 2
    (paths1, init1), (paths2, init2) = concat_calls
    assert len(paths1) == 2 and init1 is None
    assert len(paths2) == 2 and init2 is not None
    assert "init.2" in init2
    # Both subsessions' transcripts present, in order
    assert stored == "part1\n\npart2"


def test_unresumed_batch_single_merge(tmp_path, monkeypatch):
    concat_calls, stored = _run_batch(
        tmp_path, monkeypatch,
        chunk_indices=[0, 1, 2], boundary_indices=[])
    assert len(concat_calls) == 1
    assert concat_calls[0][1] is None  # starts at 0 → no init prefix
    assert stored == "part1"


def test_later_batch_without_boundary_uses_legacy_init(tmp_path,
                                                       monkeypatch):
    concat_calls, stored = _run_batch(
        tmp_path, monkeypatch,
        chunk_indices=[20, 21], boundary_indices=[])
    assert len(concat_calls) == 1
    assert concat_calls[0][1] is not None
    assert concat_calls[0][1].endswith("init.webm")


def test_mp4_resumed_batch_splits(tmp_path, monkeypatch):
    concat_calls, stored = _run_batch(
        tmp_path, monkeypatch,
        chunk_indices=[0, 1, 2, 3], boundary_indices=[2], ext=".mp4")
    assert len(concat_calls) == 2
    assert "init.2" in concat_calls[1][1]
