"""The monthly spend cap never loses a recording (#341).

The block flag is set out of band (ENFORCE_CAP_DELAY_SECONDS after any
cost row, transcription rows included), so it can flip in the middle of a
recording. What the cap may block, and what it may not:

- Starting a recording or an upload: refused with 402 before anything is
  stored (streaming init, legacy streaming init, chunked-upload init,
  multipart POST /nodes/). Typed entries are not affected.
- A recording that has started: always transcribed to the end. Resuming
  one (audio-chunk on an existing session) is allowed.
- The LLM reply after a Voice recording: skipped, with a warning the voice
  frontend shows as a toast, and the entry kept.

Same isolation patterns as test_node_deletion.py (routes) and
test_resume_subsessions.py (task bodies with the celery glue stubbed).
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime
from io import BytesIO
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
# Routes read AUDIO_STORAGE_PATH at import; keep their writes out of the repo.
_AUDIO_ROOT = tempfile.mkdtemp(prefix="wop-audio-cap-test-")
os.environ["AUDIO_STORAGE_PATH"] = _AUDIO_ROOT

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
    User, Node, Draft, NodeTranscriptChunk,
)
import backend.models as _real_backend_models  # noqa: E402
from backend.utils.spend import current_month  # noqa: E402

# A chunk past 0 without its own stream header (see test_resume_subsessions).
FRAGMENT_BYTES = b"\x00\x00\x01\x10moofdata-no-init-here"


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

    from backend.routes.nodes import nodes_bp
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(drafts_bp, url_prefix="/drafts")
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
def alice(app):
    u = User(username="alice", twitter_id="alice-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def client(app, alice):
    c = app.test_client()
    with c.session_transaction() as session:
        session["_user_id"] = str(alice.id)
    return c


def _assert_cap_402(resp):
    assert resp.status_code == 402, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "monthly_spend_limit_reached"


class TestStartIsRefusedWhenCapped:
    def test_streaming_init_refused_and_no_draft(self, client, alice):
        _cap(alice)
        _assert_cap_402(client.post("/drafts/streaming/init", json={}))
        assert Draft.query.count() == 0

    def test_streaming_init_allowed_when_not_capped(self, client, alice):
        resp = client.post("/drafts/streaming/init", json={})
        assert resp.status_code == 201
        assert Draft.query.count() == 1

    def test_legacy_streaming_init_refused(self, client, alice):
        _cap(alice)
        _assert_cap_402(client.post("/nodes/streaming/init", json={}))
        assert Node.query.count() == 0

    def test_chunked_upload_init_refused_and_no_node(self, client, alice):
        _cap(alice)
        _assert_cap_402(client.post("/nodes/upload/init", json={
            "filename": "talk.m4a", "filesize": 20 * 1024 * 1024,
            "total_chunks": 4, "upload_id": "up-1",
        }))
        assert Node.query.count() == 0

    def test_multipart_upload_refused_and_no_node(self, client, alice):
        _cap(alice)
        resp = client.post("/nodes/", data={
            "audio_file": (BytesIO(b"fake audio"), "talk.m4a"),
        }, content_type="multipart/form-data")
        _assert_cap_402(resp)
        assert Node.query.count() == 0

    def test_typed_entry_still_saved(self, client, alice):
        # The cap blocks cost actions; writing text costs nothing.
        _cap(alice)
        resp = client.post("/nodes/", json={"content": "a typed entry"})
        assert resp.status_code == 201
        assert Node.query.count() == 1


class TestResumeIsAllowedWhenCapped:
    def test_audio_chunk_accepted_for_existing_session(self, client, alice):
        # A recording that started before the cap (or is being resumed
        # after an interruption) keeps uploading: no init, no cap check.
        session_id = "sess-resume"
        draft = Draft(user_id=alice.id, session_id=session_id,
                      streaming_status="recording",
                      streaming_mime_type="audio/webm")
        draft.set_content("")
        _db.session.add(draft)
        _db.session.commit()
        from backend.routes import drafts as drafts_routes
        (drafts_routes.AUDIO_STORAGE_ROOT
         / f"drafts/{alice.id}/{session_id}").mkdir(parents=True,
                                                    exist_ok=True)
        _cap(alice)

        resp = client.post(
            f"/drafts/streaming/{session_id}/audio-chunk",
            data={
                "chunk": (BytesIO(FRAGMENT_BYTES), "chunk.webm"),
                "chunk_index": "5",
                "mime_type": "audio/webm",
            },
            content_type="multipart/form-data",
        )

        assert resp.status_code == 202, resp.get_data(as_text=True)
        chunk = NodeTranscriptChunk.query.filter_by(
            session_id=session_id, chunk_index=5).one()
        assert chunk.status == "stored"

    def test_status_carries_the_streaming_warning(self, client, alice):
        # The voice frontend's polling fallback needs the warning too.
        draft = Draft(user_id=alice.id, session_id="sess-warn",
                      streaming_status="completed",
                      streaming_warning="reply skipped")
        draft.set_content("transcript")
        _db.session.add(draft)
        _db.session.commit()

        data = client.get("/drafts/streaming/sess-warn/status").get_json()

        assert data["warning"] == "reply skipped"


class TestSavedWithoutReplyDraftIsNotRestored:
    """#345 review: a Voice finalize that skipped the reply (cap) keeps
    the draft only for the all_complete event. It must never come back as
    a restorable draft, which would save the transcript a second time."""

    def _warning_draft(self, alice):
        draft = Draft(user_id=alice.id, session_id="sess-saved",
                      parent_id=None, streaming_status="completed",
                      streaming_warning="reply skipped")
        draft.set_content("the whole transcript")
        _db.session.add(draft)
        _db.session.commit()
        return draft

    def test_get_draft_skips_it(self, client, alice):
        self._warning_draft(alice)

        assert client.get("/drafts/").status_code == 404

    def test_stale_cleanup_deletes_it(self, client, alice):
        self._warning_draft(alice)

        # Any new session start runs the stale-draft cleanup.
        assert client.post("/drafts/streaming/init", json={}).status_code == 201

        assert Draft.query.filter_by(session_id="sess-saved").count() == 0


# ── Task bodies ──────────────────────────────────────────────────────────

@pytest.fixture
def st(monkeypatch, tmp_path):
    """backend.tasks.streaming_transcription with the celery glue stubbed,
    so task bodies run directly against an in-memory app."""
    _GLUE = ("backend.celery_app", "backend.tasks.streaming_transcription",
             "backend.tasks.transcription")
    saved = {k: sys.modules.get(k) for k in _GLUE}
    fake_celery_app = MagicMock()

    def _passthrough_task(*args, **kwargs):
        def deco(f):
            return f
        return deco
    fake_celery_app.celery.task = _passthrough_task
    sys.modules["backend.celery_app"] = fake_celery_app
    sys.modules.pop("backend.tasks.streaming_transcription", None)
    sys.modules.pop("backend.tasks.transcription", None)
    import backend.tasks.streaming_transcription as module

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    fake_celery_app.flask_app = app
    module.flask_app = app
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))

    monkeypatch.setattr(module, "compress_audio_if_needed", lambda p, log: p)
    monkeypatch.setattr(module, "get_audio_duration", lambda p, log: 15.0)
    monkeypatch.setattr(module, "get_openai_chat_key", lambda cfg: "key")
    monkeypatch.setattr(
        module, "concat_fragmented_media",
        lambda paths, init_segment_path=None, output_suffix=None: str(
            _write(tmp_path / f"merged{output_suffix}", b"merged")))
    fake_openai = MagicMock()
    fake_openai.return_value.audio.transcriptions.create.return_value = (
        type("R", (), {"text": "spoken words"})())
    monkeypatch.setattr(module, "OpenAI", fake_openai)

    with app.app_context():
        _db.create_all()
        user = User(username="capped")
        _db.session.add(user)
        _db.session.commit()
        _cap(user)
        module._test_user = user
        module._test_root = tmp_path
        yield module
        _db.session.rollback()
        _db.drop_all()

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _write(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _recording(user, session_id, chunk_indices, root):
    draft = Draft(user_id=user.id, session_id=session_id,
                  streaming_status="recording",
                  streaming_mime_type="audio/webm")
    draft.set_content("")
    _db.session.add(draft)
    for idx in chunk_indices:
        _db.session.add(NodeTranscriptChunk(
            session_id=session_id, chunk_index=idx, status="stored"))
        _write(root / f"drafts/{user.id}/{session_id}/chunk_{idx:04d}.webm",
               b"\x1aE\xdf\xa3-webm-head" if idx == 0 else FRAGMENT_BYTES)
    _db.session.commit()
    return draft


class TestCappedRecordingIsTranscribed:
    def test_single_chunk_task_transcribes(self, st):
        user = st._test_user
        _recording(user, "sess-1", [3], st._test_root)
        path = st._test_root / f"drafts/{user.id}/sess-1/chunk_0003.webm"

        st.transcribe_draft_chunk(MagicMock(), "sess-1", 3, str(path))

        chunk = NodeTranscriptChunk.query.filter_by(
            session_id="sess-1", chunk_index=3).one()
        assert chunk.status == "completed"
        assert chunk.get_text() == "spoken words"

    def test_batch_task_transcribes(self, st):
        user = st._test_user
        _recording(user, "sess-2", [0, 1, 2], st._test_root)

        st.transcribe_chunk_batch(MagicMock(), "sess-2", [0, 1, 2])

        chunks = NodeTranscriptChunk.query.filter_by(
            session_id="sess-2").all()
        assert chunks, "chunk rows missing"
        assert all(c.status == "completed" for c in chunks)
        head = next(c for c in chunks if c.chunk_index == 0)
        assert head.get_text() == "spoken words"

    def test_voice_finalize_keeps_entry_and_skips_reply(self, st):
        # The whole transcript is in; the cap only skips the LLM reply,
        # and the draft carries the warning the voice frontend toasts.
        user = st._test_user
        draft = _recording(user, "sess-3", [], st._test_root)
        draft.ai_usage = "chat"
        _db.session.commit()
        saved_llm = sys.modules.get("backend.tasks.llm_completion")
        sys.modules["backend.tasks.llm_completion"] = MagicMock()
        try:
            st._start_server_side_llm_chain(
                draft, "sess-3", "the whole transcript", user.id,
                None, "gpt-5", "Voice")
        finally:
            if saved_llm is None:
                sys.modules.pop("backend.tasks.llm_completion", None)
            else:
                sys.modules["backend.tasks.llm_completion"] = saved_llm

        _db.session.refresh(draft)
        assert draft.streaming_status == "completed"
        assert draft.llm_node_id is None
        assert draft.streaming_warning == st.VOICE_REPLY_SKIPPED_SPEND_CAP
        entry = Node.query.filter(Node.parent_id.isnot(None)).one()
        assert entry.get_content() == "the whole transcript"
        assert Node.query.filter_by(node_type="llm").count() == 0

    @pytest.mark.parametrize("capped", [True, False])
    def test_voice_finalize_prewarms_only_when_not_capped(self, st, capped):
        # A capped finalize skips the reply, so it must not pay for a cache
        # warm either (nor create the early system node the warm needs).
        # The uncapped case is the control: same setup, the warm fires.
        user = st._test_user
        if not capped:
            user.spend_blocked_month = None
        draft = _recording(user, "sess-4", [], st._test_root)
        draft.set_content("x" * 600)
        _db.session.commit()
        st.flask_app.config["SUPPORTED_MODELS"] = {
            "claude-test": {"provider": "anthropic"}}
        fake_llm = MagicMock()
        saved_llm = sys.modules.get("backend.tasks.llm_completion")
        sys.modules["backend.tasks.llm_completion"] = fake_llm
        try:
            st.finalize_draft_streaming(
                MagicMock(), "sess-4", 0, label="Voice", user_id=user.id,
                parent_id=None, model="claude-test")
        finally:
            if saved_llm is None:
                sys.modules.pop("backend.tasks.llm_completion", None)
            else:
                sys.modules["backend.tasks.llm_completion"] = saved_llm

        warmed = fake_llm.prewarm_anthropic_cache.delay.called
        assert warmed is (not capped)
        assert (Node.query.count() > 0) is (not capped)

    def test_accepted_upload_is_transcribed(self, st, monkeypatch):
        # An upload the server accepted before the cap flipped finishes.
        import backend.tasks.transcription as tr
        tr.flask_app = st.flask_app
        monkeypatch.setattr(tr, "compress_audio_if_needed", lambda p, log: p)
        monkeypatch.setattr(tr, "get_audio_duration", lambda p, log: 15.0)
        monkeypatch.setattr(tr, "get_openai_chat_key", lambda cfg: "key")
        fake_openai = MagicMock()
        fake_openai.return_value.audio.transcriptions.create.return_value = (
            type("R", (), {"text": "uploaded words"})())
        monkeypatch.setattr(tr, "OpenAI", fake_openai)
        user = st._test_user
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", transcription_status="pending")
        node.set_content("[Voice note – transcription pending]")
        _db.session.add(node)
        _db.session.commit()
        path = _write(st._test_root / "upload.m4a", b"fake audio")

        tr.transcribe_audio(MagicMock(), node.id, str(path), "upload.m4a")

        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert "uploaded words" in node.get_content()
