"""No AI means no AI, for recordings (#665).

Turning a recording into text sends its audio to OpenAI. The web and the
iPhone disable Record and Upload for an entry set to None; the server now
refuses the same on every path that takes audio, before anything is
stored or queued, and the workers ask again right before the provider
call. A recording whose entry is set to None after it started keeps its
audio and is not transcribed.

Each path has a test that fails on main (the recording was accepted and
queued) and a Chat control. Same isolation patterns as
test_spend_cap_recording.py.
"""
import os
import pathlib
import sys
import tempfile
from io import BytesIO
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
# Routes read AUDIO_STORAGE_PATH at import; keep their writes out of the repo.
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-none-")

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
from backend.utils.recording_ai_usage import (  # noqa: E402
    NOT_TRANSCRIBED_CONTENT, NOT_TRANSCRIBED_NOTE, NOT_TRANSCRIBED_TEXT,
    PARTLY_NOT_TRANSCRIBED_NOTE, RECORDING_REFUSED_MESSAGE,
)

# A chunk past 0 without its own stream header (see test_resume_subsessions).
FRAGMENT_BYTES = b"\x00\x00\x01\x10moofdata-no-init-here"


def _assert_refused(resp):
    assert resp.status_code == 403, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["code"] == "ai_usage_none"
    assert body["scope"] == "entry"
    assert body["error"] == RECORDING_REFUSED_MESSAGE


# ── Routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def stub_tasks():
    """The task modules the routes import lazily, as mocks whose .delay
    calls the tests read; whatever was there is restored."""
    names = ("backend.tasks.transcription",
             "backend.tasks.streaming_transcription")
    saved = {k: sys.modules.get(k) for k in names}
    transcription = MagicMock()
    transcription.transcribe_audio.delay.return_value = MagicMock(id="tr")
    streaming = MagicMock()
    streaming.transcribe_chunk.delay.return_value = MagicMock(id="chunk")
    streaming.transcribe_chunk_batch.delay.return_value = MagicMock(
        id="batch")
    sys.modules["backend.tasks.transcription"] = transcription
    sys.modules["backend.tasks.streaming_transcription"] = streaming
    yield transcription, streaming
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


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
def app(stub_tasks, monkeypatch):
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
    import backend.routes.nodes as nodes_routes
    monkeypatch.setattr(nodes_routes, "get_openai_chat_key", lambda cfg: "key")
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
    u = User(username="alice", twitter_id="alice-twitter-id",
             default_ai_usage="chat")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def client(app, alice):
    c = app.test_client()
    with c.session_transaction() as session:
        session["_user_id"] = str(alice.id)
    return c


def _audio_root():
    import backend.routes.nodes as nodes_routes
    return nodes_routes.AUDIO_STORAGE_ROOT


def _files_under(path):
    path = pathlib.Path(path)
    return [p for p in path.rglob("*") if p.is_file()] if path.exists() else []


class TestMultipartUpload:
    def _post(self, client, ai_usage):
        return client.post("/nodes/", data={
            "audio_file": (BytesIO(b"fake audio"), "talk.m4a"),
            "ai_usage": ai_usage,
        }, content_type="multipart/form-data")

    def test_none_is_refused_before_anything_is_stored(
            self, client, alice, stub_tasks):
        transcription, _ = stub_tasks
        _assert_refused(self._post(client, "none"))
        assert Node.query.count() == 0
        assert not _files_under(_audio_root() / f"user/{alice.id}")
        transcription.transcribe_audio.delay.assert_not_called()

    def test_chat_is_stored_and_queued(self, client, alice, stub_tasks):
        transcription, _ = stub_tasks
        resp = self._post(client, "chat")
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert Node.query.count() == 1
        transcription.transcribe_audio.delay.assert_called_once()


