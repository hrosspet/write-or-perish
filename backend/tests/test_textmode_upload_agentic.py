"""A Text-mode audio upload ends up like a typed or recorded entry (#342).

The upload endpoints (multipart POST /nodes/, /nodes/upload/init +
/upload/finalize) take the same two decisions save-as-node takes:
`agentic` (the new thread gets a system node with the textmode prompt)
and `auto_generate` (an LLM reply). The reply has to wait for the
transcript, so the upload hands the model to transcribe_audio, which
creates the placeholder under the transcript's tip before it marks the
transcription complete; the status endpoint then reports it.

Covers: system node + prompt pin on both upload paths, the task kwarg
(only when a reply was asked for), refusals (parent, ai_usage 'none'),
the task creating (or, when capped, skipping) the reply, and the status
endpoint finding the reply below a split transcript.
"""
import json
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
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-upload-test-")

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
from backend.models import User, Node, NodeContextArtifact  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

SUPPORTED = {
    "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    "claude-opus-4.7": {"provider": "anthropic", "api_model": "claude-opus-4.7"},
}


@pytest.fixture
def stub_tasks():
    """Mock the task modules the routes and create_llm_placeholder import
    lazily; restore whatever was there (other test files stub them too)."""
    names = ("backend.tasks.transcription", "backend.tasks.llm_completion")
    saved = {k: sys.modules.get(k) for k in names}
    transcription = MagicMock()
    transcription.transcribe_audio.delay.return_value = MagicMock(id="tr-task")
    llm = MagicMock()
    llm.generate_llm_response.delay.return_value = MagicMock(id="llm-task")
    sys.modules["backend.tasks.transcription"] = transcription
    sys.modules["backend.tasks.llm_completion"] = llm
    yield transcription, llm
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ── Routes ───────────────────────────────────────────────────────────────

def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = SUPPORTED
    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
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
             approved=True, plan="alpha")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def client(app, alice):
    c = app.test_client()
    with c.session_transaction() as session:
        session["_user_id"] = str(alice.id)
    return c


def _upload(client, **fields):
    data = {"audio_file": (BytesIO(b"fake audio"), "talk.m4a"),
            "ai_usage": "chat", **fields}
    return client.post("/nodes/", data=data,
                       content_type="multipart/form-data")


def _assert_textmode_root(root_id):
    root = Node.query.get(root_id)
    assert root.parent_id is None
    assert root.get_prompt_key() == "textmode"
    assert NodeContextArtifact.query.filter_by(
        node_id=root.id, artifact_type="prompt").count() == 1
    return root


class TestMultipartUpload:
    def test_agentic_with_reply(self, client, stub_tasks):
        transcription, _ = stub_tasks
        resp = _upload(client, agentic="true", auto_generate="true")

        assert resp.status_code == 201, resp.get_data(as_text=True)
        data = resp.get_json()
        root = _assert_textmode_root(data["conversation_id"])
        entry = Node.query.get(data["id"])
        assert entry.parent_id == root.id
        args, kwargs = transcription.transcribe_audio.delay.call_args
        assert args[0] == entry.id
        assert kwargs == {"auto_reply_model": "gpt-5"}

    def test_agentic_without_reply(self, client, stub_tasks):
        transcription, _ = stub_tasks
        data = _upload(client, agentic="true").get_json()

        _assert_textmode_root(data["conversation_id"])
        assert transcription.transcribe_audio.delay.call_args.kwargs == {}

    def test_reply_without_agentic(self, client, stub_tasks):
        # Write New Entry with Agentic Reply off, Auto-generate on.
        transcription, _ = stub_tasks
        data = _upload(client, auto_generate="true",
                       model="claude-opus-4.7").get_json()

        assert "conversation_id" not in data
        assert Node.query.get(data["id"]).parent_id is None
        assert transcription.transcribe_audio.delay.call_args.kwargs == {
            "auto_reply_model": "claude-opus-4.7"}

    def test_plain_upload_unchanged(self, client, stub_tasks):
        transcription, _ = stub_tasks
        data = _upload(client).get_json()

        assert "conversation_id" not in data
        assert Node.query.count() == 1
        # No new kwarg: a worker on the old signature still accepts it.
        assert transcription.transcribe_audio.delay.call_args.kwargs == {}

    def test_refused_under_a_parent(self, client, alice):
        parent = Node(user_id=alice.id, human_owner_id=alice.id,
                      node_type="user", ai_usage="chat")
        parent.set_content("thread")
        _db.session.add(parent)
        _db.session.commit()

        resp = _upload(client, agentic="true", parent_id=str(parent.id))

        assert resp.status_code == 400
        assert Node.query.count() == 1

    def test_refused_with_ai_usage_none(self, client):
        resp = _upload(client, agentic="true", ai_usage="none")

        assert resp.status_code == 400
        assert Node.query.count() == 0

    def test_unsupported_model_refused(self, client):
        resp = _upload(client, auto_generate="true", model="nope")

        assert resp.status_code == 400
        assert Node.query.count() == 0


