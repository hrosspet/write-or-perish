"""Upload and session ids are plain names, streaming audio moves only
between one user's own draft and node, speech follows ai_usage, and routes
answer a node the user cannot see like a missing one.

Two users: alice owns the content, bob is another signed-in user with
voice mode. bob must not reach alice's files, transcript rows or private
nodes; alice's own requests keep working.

sqlite in-memory, minimal Flask app, ENCRYPTION_DISABLED; audio files live
under a tmp_path that stands in for AUDIO_STORAGE_PATH.
"""
import os
import sys
import types
import uuid
from datetime import datetime
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask, g, jsonify  # noqa: E402

# ── Force-import real modules ────────────────────────────────────────────
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, UserProfile, Draft, NodeTranscriptChunk,
)
import backend.models as _real_backend_models  # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["OPENAI_API_KEY"] = "sk-test"
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5",
                  "display_name": "GPT-5"},
    }

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({"error": "Unauthorized"}), 401

    from backend.routes.nodes import nodes_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.drafts import drafts_bp
    from backend.routes.voice import voice_bp
    from backend.routes.profile import profile_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(voice_bp, url_prefix="/api/voice")
    app.register_blueprint(profile_bp, url_prefix="/api/profile")
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Same sys.modules handling as test_detached_prompt_stays_agentic.py:
    # re-import the routes and backend.utils.privacy against the real
    # flask_login, then restore modules AND package attributes.
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.utils.privacy"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [
        k for k in list(sys.modules)
        if k.startswith("backend.routes") or k == "backend.utils.privacy"
    ]:
        del sys.modules[_k]

    app = _make_app()
    root = tmp_path / "audio"
    root.mkdir()
    import backend.routes.nodes as nodes_mod
    import backend.routes.drafts as drafts_mod
    import backend.utils.audio_storage as audio_storage
    monkeypatch.setattr(nodes_mod, "AUDIO_STORAGE_ROOT", root)
    monkeypatch.setattr(drafts_mod, "AUDIO_STORAGE_ROOT", root)
    monkeypatch.setattr(audio_storage, "AUDIO_STORAGE_ROOT", root)
    app.audio_root = root
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    current = [k for k in list(sys.modules) if _affected(k)]
    for k in current:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    for k in set(current) | set(saved):
        pkg_name, _, attr = k.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is None or not attr:
            continue
        if k in saved:
            setattr(pkg, attr, saved[k])
        elif hasattr(pkg, attr):
            delattr(pkg, attr)


# ── Helpers ──────────────────────────────────────────────────────────────

def _node(user, content, parent=None, privacy_level="private",
          node_type="user", human_owner=None, ai_usage="chat", **kwargs):
    n = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        token_count=1,
        **kwargs,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.commit()
    return n


def _write(app, rel_path, data=b"audio-bytes"):
    path = app.audio_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _call(app, user, method, url, **kwargs):
    """One request as *user* (None: not signed in). The fixture holds one
    app context, where Flask-Login caches the loaded user on g, so drop
    that cache before every request."""
    g.pop("_login_user", None)
    client = app.test_client()
    if user is not None:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
    return client.open(url, method=method, **kwargs)


def _session(user, text):
    """A finished streaming draft of *user* with one transcript chunk row
    and one audio chunk file on disk."""
    sid = str(uuid.uuid4())
    draft = Draft(user_id=user.id, session_id=sid,
                  streaming_status="completed")
    draft.set_content(text)
    chunk = NodeTranscriptChunk(session_id=sid, chunk_index=0,
                                status="completed")
    chunk.set_text(text)
    _db.session.add_all([draft, chunk])
    _db.session.commit()
    return draft, chunk


@pytest.fixture
def data(app):
    alice = User(username="alice", approved=True, plan="alpha",
                 public_sharing_enabled=True)
    bob = User(username="bob", approved=True, plan="alpha")
    llm = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add_all([alice, bob, llm])
    _db.session.commit()

    private = _node(alice, "ALICE PRIVATE ENTRY")
    bob_own = _node(bob, "BOB OWN ENTRY")
    alice_file = _write(
        app, f"user/{alice.id}/node/{private.id}/original.webm")
    return types.SimpleNamespace(
        alice=alice, bob=bob, llm=llm, private=private, bob_own=bob_own,
        alice_file=alice_file)