class TestChunkedUpload:
    def _init(self, client, ai_usage, upload_id="up-1"):
        return client.post("/nodes/upload/init", json={
            "filename": "talk.m4a", "filesize": 10, "total_chunks": 1,
            "upload_id": upload_id, "ai_usage": ai_usage,
        })

    def _chunk_and_finalize(self, client, node_id, upload_id="up-1"):
        resp = client.post("/nodes/upload/chunk", data={
            "chunk": (BytesIO(b"0123456789"), "blob"),
            "chunk_index": "0", "upload_id": upload_id,
            "node_id": str(node_id),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return client.post("/nodes/upload/finalize", json={
            "upload_id": upload_id, "node_id": node_id})

    def test_init_with_none_is_refused(self, client, alice):
        _assert_refused(self._init(client, "none"))
        assert Node.query.count() == 0
        assert not (_audio_root() / f"chunks/{alice.id}/up-1").exists()

    def test_set_to_none_before_finalize_keeps_audio_untranscribed(
            self, client, alice, stub_tasks):
        transcription, _ = stub_tasks
        resp = self._init(client, "chat")
        assert resp.status_code == 201
        node_id = resp.get_json()["node_id"]
        # The entry is edited to None (another tab) while it uploads.
        node = Node.query.get(node_id)
        node.ai_usage = "none"
        _db.session.commit()

        resp = self._chunk_and_finalize(client, node_id)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["transcription_status"] == "failed"
        transcription.transcribe_audio.delay.assert_not_called()
        node = Node.query.get(node_id)
        assert node.transcription_error == NOT_TRANSCRIBED_TEXT
        assert node.get_content() == f"# talk\n\n{NOT_TRANSCRIBED_CONTENT}"
        assert node.audio_original_url
        audio = _files_under(_audio_root() / f"user/{alice.id}/node/{node_id}")
        assert [p.read_bytes() for p in audio] == [b"0123456789"]

    def test_chat_throughout_is_queued(self, client, alice, stub_tasks):
        transcription, _ = stub_tasks
        node_id = self._init(client, "chat").get_json()["node_id"]

        resp = self._chunk_and_finalize(client, node_id)

        assert resp.status_code == 200
        transcription.transcribe_audio.delay.assert_called_once()
        assert Node.query.get(node_id).transcription_status == "pending"


class TestNodeStreaming:
    def _init(self, client, ai_usage):
        return client.post("/nodes/streaming/init", json={"ai_usage": ai_usage})

    def _chunk(self, client, node_id, session_id):
        return client.post(f"/nodes/{node_id}/audio-chunk", data={
            "chunk": (BytesIO(b"audio"), "chunk.webm"),
            "chunk_index": "0", "session_id": session_id,
        }, content_type="multipart/form-data")

    def test_init_with_none_is_refused(self, client, alice):
        _assert_refused(self._init(client, "none"))
        assert Node.query.count() == 0

    def test_chunk_after_set_to_none_is_kept_not_queued(
            self, client, alice, stub_tasks):
        _, streaming = stub_tasks
        data = self._init(client, "chat").get_json()
        node = Node.query.get(data["node_id"])
        node.ai_usage = "none"
        _db.session.commit()

        resp = self._chunk(client, data["node_id"], data["session_id"])

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["status"] == "not_transcribed"
        streaming.transcribe_chunk.delay.assert_not_called()
        chunk = NodeTranscriptChunk.query.filter_by(
            node_id=data["node_id"], chunk_index=0).one()
        assert (chunk.status, chunk.error) == ("failed", NOT_TRANSCRIBED_TEXT)
        stored = _files_under(
            _audio_root() / f"streaming/{alice.id}/{data['session_id']}")
        assert [p.name for p in stored] == ["chunk_0000.webm"]

    def test_chat_chunk_is_queued(self, client, alice, stub_tasks):
        _, streaming = stub_tasks
        data = self._init(client, "chat").get_json()

        resp = self._chunk(client, data["node_id"], data["session_id"])

        assert resp.status_code == 202
        streaming.transcribe_chunk.delay.assert_called_once()


class TestDictation:
    def test_init_with_none_is_refused(self, client, alice):
        _assert_refused(client.post("/drafts/streaming/init",
                                    json={"ai_usage": "none"}))
        assert Draft.query.count() == 0

    def test_init_without_ai_usage_is_refused(self, client, alice):
        # The default stays None, as before: every app sends the value.
        _assert_refused(client.post("/drafts/streaming/init", json={}))
        assert Draft.query.count() == 0

    def test_init_with_chat_starts(self, client, alice):
        resp = client.post("/drafts/streaming/init", json={"ai_usage": "chat"})
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert Draft.query.count() == 1

    def test_voice_keeps_its_own_rule(self, client, alice):
        # A Voice turn continuing a Chat thread starts even when the
        # account's default (sent by the Voice page) is None.
        alice.default_ai_usage = "none"
        parent = Node(user_id=alice.id, human_owner_id=alice.id,
                      node_type="user", ai_usage="chat")
        parent.set_content("a chat thread")
        _db.session.add(parent)
        _db.session.commit()
        resp = client.post("/drafts/streaming/init", json={
            "ai_usage": "none", "label": "Voice", "parent_id": parent.id})
        assert resp.status_code == 201, resp.get_data(as_text=True)

    def _session(self, alice, ai_usage, stored, session_id="sess-1"):
        draft = Draft(user_id=alice.id, session_id=session_id,
                      streaming_status="recording", ai_usage=ai_usage,
                      streaming_mime_type="audio/webm")
        draft.set_content("")
        _db.session.add(draft)
        for idx in range(stored):
            _db.session.add(NodeTranscriptChunk(
                session_id=session_id, chunk_index=idx, status="stored"))
        _db.session.commit()
        (_audio_root() / f"drafts/{alice.id}/{session_id}").mkdir(
            parents=True, exist_ok=True)
        return draft

    def _twentieth_chunk(self, client):
        return client.post("/drafts/streaming/sess-1/audio-chunk", data={
            "chunk": (BytesIO(FRAGMENT_BYTES), "chunk.webm"),
            "chunk_index": "19", "mime_type": "audio/webm",
        }, content_type="multipart/form-data")

    def test_full_batch_of_a_none_session_is_kept_not_queued(
            self, client, alice, stub_tasks):
        # A session started before the routes refused one.
        _, streaming = stub_tasks
        self._session(alice, "none", stored=19)

        resp = self._twentieth_chunk(client)

        assert resp.status_code == 202, resp.get_data(as_text=True)
        streaming.transcribe_chunk_batch.delay.assert_not_called()
        chunks = NodeTranscriptChunk.query.filter_by(session_id="sess-1").all()
        assert len(chunks) == 20
        assert {(c.status, c.error) for c in chunks} == {
            ("failed", NOT_TRANSCRIBED_TEXT)}

    def test_full_batch_of_a_chat_session_is_queued(
            self, client, alice, stub_tasks):
        _, streaming = stub_tasks
        self._session(alice, "chat", stored=19)

        resp = self._twentieth_chunk(client)

        assert resp.status_code == 202
        streaming.transcribe_chunk_batch.delay.assert_called_once()

    def test_transcribe_remaining_of_a_none_session_queues_nothing(
            self, client, alice, stub_tasks):
        _, streaming = stub_tasks
        self._session(alice, "none", stored=3)

        resp = client.post("/drafts/streaming/sess-1/transcribe-remaining")

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["not_transcribed"] == 3
        streaming.transcribe_chunk_batch.delay.assert_not_called()

    def test_transcribe_remaining_of_a_chat_session_is_queued(
            self, client, alice, stub_tasks):
        _, streaming = stub_tasks
        self._session(alice, "chat", stored=3)

        resp = client.post("/drafts/streaming/sess-1/transcribe-remaining")

        assert resp.status_code == 202
        streaming.transcribe_chunk_batch.delay.assert_called_once()


# ── Task bodies: the last check before the provider call ─────────────────

@pytest.fixture
def tasks(monkeypatch, tmp_path):
    """backend.tasks.streaming_transcription and .transcription with the
    celery glue stubbed and the provider mocked, against an in-memory
    app."""
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
    import backend.tasks.streaming_transcription as st
    import backend.tasks.transcription as tr

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    fake_celery_app.flask_app = app
    st.flask_app = app
    tr.flask_app = app
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))

    fake_openai = MagicMock()
    fake_openai.return_value.audio.transcriptions.create.return_value = (
        type("R", (), {"text": "spoken words"})())
    for module in (st, tr):
        monkeypatch.setattr(module, "compress_audio_if_needed",
                            lambda p, log: p)
        monkeypatch.setattr(module, "get_audio_duration",
                            lambda p, log: 15.0)
        monkeypatch.setattr(module, "get_openai_chat_key", lambda cfg: "key")
        monkeypatch.setattr(module, "OpenAI", fake_openai)
    monkeypatch.setattr(
        st, "concat_fragmented_media",
        lambda paths, init_segment_path=None, output_suffix=None,
        report=None: str(_write(tmp_path / f"merged{output_suffix}",
                                b"merged")))

    with app.app_context():
        _db.create_all()
        user = User(username="bob", default_ai_usage="chat")
        _db.session.add(user)
        _db.session.commit()
        ns = MagicMock()
        ns.st, ns.tr, ns.user, ns.root = st, tr, user, tmp_path
        ns.transcribe = fake_openai.return_value.audio.transcriptions.create
        yield ns
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


