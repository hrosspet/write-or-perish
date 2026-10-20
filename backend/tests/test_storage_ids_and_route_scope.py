"""Streaming session ids are plain folder names, streaming audio moves
only between one user's own draft and node, speech follows ai_usage, and routes
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
    User, Node, UserProfile, Draft, NodeTranscriptChunk, ExternalItem,
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
    from backend.routes.sse import sse_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(voice_bp, url_prefix="/api/voice")
    app.register_blueprint(profile_bp, url_prefix="/api/profile")
    app.register_blueprint(sse_bp, url_prefix="/api/sse")
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
        assert storage_path(tmp_path, "nodes", 3, 41) == \
            tmp_path / "nodes" / "3" / "41"

    @pytest.mark.parametrize("bad", [
        "..", ".", "", "a/b", "../2", "/etc", "a\\b", ".hidden", "-x",
        "x" * 65, None, -1, True, 1.5,
    ])
    def test_refuses_anything_but_a_plain_name_or_id(self, tmp_path, bad):
        from backend.utils.audio_storage import storage_path
        with pytest.raises(ValueError):
            storage_path(tmp_path, "drafts", 3, bad)

    def test_storage_id_check(self):
        from backend.utils.audio_storage import is_storage_id
        assert is_storage_id(str(uuid.uuid4()))
        assert is_storage_id("sess-1_a")
        for bad in ("..", "a/b", "", None, 5, "a.b", "x" * 65):
            assert not is_storage_id(bad), bad


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

    def test_model_reply_follows_its_ai_usage(self, app, data, fake_tasks):
        """A reply marked 'none' (imported, or set by its owner: none is
        generated any more, 2026-10-01) gets no new speech; one marked
        'chat' does."""
        entry = _node(data.alice, "ALICE ENTRY", ai_usage="chat")
        reply = _node(data.llm, "MODEL REPLY", parent=entry, node_type="llm",
                      human_owner=data.alice, ai_usage="none",
                      llm_model="gpt-5")
        resp = _call(app, data.alice, "POST", f"/api/nodes/{reply.id}/tts")
        assert resp.status_code == 403
        fake_tasks.tts.generate_tts_audio.delay.assert_not_called()

        reply.ai_usage = "chat"
        _db.session.commit()
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
        """The job refuses a 'none' entry or reply however it was queued,
        and speaks a reply AI may read."""
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
        assert result["status"] == "refused"
        speak.assert_not_called()

        reply.ai_usage = "chat"
        _db.session.commit()
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

    def test_listen_speaks_links_in_node_profile_and_reference(
            self, app, data):
        """#461: every Listen source runs the same link step: a Markdown
        link is spoken as its text, a bare GitHub PR address as "PR n",
        another address as "a link to <domain>". The stored text is not
        changed. TTS itself is mocked."""
        tts = _load_tts_tasks(app)
        spoken = []
        tts._generate_tts_chunks = (
            lambda task, entity, text, *a, **k: (
                spoken.append(text) or "/media/new.mp3"))
        pr = "https://github.com/hrosspet/write-or-perish/pull/460"
        source = (f"See [the docs](https://example.com/a) and {pr} "
                  "or https://www.example.org/x.")
        want = "See the docs and PR 460 or a link to example.org."

        entry = _node(data.alice, source)
        tts.generate_tts_audio(None, entry.id, str(app.audio_root))

        profile = UserProfile(user_id=data.alice.id, generated_by="user",
                              tokens_used=0, ai_usage="chat")
        profile.set_content(source)
        _db.session.add(profile)
        _db.session.commit()
        tts.generate_tts_audio_for_profile(
            None, profile.id, str(app.audio_root))

        item = ExternalItem(user_id=data.alice.id, source="web_clip",
                            external_id="a" * 64, title="Clip",
                            url="https://example.com/clip")
        item.set_content(source)
        _db.session.add(item)
        _db.session.commit()
        tts.generate_tts_audio_for_item(None, item.id, str(app.audio_root))

        assert spoken == [want, want, "# Clip\n\n" + want]
        assert entry.get_content() == profile.get_content() == source
        assert item.get_content() == source

    def test_speech_rule(self, app, data):
        from backend.utils.privacy import speech_allowed
        assert not speech_allowed(types.SimpleNamespace(
            node_type="user", ai_usage="none"))
        assert speech_allowed(types.SimpleNamespace(
            node_type="user", ai_usage="chat"))
        assert not speech_allowed(types.SimpleNamespace(
            node_type="llm", ai_usage="none"))
        assert speech_allowed(types.SimpleNamespace(
            node_type="llm", ai_usage="chat"))
        # A saved reference has no ai_usage setting.
        assert speech_allowed(types.SimpleNamespace(source="clip"))


# ── A node the user cannot see answers like a missing one ───────────────

class TestNotFoundForInvisibleNodes:

    def _deleted_private(self, data):
        n = _node(data.alice, "ALICE DELETED ENTRY")
        n.deleted_at = datetime.utcnow()
        _db.session.commit()
        return n

    def test_resolve_quotes(self, app, data):
        url = f"/api/nodes/{data.private.id}/resolve-quotes"
        refused = _call(app, data.bob, "GET", url)
        missing = _call(app, data.bob, "GET",
                        "/api/nodes/999999/resolve-quotes")
        assert refused.status_code == missing.status_code == 404
        assert "ALICE" not in refused.get_data(as_text=True)
        assert _call(app, data.alice, "GET", url).status_code == 200

    def test_save_draft_for_a_node_the_user_cannot_edit(self, app, data):
        deleted = self._deleted_private(data)
        missing = _call(app, data.bob, "POST", "/api/drafts/",
                        json={"content": "x", "node_id": 999999})
        assert missing.status_code == 404
        for nid in (data.private.id, deleted.id):
            resp = _call(app, data.bob, "POST", "/api/drafts/",
                         json={"content": "x", "node_id": nid})
            assert resp.status_code == 404, nid
            assert resp.get_json() == missing.get_json()
        assert Draft.query.filter_by(user_id=data.bob.id).count() == 0

    def test_save_draft_under_a_parent_the_user_cannot_see(self, app, data):
        deleted = self._deleted_private(data)
        missing = _call(app, data.bob, "POST", "/api/drafts/",
                        json={"content": "x", "parent_id": 999999})
        assert missing.status_code == 404
        for pid in (data.private.id, deleted.id):
            resp = _call(app, data.bob, "POST", "/api/drafts/",
                         json={"content": "x", "parent_id": pid})
            assert resp.status_code == 404, pid
            assert resp.get_json() == missing.get_json()
        assert Draft.query.filter_by(user_id=data.bob.id).count() == 0

    def test_owner_still_saves_drafts_and_sees_deleted_targets_as_410(
            self, app, data):
        deleted = self._deleted_private(data)
        resp = _call(app, data.alice, "POST", "/api/drafts/",
                     json={"content": "x", "parent_id": data.private.id})
        assert resp.status_code == 200
        resp = _call(app, data.alice, "POST", "/api/drafts/",
                     json={"content": "x", "node_id": data.private.id})
        assert resp.status_code == 200
        for field in ("node_id", "parent_id"):
            resp = _call(app, data.alice, "POST", "/api/drafts/",
                         json={"content": "x", field: deleted.id})
            assert resp.status_code == 410, field

    def test_load_and_delete_draft_for_a_node_the_user_cannot_edit(
            self, app, data):
        for method in ("GET", "DELETE"):
            resp = _call(app, data.bob, method,
                         f"/api/drafts/?node_id={data.private.id}")
            assert resp.status_code == 404, method


# ── Thread view counts ───────────────────────────────────────────────────

def _alive_below(d):
    """Alive nodes in the serialized subtree below *d* (tombstones only
    anchor what is under them)."""
    return sum((0 if c.get("deleted") else 1) + _alive_below(c)
               for c in d.get("children", []))


def _assert_counts_match_tree(d):
    """child_count and descendant_count of *d* and of every node under it
    describe the tree that was returned, nothing more."""
    assert d["child_count"] == len(d["children"]), d["id"]
    if "descendant_count" in d:
        assert d["descendant_count"] == _alive_below(d), d["id"]
    for c in d["children"]:
        _assert_counts_match_tree(c)


class TestThreadViewCounts:
    """GET /api/nodes/<id> counts only children and descendants the viewer
    can see."""

    @pytest.fixture
    def tree(self, data):
        a = data.alice
        root = _node(a, "ALICE PUBLIC ROOT", privacy_level="public")
        hidden = _node(a, "alice private reply 1", parent=root)
        _node(a, "public under private", parent=hidden,
              privacy_level="public")
        _node(a, "alice private reply 2", parent=root)
        reply = _node(a, "alice public reply", parent=root,
                      privacy_level="public")
        _node(a, "private under public", parent=reply)
        shown = _node(a, "public under public", parent=reply,
                      privacy_level="public")
        _node(a, "public leaf", parent=shown, privacy_level="public")
        return types.SimpleNamespace(root=root, hidden=hidden, reply=reply,
                                     shown=shown)

    def _get(self, app, viewer, node):
        resp = _call(app, viewer, "GET", f"/api/nodes/{node.id}")
        assert resp.status_code == 200
        return resp.get_json()

    def test_other_user_counts_only_what_they_can_see(self, app, data, tree):
        opened = self._get(app, data.bob, tree.reply)
        assert [a["child_count"] for a in opened["ancestors"]] == [1]
        assert opened["child_count"] == 1
        assert opened["children"][0]["descendant_count"] == 1
        _assert_counts_match_tree(opened)

        root = self._get(app, data.bob, tree.root)
        assert root["child_count"] == 1
        assert root["children"][0]["id"] == tree.reply.id
        assert root["children"][0]["descendant_count"] == 2
        _assert_counts_match_tree(root)

    def test_owner_counts_are_unchanged(self, app, data, tree):
        opened = self._get(app, data.alice, tree.reply)
        assert [a["child_count"] for a in opened["ancestors"]] == [3]
        assert opened["child_count"] == 2

        root = self._get(app, data.alice, tree.root)
        assert root["child_count"] == 3
        by_id = {c["id"]: c for c in root["children"]}
        assert by_id[tree.reply.id]["descendant_count"] == 3
        assert by_id[tree.hidden.id]["descendant_count"] == 1
        # Largest subtree first.
        assert root["children"][0]["id"] == tree.reply.id
        _assert_counts_match_tree(root)

    def test_deleted_children_count_as_the_tree_shows_them(
            self, app, data, tree):
        a = data.alice
        leaf = _node(a, "deleted leaf", parent=tree.root,
                     privacy_level="public")
        anchor = _node(a, "deleted with a reply", parent=tree.reply,
                       privacy_level="public")
        below = _node(a, "alive under deleted", parent=anchor,
                      privacy_level="public")
        leaf.deleted_at = anchor.deleted_at = datetime.utcnow()
        _db.session.commit()

        for viewer in (data.bob, data.alice):
            root = self._get(app, viewer, tree.root)
            _assert_counts_match_tree(root)
            reply = self._get(app, viewer, tree.reply)
            # The deleted leaf is pruned; the deleted node with a live
            # reply shows as a tombstone and is counted as a child.
            assert leaf.id not in [c["id"] for c in root["children"]]
            assert anchor.id in [c["id"] for c in reply["children"]]
            # Seen from below, each ancestor's count matches what the
            # viewer gets when opening that ancestor.
            deep = self._get(app, viewer, below)
            entries = {e["id"]: e for e in deep["ancestors"]}
            assert entries[anchor.id]["deleted"] is True
            assert entries[tree.reply.id]["child_count"] == \
                reply["child_count"]
            assert entries[tree.root.id]["child_count"] == \
                root["child_count"]


# ── No admin exception on status and stream routes ──────────────────────

class TestAdminsOpenOnlyWhatAnyUserCan:
    """An admin reads another user's node, profile or draft through the
    status and stream routes exactly as any other user would: a private
    one answers 404, the same as a missing one. Owners keep access."""

    @pytest.fixture
    def admin(self, data):
        admin = User(username="site-admin", approved=True, plan="alpha",
                     is_admin=True)
        _db.session.add(admin)
        _db.session.commit()
        return admin

    @pytest.fixture
    def owned(self, data):
        node = data.private
        node.transcription_status = "completed"
        node.llm_task_status = "completed"
        profile = UserProfile(user_id=data.alice.id, generated_by="user",
                              tokens_used=0)
        profile.set_content("ALICE PROFILE")
        draft = Draft(user_id=data.alice.id, session_id=str(uuid.uuid4()),
                      streaming_status="completed")
        draft.set_content("ALICE DRAFT")
        _db.session.add_all([profile, draft])
        _db.session.commit()
        return types.SimpleNamespace(node=node, profile=profile, draft=draft)

    @staticmethod
    def _urls(node_id, profile_id, session_id):
        return [
            f"/api/nodes/{node_id}/transcription-status",
            f"/api/nodes/{node_id}/llm-status",
            f"/api/nodes/{node_id}/tts-status",
            f"/api/nodes/{node_id}/streaming-status",
            f"/api/nodes/{node_id}/tts-chapters",
            f"/api/sse/nodes/{node_id}/transcription-stream",
            f"/api/sse/nodes/{node_id}/tts-stream",
            f"/api/sse/nodes/{node_id}/llm-stream",
            f"/api/sse/profiles/{profile_id}/tts-stream",
            f"/api/sse/drafts/{session_id}/transcription-stream",
        ]

    def test_admin_gets_404_like_any_other_user(self, app, data, admin, owned):
        urls = self._urls(owned.node.id, owned.profile.id,
                          owned.draft.session_id)
        missing = self._urls(999999, 999999, str(uuid.uuid4()))
        for url, missing_url in zip(urls, missing):
            gone = _call(app, admin, "GET", missing_url)
            assert gone.status_code == 404, missing_url
            for viewer in (admin, data.bob):
                resp = _call(app, viewer, "GET", url)
                assert resp.status_code == 404, (viewer.username, url)
                assert resp.get_json() == gone.get_json(), url
                assert "ALICE" not in resp.get_data(as_text=True)

    def test_owner_keeps_access(self, app, data, owned):
        expected = [200, 200, 200,
                    400,  # not a streaming-transcription node
                    200,
                    400,  # streaming transcription not enabled
                    400,  # no TTS run for this node
                    200,  # stream that ends at once: the reply is done
                    400,  # no TTS run for this profile
                    200]  # draft stream
        urls = self._urls(owned.node.id, owned.profile.id,
                          owned.draft.session_id)
        for url, status in zip(urls, expected):
            resp = _call(app, data.alice, "GET", url)
            assert resp.status_code == status, url
            resp.close()
        resp = _call(app, data.alice, "GET",
                     f"/api/nodes/{owned.node.id}/llm-status")
        assert resp.get_json()["content"] == "ALICE PRIVATE ENTRY"