@pytest.fixture
def fake_tasks(monkeypatch):
    """Celery task modules the routes import lazily."""
    mods = {}
    for name, attrs in {
        "backend.tasks.transcription": ["transcribe_audio"],
        "backend.tasks.tts": ["generate_tts_audio",
                              "generate_tts_audio_for_profile"],
        "backend.tasks.llm_completion": ["generate_llm_response"],
    }.items():
        mod = types.ModuleType(name)
        for attr in attrs:
            task = MagicMock()
            task.delay.return_value.id = "task-1"
            setattr(mod, attr, task)
        monkeypatch.setitem(sys.modules, name, mod)
        mods[name.rsplit(".", 1)[-1]] = mod
    return types.SimpleNamespace(**mods)


def _load_tts_tasks(app):
    """A fresh copy of backend.tasks.tts whose tasks are plain functions
    (self first) running against *app*. sys.modules and the
    backend.tasks package attribute are restored afterwards, as in
    test_tts_slow_calls.py."""
    import backend.utils.audio_processing  # noqa: F401  (keeps real pydub)
    import backend.tasks as tasks_pkg

    celery_stub = MagicMock()
    celery_stub.Task = object
    celery_app = types.ModuleType("backend.celery_app")
    celery_app.celery = MagicMock()
    celery_app.celery.task = lambda *a, **k: (lambda f: f)
    celery_app.flask_app = app
    glue = {
        "celery": celery_stub,
        "celery.utils": MagicMock(),
        "celery.utils.log": MagicMock(),
        "pydub": MagicMock(),
        "backend.celery_app": celery_app,
    }
    names = list(glue) + ["backend.tasks.tts"]
    saved = {k: sys.modules.get(k) for k in names}
    had_attr = hasattr(tasks_pkg, "tts")
    saved_attr = getattr(tasks_pkg, "tts", None)
    try:
        sys.modules.update(glue)
        sys.modules.pop("backend.tasks.tts", None)
        import backend.tasks.tts as tts
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if had_attr:
            tasks_pkg.tts = saved_attr
        elif hasattr(tasks_pkg, "tts"):
            delattr(tasks_pkg, "tts")
    return tts


# ── Path construction ────────────────────────────────────────────────────

class TestStoragePath:

    def test_builds_paths_from_ids_and_plain_names(self, tmp_path):
        from backend.utils.audio_storage import storage_path
        sid = str(uuid.uuid4())
        assert storage_path(tmp_path, "drafts", 3, sid) == \
            tmp_path / "drafts" / "3" / sid
        assert storage_path(tmp_path, "chunks", 3, "1727000000000-ab12cd34e") \
            == tmp_path / "chunks" / "3" / "1727000000000-ab12cd34e"

    @pytest.mark.parametrize("bad", [
        "..", ".", "", "a/b", "../2", "/etc", "a\\b", ".hidden", "-x",
        "x" * 65, None, -1, True, 1.5,
    ])
    def test_refuses_anything_but_a_plain_name_or_id(self, tmp_path, bad):
        from backend.utils.audio_storage import storage_path
        with pytest.raises(ValueError):
            storage_path(tmp_path, "chunks", 3, bad)

    def test_storage_id_check(self):
        from backend.utils.audio_storage import is_storage_id
        assert is_storage_id(str(uuid.uuid4()))
        assert is_storage_id("1727000000000-ab12cd34e")      # web upload id
        assert is_storage_id("1727000000000-1a2b3c4d-")      # iOS upload id
        for bad in ("..", "a/b", "", None, 5, "a.b", "x" * 65):
            assert not is_storage_id(bad), bad


# ── Chunked upload ids ───────────────────────────────────────────────────