class TestChunkedUpload:
    def test_init_builds_root_and_finalize_passes_the_model(
            self, client, alice, stub_tasks):
        transcription, _ = stub_tasks
        resp = client.post("/nodes/upload/init", json={
            "filename": "talk.m4a", "filesize": 4, "total_chunks": 1,
            "upload_id": "up-342", "ai_usage": "chat",
            "agentic": True, "auto_generate": True,
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        data = resp.get_json()
        root = _assert_textmode_root(data["conversation_id"])
        assert Node.query.get(data["node_id"]).parent_id == root.id

        resp = client.post("/nodes/upload/chunk", data={
            "chunk": (BytesIO(b"abcd"), "blob"), "chunk_index": "0",
            "upload_id": "up-342", "node_id": str(data["node_id"]),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        resp = client.post("/nodes/upload/finalize", json={
            "upload_id": "up-342", "node_id": data["node_id"]})
        assert resp.status_code == 200, resp.get_data(as_text=True)

        assert transcription.transcribe_audio.delay.call_args.kwargs == {
            "auto_reply_model": "gpt-5"}


class TestStatusReportsTheReply:
    def _entry(self, alice, parent_id=None):
        n = Node(user_id=alice.id, human_owner_id=alice.id,
                 node_type="user", ai_usage="chat", parent_id=parent_id,
                 transcription_status="completed")
        n.set_content("transcript")
        _db.session.add(n)
        _db.session.flush()
        return n

    def _llm_under(self, parent):
        bot = User(username="gpt-5", twitter_id="llm-gpt-5")
        _db.session.add(bot)
        _db.session.flush()
        llm = Node(user_id=bot.id, human_owner_id=parent.human_owner_id,
                   node_type="llm", parent_id=parent.id, ai_usage="chat",
                   llm_task_status="pending")
        llm.set_content("[pending]")
        _db.session.add(llm)
        _db.session.commit()
        return llm

    def test_reply_below_a_split_transcript(self, client, alice):
        from backend.utils.node_split import split_node_into_chain
        entry = self._entry(alice)
        parts = split_node_into_chain(entry, segments=["a", "b", "c"])
        llm = self._llm_under(parts[-1])

        data = client.get(
            f"/nodes/{entry.id}/transcription-status").get_json()

        assert data["llm_node_id"] == llm.id

    def test_a_later_reply_thread_is_not_the_upload_reply(self, client, alice):
        # entry → the user's own follow-up → an LLM reply to *that*.
        entry = self._entry(alice)
        _db.session.commit()
        followup = self._entry(alice, parent_id=entry.id)
        followup.created_at = datetime(2030, 1, 1)
        self._llm_under(followup)

        data = client.get(
            f"/nodes/{entry.id}/transcription-status").get_json()

        assert "llm_node_id" not in data

    def test_warnings_reported(self, client, alice):
        entry = self._entry(alice)
        entry.llm_task_warnings = json.dumps(["reply skipped"])
        _db.session.commit()

        data = client.get(
            f"/nodes/{entry.id}/transcription-status").get_json()

        assert data["warnings"] == ["reply skipped"]


# ── The task ─────────────────────────────────────────────────────────────

@pytest.fixture
def tr(monkeypatch, tmp_path):
    """backend.tasks.transcription with the celery glue stubbed so the task
    body runs against an in-memory app (see test_resume_subsessions)."""
    names = ("backend.celery_app", "backend.tasks.transcription",
             "backend.tasks.llm_completion")
    saved = {k: sys.modules.get(k) for k in names}
    fake_celery_app = MagicMock()

    def _passthrough_task(*args, **kwargs):
        def deco(f):
            return f
        return deco
    fake_celery_app.celery.task = _passthrough_task
    sys.modules["backend.celery_app"] = fake_celery_app
    llm = MagicMock()
    llm.generate_llm_response.delay.return_value = MagicMock(id="llm-task")
    sys.modules["backend.tasks.llm_completion"] = llm
    sys.modules.pop("backend.tasks.transcription", None)
    import backend.tasks.transcription as module

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    module.flask_app = app
    monkeypatch.setattr(module, "compress_audio_if_needed", lambda p, log: p)
    monkeypatch.setattr(module, "get_audio_duration", lambda p, log: 15.0)
    monkeypatch.setattr(module, "get_openai_chat_key", lambda cfg: "key")
    fake_openai = MagicMock()
    fake_openai.return_value.audio.transcriptions.create.return_value = (
        type("R", (), {"text": "uploaded words"})())
    monkeypatch.setattr(module, "OpenAI", fake_openai)

    with app.app_context():
        _db.create_all()
        user = User(username="uploader", approved=True, plan="alpha")
        _db.session.add(user)
        _db.session.commit()
        root = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", ai_usage="chat")
        _db.session.add(root)
        _db.session.flush()
        entry = Node(user_id=user.id, human_owner_id=user.id,
                     node_type="user", ai_usage="chat", parent_id=root.id,
                     transcription_status="pending")
        entry.set_content("[Voice note – transcription pending]")
        _db.session.add(entry)
        _db.session.commit()
        audio = pathlib.Path(tmp_path) / "talk.m4a"
        audio.write_bytes(b"fake audio")
        module._t = {"user": user, "entry": entry, "audio": str(audio),
                     "llm": llm}
        yield module
        _db.session.rollback()
        _db.drop_all()

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class TestTranscribeAudioReply:
    def test_reply_created_when_asked(self, tr):
        entry = tr._t["entry"]

        tr.transcribe_audio(MagicMock(), entry.id, tr._t["audio"],
                            "talk.m4a", auto_reply_model="gpt-5")

        _db.session.refresh(entry)
        assert entry.transcription_status == "completed"
        assert entry.get_content() == "uploaded words"
        reply = Node.query.filter_by(parent_id=entry.id,
                                     node_type="llm").one()
        assert reply.llm_model == "gpt-5"
        assert reply.llm_task_status == "pending"
        tr._t["llm"].generate_llm_response.delay.assert_called_once()
        assert (tr._t["llm"].generate_llm_response.delay.call_args.kwargs
                ["source_mode"] == "textmode")

    def test_no_reply_without_the_flag(self, tr):
        entry = tr._t["entry"]

        tr.transcribe_audio(MagicMock(), entry.id, tr._t["audio"], "talk.m4a")

        assert Node.query.filter_by(node_type="llm").count() == 0
        _db.session.refresh(entry)
        assert entry.transcription_status == "completed"

    def test_capped_user_keeps_the_entry_without_a_reply(self, tr):
        # The cap flips while the file is being transcribed (the flag is
        # set out of band, #341): the entry is kept, the reply skipped.
        from backend.utils.spend import current_month
        user_id, entry = tr._t["user"].id, tr._t["entry"]

        def transcribe_then_cap(**kwargs):
            # The task runs in its own app context (its own session); the
            # task's next commit persists this.
            _db.session.get(User, user_id).spend_blocked_month = (
                current_month(datetime.utcnow()))
            return type("R", (), {"text": "uploaded words"})()
        tr.OpenAI.return_value.audio.transcriptions.create.side_effect = (
            transcribe_then_cap)

        tr.transcribe_audio(MagicMock(), entry.id, tr._t["audio"],
                            "talk.m4a", auto_reply_model="gpt-5")

        _db.session.refresh(entry)
        assert entry.transcription_status == "completed"
        assert entry.get_content() == "uploaded words"
        assert Node.query.filter_by(node_type="llm").count() == 0
        assert json.loads(entry.llm_task_warnings) == [
            tr.UPLOAD_REPLY_SKIPPED_SPEND_CAP]

    def test_reply_failure_does_not_fail_the_transcript(self, tr, monkeypatch):
        import backend.utils.llm_nodes as llm_nodes
        entry = tr._t["entry"]

        def boom(*a, **k):
            raise RuntimeError("broker down")
        monkeypatch.setattr(llm_nodes, "create_llm_placeholder", boom)

        tr.transcribe_audio(MagicMock(), entry.id, tr._t["audio"],
                            "talk.m4a", auto_reply_model="gpt-5")

        _db.session.refresh(entry)
        assert entry.transcription_status == "completed"
        assert entry.get_content() == "uploaded words"
        assert json.loads(entry.llm_task_warnings) == ["broker down"]