def _upload_node(user, ai_usage, content="[Voice note – transcription pending]"):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                transcription_status="pending", ai_usage=ai_usage)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _recording(user, session_id, ai_usage, chunk_indices, root,
               label=None, parent_id=None):
    draft = Draft(user_id=user.id, session_id=session_id,
                  streaming_status="recording", ai_usage=ai_usage,
                  label=label, parent_id=parent_id,
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


class TestUploadTask:
    def test_none_entry_is_not_sent(self, tasks):
        node = _upload_node(tasks.user, "none")
        path = _write(tasks.root / "upload.m4a", b"fake audio")

        result = tasks.tr.transcribe_audio(
            MagicMock(), node.id, str(path), "recording.m4a")

        assert result["status"] == "not_transcribed"
        tasks.transcribe.assert_not_called()
        _db.session.refresh(node)
        assert node.transcription_status == "failed"
        assert node.transcription_error == NOT_TRANSCRIBED_TEXT
        assert NOT_TRANSCRIBED_CONTENT in node.get_content()
        assert path.exists()

    def test_an_existing_transcript_is_kept(self, tasks):
        node = _upload_node(tasks.user, "none", content="# talk\n\nwords")
        path = _write(tasks.root / "upload.m4a", b"fake audio")

        tasks.tr.transcribe_audio(MagicMock(), node.id, str(path), "talk.m4a")

        tasks.transcribe.assert_not_called()
        _db.session.refresh(node)
        assert node.get_content() == "# talk\n\nwords"

    def test_chat_entry_is_transcribed(self, tasks):
        node = _upload_node(tasks.user, "chat")
        path = _write(tasks.root / "upload.m4a", b"fake audio")

        tasks.tr.transcribe_audio(MagicMock(), node.id, str(path), "talk.m4a")

        tasks.transcribe.assert_called_once()
        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert node.get_content() == "# talk\n\nspoken words"


class TestBatchTask:
    def test_none_session_is_not_sent(self, tasks):
        _recording(tasks.user, "s-none", "none", [0, 1], tasks.root)

        result = tasks.st.transcribe_chunk_batch(MagicMock(), "s-none", [0, 1])

        assert result["status"] == "not_transcribed"
        tasks.transcribe.assert_not_called()
        chunks = NodeTranscriptChunk.query.filter_by(session_id="s-none").all()
        assert {(c.status, c.error) for c in chunks} == {
            ("failed", NOT_TRANSCRIBED_TEXT)}
        folder = tasks.root / f"drafts/{tasks.user.id}/s-none"
        assert sorted(p.name for p in folder.iterdir()) == [
            "chunk_0000.webm", "chunk_0001.webm"]

    def test_chat_session_is_sent(self, tasks):
        _recording(tasks.user, "s-chat", "chat", [0, 1], tasks.root)

        tasks.st.transcribe_chunk_batch(MagicMock(), "s-chat", [0, 1])

        tasks.transcribe.assert_called_once()

    def _thread(self, user, ai_usage):
        parent = Node(user_id=user.id, human_owner_id=user.id,
                      node_type="user", ai_usage=ai_usage)
        parent.set_content("thread")
        _db.session.add(parent)
        _db.session.commit()
        return parent

    def test_voice_turn_takes_the_threads_setting(self, tasks):
        # The Voice page sends the account default (None here); the turn's
        # entry takes the thread's Chat, so it is transcribed.
        tasks.user.default_ai_usage = "none"
        parent = self._thread(tasks.user, "chat")
        _recording(tasks.user, "s-voice", "none", [0], tasks.root,
                   label="Voice", parent_id=parent.id)

        tasks.st.transcribe_chunk_batch(MagicMock(), "s-voice", [0])

        tasks.transcribe.assert_called_once()

    def test_voice_turn_in_a_thread_set_to_none_is_not_sent(self, tasks):
        # The thread was set to None while the turn was recorded.
        parent = self._thread(tasks.user, "none")
        _recording(tasks.user, "s-voice-none", "chat", [0], tasks.root,
                   label="Voice", parent_id=parent.id)

        tasks.st.transcribe_chunk_batch(MagicMock(), "s-voice-none", [0])

        tasks.transcribe.assert_not_called()

    def test_single_chunk_task_checks_too(self, tasks):
        _recording(tasks.user, "s-one", "none", [3], tasks.root)
        path = tasks.root / f"drafts/{tasks.user.id}/s-one/chunk_0003.webm"

        tasks.st.transcribe_draft_chunk(MagicMock(), "s-one", 3, str(path))

        tasks.transcribe.assert_not_called()


class TestDictationFinalize:
    def test_none_session_queues_nothing_and_says_why(self, tasks, monkeypatch):
        delay = MagicMock()
        monkeypatch.setattr(tasks.st.transcribe_chunk_batch, "delay", delay,
                            raising=False)
        _recording(tasks.user, "s-fin", "none", [0, 1], tasks.root)

        tasks.st.finalize_draft_streaming(MagicMock(), "s-fin", 2)

        delay.assert_not_called()
        tasks.transcribe.assert_not_called()
        draft = Draft.query.filter_by(session_id="s-fin").one()
        assert draft.streaming_status == "completed"
        assert NOT_TRANSCRIBED_NOTE in draft.get_content()
        assert "failed to transcribe" not in draft.get_content()


class TestLegacyStreaming:
    def _node(self, user, ai_usage, session_id):
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", ai_usage=ai_usage,
                    streaming_transcription=True,
                    streaming_session_id=session_id,
                    transcription_status="processing")
        node.set_content("")
        _db.session.add(node)
        _db.session.commit()
        return node

    def test_chunk_task_does_not_send_a_none_entry(self, tasks):
        node = self._node(tasks.user, "none", "legacy-1")
        _db.session.add(NodeTranscriptChunk(
            node_id=node.id, chunk_index=0, status="processing"))
        _db.session.commit()
        path = _write(tasks.root / "streaming/chunk_0000.webm", b"audio")

        tasks.st.transcribe_chunk(MagicMock(), node.id, 0, str(path))

        tasks.transcribe.assert_not_called()
        chunk = NodeTranscriptChunk.query.filter_by(node_id=node.id).one()
        assert chunk.error == NOT_TRANSCRIBED_TEXT

    def test_finalize_keeps_the_audio_of_untranscribed_chunks(self, tasks):
        session_id = "123e4567-e89b-42d3-a456-426614174000"
        node = self._node(tasks.user, "none", session_id)
        done = NodeTranscriptChunk(node_id=node.id, chunk_index=0,
                                   status="completed")
        done.set_text("said while Chat")
        _db.session.add(done)
        _db.session.add(NodeTranscriptChunk(
            node_id=node.id, chunk_index=1, status="failed",
            error=NOT_TRANSCRIBED_TEXT))
        _db.session.commit()
        folder = tasks.root / f"streaming/{tasks.user.id}/{session_id}"
        _write(folder / "chunk_0001.webm", b"kept audio")

        tasks.st.finalize_streaming(MagicMock(), node.id, session_id, 2)

        _db.session.refresh(node)
        assert node.transcription_status == "completed"
        assert PARTLY_NOT_TRANSCRIBED_NOTE in node.get_content()
        assert "said while Chat" in node.get_content()
        kept = tasks.root / f"nodes/{tasks.user.id}/{node.id}/chunk_0001.webm"
        assert kept.read_bytes() == b"kept audio"
        assert not folder.exists()