class TestChunkedUploadIds:

    def _escape(self, app, data):
        """An upload id that names alice's node folder from bob's
        chunks/<bob>/ folder, which exists once bob has started any
        upload."""
        (app.audio_root / f"chunks/{data.bob.id}/earlier").mkdir(parents=True)
        return f"../../user/{data.alice.id}/node/{data.private.id}"

    def test_init_refuses_an_upload_id_that_is_not_a_plain_name(
            self, app, data):
        before = Node.query.count()
        resp = _call(app, data.bob, "POST", "/api/nodes/upload/init", json={
            "filename": "a.webm", "filesize": 10, "total_chunks": 1,
            "upload_id": self._escape(app, data)})
        assert resp.status_code == 400
        assert Node.query.count() == before
        assert not (data.alice_file.parent / "metadata.json").exists()

    def test_chunk_refuses_an_upload_id_that_is_not_a_plain_name(
            self, app, data):
        resp = _call(app, data.bob, "POST", "/api/nodes/upload/chunk", data={
            "chunk": (__import__("io").BytesIO(b"x"), "c.bin"),
            "chunk_index": "0", "node_id": str(data.bob_own.id),
            "upload_id": self._escape(app, data)},
            content_type="multipart/form-data")
        assert resp.status_code == 400
        assert sorted(p.name for p in data.alice_file.parent.iterdir()) == \
            ["original.webm"]

    def test_finalize_refuses_an_upload_id_that_is_not_a_plain_name(
            self, app, data):
        resp = _call(app, data.bob, "POST", "/api/nodes/upload/finalize",
                     json={"upload_id": self._escape(app, data),
                           "node_id": data.bob_own.id})
        assert resp.status_code == 400
        assert data.alice_file.exists()

    def test_cleanup_refuses_an_upload_id_that_is_not_a_plain_name(
            self, app, data):
        self._escape(app, data)
        resp = _call(app, data.bob, "POST", "/api/nodes/upload/cleanup",
                     json={"upload_id": f"../../user/{data.alice.id}"})
        assert resp.status_code == 400
        assert data.alice_file.exists()

    @pytest.mark.parametrize("upload_id", [
        "1727000000000-ab12cd34e",    # web: Date.now()-base36
        "1727000000000-1a2b3c4d-",    # iOS: ms-UUID prefix
    ])
    def test_upload_with_client_ids_still_works(
            self, app, data, fake_tasks, upload_id):
        resp = _call(app, data.alice, "POST", "/api/nodes/upload/init", json={
            "filename": "a.webm", "filesize": 6, "total_chunks": 2,
            "upload_id": upload_id})
        assert resp.status_code == 201, resp.get_json()
        node_id = resp.get_json()["node_id"]
        import io
        for i, part in enumerate((b"abc", b"def")):
            resp = _call(app, data.alice, "POST", "/api/nodes/upload/chunk",
                         data={"chunk": (io.BytesIO(part), "c.bin"),
                               "chunk_index": str(i), "node_id": str(node_id),
                               "upload_id": upload_id},
                         content_type="multipart/form-data")
            assert resp.status_code == 200, resp.get_json()
        resp = _call(app, data.alice, "POST", "/api/nodes/upload/finalize",
                     json={"upload_id": upload_id, "node_id": node_id})
        assert resp.status_code == 200, resp.get_json()
        stored = (app.audio_root / f"user/{data.alice.id}/node/{node_id}"
                  / "original.webm")
        assert stored.read_bytes() == b"abcdef"
        assert not (app.audio_root / f"chunks/{data.alice.id}/{upload_id}"
                    ).exists()
        fake_tasks.transcription.transcribe_audio.delay.assert_called_once()

    def test_cleanup_with_a_plain_upload_id_still_works(self, app, data):
        d = app.audio_root / f"chunks/{data.alice.id}/1727-abc"
        d.mkdir(parents=True)
        resp = _call(app, data.alice, "POST", "/api/nodes/upload/cleanup",
                     json={"upload_id": "1727-abc"})
        assert resp.status_code == 200
        assert not d.exists()


# ── Streaming session ids ────────────────────────────────────────────────

class TestStreamingSessionIds:

    def test_draft_streaming_routes_answer_a_malformed_session_id_as_missing(
            self, app, data):
        resp = _call(app, data.alice, "DELETE",
                     "/api/drafts/streaming/a.b/discard")
        assert resp.status_code == 404
        resp = _call(app, data.alice, "POST",
                     "/api/drafts/streaming/a.b/audio-chunk")
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "session_not_found"


# ── Streaming audio moves to a node ──────────────────────────────────────

class TestAttachStreamingAudio:

    def test_voice_session_refuses_a_session_id_that_is_not_a_plain_name(
            self, app, data, fake_tasks):
        draft, _ = _session(data.alice, "ALICE TRANSCRIPT")
        alice_audio = _write(
            app, f"drafts/{data.alice.id}/{draft.session_id}/chunk_0000.webm")
        resp = _call(app, data.bob, "POST", "/api/voice/", json={
            "content": "bob words", "model": "gpt-5",
            "session_id": f"../{data.alice.id}/{draft.session_id}"})
        assert resp.status_code == 400
        assert alice_audio.exists()
        assert Node.query.filter_by(user_id=data.bob.id).count() == 1

    def test_voice_session_does_not_take_another_users_transcript_rows(
            self, app, data, fake_tasks):
        draft, chunk = _session(data.alice, "ALICE TRANSCRIPT")
        resp = _call(app, data.bob, "POST", "/api/voice/", json={
            "content": "bob words", "model": "gpt-5",
            "session_id": draft.session_id})
        assert resp.status_code == 202
        _db.session.refresh(chunk)
        assert chunk.node_id is None
        assert Draft.query.get(draft.id) is not None
        bob_node = Node.query.get(resp.get_json()["user_node_id"])
        assert not bob_node.streaming_transcription

    def test_voice_session_attaches_the_users_own_session(
            self, app, data, fake_tasks):
        draft, chunk = _session(data.alice, "ALICE TRANSCRIPT")
        _write(app,
               f"drafts/{data.alice.id}/{draft.session_id}/chunk_0000.webm")
        resp = _call(app, data.alice, "POST", "/api/voice/", json={
            "content": "ALICE TRANSCRIPT", "model": "gpt-5",
            "session_id": draft.session_id})
        assert resp.status_code == 202
        node_id = resp.get_json()["user_node_id"]
        _db.session.refresh(chunk)
        assert chunk.node_id == node_id
        assert Node.query.get(node_id).streaming_transcription
        assert (app.audio_root / f"nodes/{data.alice.id}/{node_id}"
                / "chunk_0000.webm").exists()
        assert Draft.query.filter_by(session_id=draft.session_id).count() == 0

    def test_a_node_receives_audio_only_from_its_owners_session(
            self, app, data):
        from backend.utils.audio_storage import attach_streaming_audio_to_node
        draft, chunk = _session(data.alice, "ALICE TRANSCRIPT")
        audio = _write(
            app, f"drafts/{data.alice.id}/{draft.session_id}/chunk_0000.webm")

        attach_streaming_audio_to_node(
            draft.session_id, data.bob_own, data.alice.id)

        _db.session.refresh(chunk)
        assert chunk.node_id is None
        assert audio.exists()
        assert Draft.query.get(draft.id) is not None
        assert not (app.audio_root
                    / f"nodes/{data.alice.id}/{data.bob_own.id}").exists()


# ── Speech and ai_usage ──────────────────────────────────────────────────

class TestSpeechFollowsAiUsage:

    def test_entry_with_ai_usage_none_gets_no_speech(
            self, app, data, fake_tasks):
        entry = _node(data.alice, "ALICE NO-AI ENTRY", ai_usage="none")
        resp = _call(app, data.alice, "POST", f"/api/nodes/{entry.id}/tts")
        assert resp.status_code == 403
        fake_tasks.tts.generate_tts_audio.delay.assert_not_called()
        _db.session.refresh(entry)
        assert entry.tts_task_status is None

    def test_entry_that_allows_ai_gets_speech(self, app, data, fake_tasks):
        entry = _node(data.alice, "ALICE CHAT ENTRY", ai_usage="chat")
        resp = _call(app, data.alice, "POST", f"/api/nodes/{entry.id}/tts")
        assert resp.status_code == 202
        fake_tasks.tts.generate_tts_audio.delay.assert_called_once()

    def test_model_reply_gets_speech_whatever_its_ai_usage(
            self, app, data, fake_tasks):
        """Voice mode speaks every reply, also in a thread that is 'none'."""
        entry = _node(data.alice, "ALICE NO-AI ENTRY", ai_usage="none")
        reply = _node(data.llm, "MODEL REPLY", parent=entry, node_type="llm",
                      human_owner=data.alice, ai_usage="none",
                      llm_model="gpt-5")
        resp = _call(app, data.alice, "POST", f"/api/nodes/{reply.id}/tts")
        assert resp.status_code == 202
        fake_tasks.tts.generate_tts_audio.delay.assert_called_once()

    def test_existing_speech_is_still_returned(self, app, data, fake_tasks):
        entry = _node(data.alice, "ALICE NO-AI ENTRY", ai_usage="none")
        entry.audio_tts_url = "/media/x.mp3"
        _db.session.commit()
        resp = _call(app, data.alice, "POST", f"/api/nodes/{entry.id}/tts")
        assert resp.status_code == 200
        assert resp.get_json()["tts_url"] == "/media/x.mp3"

    def test_profile_with_ai_usage_none_gets_no_speech(
            self, app, data, fake_tasks):
        profile = UserProfile(user_id=data.alice.id, generated_by="user",
                              tokens_used=0, ai_usage="none")
        profile.set_content("ALICE PROFILE")
        _db.session.add(profile)
        _db.session.commit()
        resp = _call(app, data.alice, "POST",
                     f"/api/profile/{profile.id}/tts")
        assert resp.status_code == 403
        fake_tasks.tts.generate_tts_audio_for_profile.delay.assert_not_called()

        profile.ai_usage = "chat"
        _db.session.commit()
        resp = _call(app, data.alice, "POST",
                     f"/api/profile/{profile.id}/tts")
        assert resp.status_code == 202

    def test_speech_job_checks_ai_usage_when_it_runs(self, app, data):
        """The job refuses a 'none' entry however it was queued, and still
        speaks a model's reply."""
        tts = _load_tts_tasks(app)
        speak = MagicMock(return_value="/media/new.mp3")
        tts._generate_tts_chunks = speak

        entry = _node(data.alice, "ALICE NO-AI ENTRY", ai_usage="none")
        result = tts.generate_tts_audio(None, entry.id, str(app.audio_root))
        assert result["status"] == "refused"
        speak.assert_not_called()
        _db.session.refresh(entry)
        assert entry.tts_task_status == "failed"
        assert entry.audio_tts_url is None

        reply = _node(data.llm, "MODEL REPLY", parent=entry, node_type="llm",
                      human_owner=data.alice, ai_usage="none",
                      llm_model="gpt-5")
        result = tts.generate_tts_audio(None, reply.id, str(app.audio_root))
        assert result["status"] == "completed"
        speak.assert_called_once()

        profile = UserProfile(user_id=data.alice.id, generated_by="user",
                              tokens_used=0, ai_usage="none")
        profile.set_content("ALICE PROFILE")
        _db.session.add(profile)
        _db.session.commit()
        result = tts.generate_tts_audio_for_profile(
            None, profile.id, str(app.audio_root))
        assert result["status"] == "refused"
        assert speak.call_count == 1

    def test_speech_rule(self, app, data):
        from backend.utils.privacy import speech_allowed
        assert not speech_allowed(types.SimpleNamespace(
            node_type="user", ai_usage="none"))
        assert speech_allowed(types.SimpleNamespace(
            node_type="user", ai_usage="chat"))
        assert speech_allowed(types.SimpleNamespace(
            node_type="llm", ai_usage="none"))
        # A saved reference has no ai_usage setting.
        assert speech_allowed(types.SimpleNamespace(source="clip"))
