"""Routes serve a node's media, audio and content only to users who may see
the node, and a node can only be created under a parent the user can see.

Two users: alice owns the content, bob is another signed-in user with
voice mode. Anonymous requests come from a client with no session. bob and
anonymous visitors must get 404 for alice's private node, its files, her
profile and her saved references; alice keeps access to all of it, and
everyone keeps access to her public node and its audio.

sqlite in-memory, minimal Flask app, ENCRYPTION_DISABLED; media files live
under a tmp_path that stands in for AUDIO_STORAGE_PATH.
"""
import os
import sys
import types
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
    User, Node, UserProfile, ExternalItem, Draft,
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
    from backend.routes.media import media_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.drafts import drafts_bp
    from backend.routes.textmode import textmode_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(media_bp, url_prefix="/media")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    return app


@pytest.fixture
def app(tmp_path):
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
    media_root = tmp_path / "media"
    media_root.mkdir()
    import backend.routes.media as media_mod
    import backend.routes.nodes as nodes_mod
    saved_roots = (media_mod.MEDIA_ROOT, nodes_mod.AUDIO_STORAGE_ROOT)
    media_mod.MEDIA_ROOT = media_root
    nodes_mod.AUDIO_STORAGE_ROOT = media_root
    app.media_root = media_root
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()
    media_mod.MEDIA_ROOT, nodes_mod.AUDIO_STORAGE_ROOT = saved_roots

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
          node_type="user", human_owner=None, **kwargs):
    n = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        privacy_level=privacy_level,
        ai_usage="chat",
        token_count=1,
        **kwargs,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.commit()
    return n


def _write(app, rel_path, data=b"audio-bytes"):
    path = app.media_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel_path


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


@pytest.fixture
def data(app):
    alice = User(username="alice", approved=True, plan="alpha",
                 public_sharing_enabled=True)
    bob = User(username="bob", approved=True, plan="alpha")
    llm = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add_all([alice, bob, llm])
    _db.session.commit()

    private = _node(alice, "ALICE PRIVATE ENTRY", streaming_transcription=True)
    reply = _node(llm, "ALICE PRIVATE REPLY", parent=private,
                  node_type="llm", human_owner=alice, llm_model="gpt-5")
    public = _node(alice, "ALICE PUBLIC ENTRY", privacy_level="public")
    bob_own = _node(bob, "BOB OWN ENTRY")

    profile = UserProfile(user_id=alice.id, generated_by="test",
                          tokens_used=0)
    profile.set_content("ALICE PROFILE TEXT")
    item = ExternalItem(user_id=alice.id, source="clip", external_id="1",
                        url="https://example.com/a")
    item.set_content("ALICE SAVED REFERENCE")
    _db.session.add_all([profile, item])
    _db.session.commit()

    files = {
        "private_tts": _write(app, f"user/{alice.id}/node/{private.id}/tts.mp3"),
        "private_chunk": _write(
            app, f"nodes/{alice.id}/{private.id}/chunk_0000.webm"),
        "reply_tts": _write(app, f"user/{llm.id}/node/{reply.id}/tts.mp3"),
        "public_tts": _write(app, f"user/{alice.id}/node/{public.id}/tts.mp3"),
        "profile_tts": _write(
            app, f"user/{alice.id}/profile/{profile.id}/tts.mp3"),
        "item_tts": _write(app, f"user/{alice.id}/item/{item.id}/tts.mp3"),
        "draft_chunk": _write(
            app, f"drafts/{alice.id}/session-a/chunk_0000.webm"),
        "unknown": _write(app, "misc/file.mp3"),
    }
    private.audio_tts_url = f"/media/{files['private_tts']}?v=1"
    public.audio_tts_url = f"/media/{files['public_tts']}?v=1"
    _db.session.commit()

    return types.SimpleNamespace(
        alice=alice, bob=bob, llm=llm, private=private, reply=reply,
        public=public, bob_own=bob_own, profile=profile, item=item,
        files=files)


# ── /media/<path> ────────────────────────────────────────────────────────

class TestMediaFiles:

    def test_owner_gets_private_files_with_private_cache_control(self, app, data):
        for key in ("private_tts", "private_chunk", "reply_tts",
                    "profile_tts", "item_tts", "draft_chunk"):
            resp = _call(app, data.alice, "GET", f"/media/{data.files[key]}")
            assert resp.status_code == 200, key
            assert resp.headers["Cache-Control"].startswith("private"), key
            assert "public" not in resp.headers["Cache-Control"], key

    def test_other_user_gets_404_for_private_files(self, app, data):
        for key in ("private_tts", "private_chunk", "reply_tts",
                    "profile_tts", "item_tts", "draft_chunk"):
            resp = _call(app, data.bob, "GET", f"/media/{data.files[key]}")
            assert resp.status_code == 404, key
            assert resp.data != b"audio-bytes", key

    def test_anonymous_gets_404_for_private_files(self, app, data):
        for key in ("private_tts", "private_chunk", "reply_tts",
                    "profile_tts", "item_tts", "draft_chunk"):
            resp = _call(app, None, "GET", f"/media/{data.files[key]}")
            assert resp.status_code == 404, key

    def test_public_node_audio_is_served_to_everyone(self, app, data):
        url = f"/media/{data.files['public_tts']}?v=1"
        for user in (data.alice, data.bob, None):
            resp = _call(app, user, "GET", url)
            assert resp.status_code == 200
            assert resp.data == b"audio-bytes"
        resp = _call(app, None, "GET", url)
        assert resp.headers["Cache-Control"].startswith("public")

    def test_public_node_audio_needs_the_author_public_sharing_for_anonymous(
            self, app, data):
        """Signed out, a public node's audio follows the public pages'
        rule: the author must have public sharing switched on."""
        data.alice.public_sharing_enabled = False
        _db.session.commit()
        url = f"/media/{data.files['public_tts']}"
        assert _call(app, None, "GET", url).status_code == 404
        resp = _call(app, data.bob, "GET", url)
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"].startswith("private")

    def test_cache_busting_query_still_served(self, app, data):
        resp = _call(app, data.alice, "GET",
                     f"/media/{data.files['private_tts']}?v=12345")
        assert resp.status_code == 200
        assert resp.data == b"audio-bytes"

    def test_range_request_still_served(self, app, data):
        resp = _call(app, data.alice, "GET",
                     f"/media/{data.files['private_tts']}",
                     headers={"Range": "bytes=0-4"})
        assert resp.status_code == 206
        assert resp.data == b"audio"

    def test_file_outside_known_layouts_is_refused(self, app, data):
        resp = _call(app, data.alice, "GET", f"/media/{data.files['unknown']}")
        assert resp.status_code == 404

    def test_missing_file_and_refused_file_look_the_same(self, app, data):
        missing = _call(app, data.bob, "GET",
                        f"/media/user/{data.alice.id}/node/999999/tts.mp3")
        refused = _call(app, data.bob, "GET",
                        f"/media/{data.files['private_tts']}")
        assert missing.status_code == refused.status_code == 404
        assert missing.get_json() == refused.get_json()

    def test_path_outside_media_root_is_refused(self, app, data):
        (app.media_root.parent / "outside.mp3").write_bytes(b"outside")
        resp = _call(app, data.alice, "GET",
                     f"/media/user/{data.alice.id}/../../../outside.mp3")
        assert resp.status_code == 404


# Ways a client can spell a ".." segment in the URL path.
_DOTDOT_FORMS = ("..", "%2e%2e", "%2E%2E", ".%2e", "%2e.")


def _assert_refused(resp):
    assert resp.status_code == 404
    assert resp.data != b"audio-bytes"
    assert "public" not in resp.headers.get("Cache-Control", "")


class TestMediaAccessIsDecidedOnTheResolvedPath:
    """The access rule is decided on the path the file is opened from,
    after ``..`` segments and symlinks are resolved."""

    @pytest.mark.parametrize("dotdot", _DOTDOT_FORMS)
    def test_path_from_own_folder_into_another_users_file_is_refused(
            self, app, data, dotdot):
        _write(app, f"user/{data.bob.id}/node/{data.bob_own.id}/tts.mp3")
        url = (f"/media/user/{data.bob.id}/{dotdot}/{data.alice.id}/node/"
               f"{data.private.id}/tts.mp3")
        _assert_refused(_call(app, data.bob, "GET", url))

    @pytest.mark.parametrize("dotdot", _DOTDOT_FORMS)
    def test_path_from_public_node_folder_into_private_node_is_refused(
            self, app, data, dotdot):
        url = (f"/media/user/{data.alice.id}/node/{data.public.id}/{dotdot}/"
               f"{data.private.id}/tts.mp3")
        for user in (None, data.bob):
            _assert_refused(_call(app, user, "GET", url))

    def test_dot_segments_are_refused_even_for_the_owner(self, app, data):
        alice, private = data.alice.id, data.private.id
        for url in (
            f"/media/user/{alice}/node/{private}/../{private}/tts.mp3",
            f"/media/user/{alice}/./node/{private}/tts.mp3",
            f"/media/user/{alice}/%2e/node/{private}/tts.mp3",
            f"/media/user/{alice}/node/{private}/%252e%252e/{private}/tts.mp3",
        ):
            assert _call(app, data.alice, "GET", url).status_code == 404, url

    def test_symlink_to_another_users_file_is_refused(self, app, data):
        link = (app.media_root
                / f"user/{data.bob.id}/node/{data.bob_own.id}/tts.mp3")
        link.parent.mkdir(parents=True)
        link.symlink_to(app.media_root / data.files["private_tts"])
        url = f"/media/user/{data.bob.id}/node/{data.bob_own.id}/tts.mp3"
        _assert_refused(_call(app, data.bob, "GET", url))
        # The owner of the file it points to still gets it.
        resp = _call(app, data.alice, "GET", url)
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"].startswith("private")

    def test_symlink_out_of_the_media_root_is_refused(self, app, data):
        outside = app.media_root.parent / "outside.mp3"
        outside.write_bytes(b"outside")
        link = app.media_root / f"user/{data.alice.id}/node/{data.private.id}/x.mp3"
        link.symlink_to(outside)
        resp = _call(app, data.alice, "GET",
                     f"/media/user/{data.alice.id}/node/{data.private.id}/x.mp3")
        assert resp.status_code == 404
        assert resp.data != b"outside"


@pytest.fixture
def encrypted(app, data, monkeypatch):
    """Encryption on with a stand-in for KMS, and the private, public and
    chunk files stored the way production stores them: ``<name>.enc``."""
    from backend.utils import encryption
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek",
                        lambda w: w[len(b"wrapped:"):])
    for key in ("private_tts", "public_tts", "private_chunk"):
        path = app.media_root / data.files[key]
        assert encryption.encrypt_file(str(path)) == str(path) + ".enc"
        assert not path.exists()
    return data


class TestEncryptedMediaFiles:

    def test_owner_gets_decrypted_private_file(self, app, encrypted):
        for key in ("private_tts", "private_chunk"):
            resp = _call(app, encrypted.alice, "GET",
                         f"/media/{encrypted.files[key]}?v=3")
            assert resp.status_code == 200, key
            assert resp.data == b"audio-bytes", key
            assert resp.headers["Cache-Control"].startswith("private"), key

    def test_range_request_on_encrypted_file(self, app, encrypted):
        resp = _call(app, encrypted.alice, "GET",
                     f"/media/{encrypted.files['private_tts']}?v=3",
                     headers={"Range": "bytes=0-4"})
        assert resp.status_code == 206
        assert resp.data == b"audio"
        assert resp.headers["Content-Range"] == "bytes 0-4/11"
        assert resp.headers["Content-Type"] == "audio/mpeg"
        assert resp.headers["Cache-Control"].startswith("private")

    def test_public_encrypted_file_is_served_to_everyone(self, app, encrypted):
        resp = _call(app, None, "GET", f"/media/{encrypted.files['public_tts']}")
        assert resp.status_code == 200
        assert resp.data == b"audio-bytes"
        assert resp.headers["Cache-Control"].startswith("public")

    def test_other_users_get_404_for_encrypted_private_file(self, app, encrypted):
        url = f"/media/{encrypted.files['private_tts']}"
        for user in (None, encrypted.bob):
            _assert_refused(_call(app, user, "GET", url))

    @pytest.mark.parametrize("dotdot", _DOTDOT_FORMS)
    def test_dot_segments_into_encrypted_private_file_are_refused(
            self, app, encrypted, dotdot):
        d = encrypted
        _write(app, f"user/{d.bob.id}/node/{d.bob_own.id}/tts.mp3")
        _assert_refused(_call(
            app, d.bob, "GET",
            f"/media/user/{d.bob.id}/{dotdot}/{d.alice.id}/node/"
            f"{d.private.id}/tts.mp3"))
        for user in (None, d.bob):
            _assert_refused(_call(
                app, user, "GET",
                f"/media/user/{d.alice.id}/node/{d.public.id}/{dotdot}/"
                f"{d.private.id}/tts.mp3"))


# ── /api/nodes/<id>/audio, /audio-chunks, /audio-download, /tts ──────────

class TestNodeAudioRoutes:

    def test_other_user_gets_404_for_private_node_audio_urls(self, app, data):
        nid = data.private.id
        for url in (f"/api/nodes/{nid}/audio",
                    f"/api/nodes/{nid}/audio-chunks",
                    f"/api/nodes/{nid}/audio-download"):
            resp = _call(app, data.bob, "GET", url)
            assert resp.status_code == 404, url
            assert "ALICE" not in resp.get_data(as_text=True), url

    def test_owner_gets_private_node_audio_urls(self, app, data):
        resp = _call(app, data.alice, "GET",
                     f"/api/nodes/{data.private.id}/audio")
        assert resp.status_code == 200
        assert resp.get_json()["tts_url"].startswith("/media/")
        resp = _call(app, data.alice, "GET",
                     f"/api/nodes/{data.private.id}/audio-chunks")
        assert resp.status_code == 200
        assert len(resp.get_json()["chunks"]) == 1

    def test_other_user_gets_public_node_audio_urls(self, app, data):
        resp = _call(app, data.bob, "GET",
                     f"/api/nodes/{data.public.id}/audio")
        assert resp.status_code == 200
        assert resp.get_json()["tts_url"].startswith("/media/")

    def test_other_user_cannot_request_speech_for_private_node(
            self, app, data, monkeypatch):
        fake_tts = types.ModuleType("backend.tasks.tts")
        fake_tts.generate_tts_audio = MagicMock()
        monkeypatch.setitem(sys.modules, "backend.tasks.tts", fake_tts)
        target = _node(data.alice, "ALICE PRIVATE, NO AUDIO YET")

        resp = _call(app, data.bob, "POST", f"/api/nodes/{target.id}/tts")
        assert resp.status_code == 404
        fake_tts.generate_tts_audio.delay.assert_not_called()

    def test_voice_user_can_request_speech_for_public_node(
            self, app, data, monkeypatch):
        fake_tts = types.ModuleType("backend.tasks.tts")
        fake_tts.generate_tts_audio = MagicMock()
        fake_tts.generate_tts_audio.delay.return_value.id = "task-1"
        monkeypatch.setitem(sys.modules, "backend.tasks.tts", fake_tts)
        target = _node(data.alice, "ALICE PUBLIC, NO AUDIO YET",
                       privacy_level="public")

        resp = _call(app, data.bob, "POST", f"/api/nodes/{target.id}/tts")
        assert resp.status_code == 202
        kwargs = fake_tts.generate_tts_audio.delay.call_args.kwargs
        assert kwargs["requesting_user_id"] == data.bob.id

    def test_other_user_gets_404_for_suggested_model(self, app, data):
        resp = _call(app, data.bob, "GET",
                     f"/api/nodes/{data.private.id}/suggested-model")
        assert resp.status_code == 404
        resp = _call(app, data.alice, "GET",
                     f"/api/nodes/{data.private.id}/suggested-model")
        assert resp.status_code == 200


# ── /api/nodes/upload/* (chunked upload) ─────────────────────────────────

# Formats the clients send: web "<ms>-<base36>", iOS "<ms>-<UUID prefix>".
_WEB_UPLOAD_ID = "1727712000000-k3j2h1g0f"
_IOS_UPLOAD_ID = "1727712000000-1a2b3c4d-"


def _bad_upload_ids(data, json=True):
    alice = data.alice.id
    ids = (
        f"../../user/{alice}/node/{data.private.id}",
        f"../{alice}/{_WEB_UPLOAD_ID}",
        "..", ".", "a/b", "up.1", "/tmp", "%2e%2e", "up-1\n",
        "x" * 65,
    )
    # A JSON body can carry a value that is not a string at all.
    return ids + (12345, ["up-1"]) if json else ids


def _tree(root):
    """Every file and folder under *root*, with file contents."""
    return {p.relative_to(root).as_posix():
            (p.read_bytes() if p.is_file() else None)
            for p in root.rglob("*")}


@pytest.fixture
def transcription(monkeypatch):
    fake = types.ModuleType("backend.tasks.transcription")
    fake.transcribe_audio = MagicMock()
    fake.transcribe_audio.delay.return_value.id = "task-1"
    monkeypatch.setitem(sys.modules, "backend.tasks.transcription", fake)
    return fake


def _init_upload(app, user, upload_id, total_chunks=1, filesize=4):
    return _call(app, user, "POST", "/api/nodes/upload/init", json={
        "filename": "talk.m4a", "filesize": filesize,
        "total_chunks": total_chunks, "upload_id": upload_id})


def _send_chunk(app, user, upload_id, node_id, index, payload):
    from io import BytesIO
    return _call(app, user, "POST", "/api/nodes/upload/chunk", data={
        "chunk": (BytesIO(payload), "blob"), "chunk_index": str(index),
        "upload_id": upload_id, "node_id": str(node_id),
    }, content_type="multipart/form-data")


@pytest.fixture
def uploads(app, data):
    """alice has an upload in progress; bob has started one of his own."""
    resp = _init_upload(app, data.alice, _WEB_UPLOAD_ID)
    assert resp.status_code == 201
    alice_node = resp.get_json()["node_id"]
    assert _send_chunk(app, data.alice, _WEB_UPLOAD_ID, alice_node, 0,
                       b"ALCE").status_code == 200
    resp = _init_upload(app, data.bob, _IOS_UPLOAD_ID)
    assert resp.status_code == 201
    data.alice_upload_dir = (
        app.media_root / f"chunks/{data.alice.id}/{_WEB_UPLOAD_ID}")
    data.bob_node = resp.get_json()["node_id"]
    return data


class TestChunkedUploadFolder:
    """Each upload route reads, writes and deletes only inside the
    caller's own chunks/<user id>/<upload_id> folder."""

    def test_init_refuses_bad_upload_ids(self, app, uploads):
        before, nodes = _tree(app.media_root.parent), Node.query.count()
        for upload_id in _bad_upload_ids(uploads):
            resp = _init_upload(app, uploads.bob, upload_id)
            assert resp.status_code == 400, upload_id
        assert Node.query.count() == nodes
        assert _tree(app.media_root.parent) == before

    def test_chunk_refuses_bad_upload_ids(self, app, uploads):
        before = _tree(app.media_root.parent)
        for upload_id in _bad_upload_ids(uploads, json=False):
            resp = _send_chunk(app, uploads.bob, upload_id, uploads.bob_node,
                               0, b"BOB!")
            assert resp.status_code == 400, upload_id
        assert _tree(app.media_root.parent) == before

    def test_finalize_refuses_bad_upload_ids(self, app, uploads):
        before = _tree(app.media_root.parent)
        for upload_id in _bad_upload_ids(uploads):
            resp = _call(app, uploads.bob, "POST", "/api/nodes/upload/finalize",
                         json={"upload_id": upload_id,
                               "node_id": uploads.bob_node})
            assert resp.status_code == 400, upload_id
        assert _tree(app.media_root.parent) == before

    def test_cleanup_refuses_bad_upload_ids(self, app, uploads):
        before = _tree(app.media_root.parent)
        for upload_id in _bad_upload_ids(uploads):
            resp = _call(app, uploads.bob, "POST", "/api/nodes/upload/cleanup",
                         json={"upload_id": upload_id})
            assert resp.status_code == 400, upload_id
        assert _tree(app.media_root.parent) == before
        assert (app.media_root / uploads.files["private_tts"]).exists()
        assert uploads.alice_upload_dir.is_dir()

    def test_another_users_upload_id_names_the_callers_own_folder(
            self, app, uploads):
        before = _tree(uploads.alice_upload_dir)
        resp = _send_chunk(app, uploads.bob, _WEB_UPLOAD_ID, uploads.bob_node,
                           0, b"BOB!")
        assert resp.status_code == 404
        resp = _call(app, uploads.bob, "POST", "/api/nodes/upload/finalize",
                     json={"upload_id": _WEB_UPLOAD_ID,
                           "node_id": uploads.bob_node})
        assert resp.status_code == 404
        resp = _call(app, uploads.bob, "POST", "/api/nodes/upload/cleanup",
                     json={"upload_id": _WEB_UPLOAD_ID})
        assert resp.status_code == 200
        assert _tree(uploads.alice_upload_dir) == before

    def test_symlinked_upload_folder_is_refused(self, app, uploads):
        target = app.media_root / f"user/{uploads.alice.id}/node/{uploads.private.id}"
        (app.media_root / f"chunks/{uploads.bob.id}/linked").symlink_to(
            target, target_is_directory=True)
        resp = _call(app, uploads.bob, "POST", "/api/nodes/upload/cleanup",
                     json={"upload_id": "linked"})
        assert resp.status_code == 400
        assert (app.media_root / uploads.files["private_tts"]).exists()

    @pytest.mark.parametrize("upload_id", [
        _WEB_UPLOAD_ID, _IOS_UPLOAD_ID, "x" * 64, "up_1"])
    def test_upload_runs_end_to_end(self, app, data, transcription, upload_id):
        resp = _init_upload(app, data.bob, upload_id, total_chunks=2,
                            filesize=8)
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()["upload_id"] == upload_id
        node_id = resp.get_json()["node_id"]
        chunk_dir = app.media_root / f"chunks/{data.bob.id}/{upload_id}"
        assert chunk_dir.is_dir()

        for index, payload in enumerate((b"abcd", b"efgh")):
            resp = _send_chunk(app, data.bob, upload_id, node_id, index,
                               payload)
            assert resp.status_code == 200, resp.get_data(as_text=True)
        resp = _call(app, data.bob, "POST", "/api/nodes/upload/finalize",
                     json={"upload_id": upload_id, "node_id": node_id})
        assert resp.status_code == 200, resp.get_data(as_text=True)

        original = app.media_root / f"user/{data.bob.id}/node/{node_id}/original.m4a"
        assert original.read_bytes() == b"abcdefgh"
        assert not chunk_dir.exists()
        transcription.transcribe_audio.delay.assert_called_once()

    def test_cleanup_removes_the_callers_own_upload(self, app, uploads):
        resp = _call(app, uploads.bob, "POST", "/api/nodes/upload/cleanup",
                     json={"upload_id": _IOS_UPLOAD_ID})
        assert resp.status_code == 200
        assert not (app.media_root
                    / f"chunks/{uploads.bob.id}/{_IOS_UPLOAD_ID}").exists()
        assert uploads.alice_upload_dir.is_dir()


# ── Removed routes ───────────────────────────────────────────────────────

class TestRemovedRoutes:

    def test_children_previews_route_is_gone(self, app, data):
        resp = _call(app, data.bob, "GET",
                     f"/api/nodes/{data.private.id}/children")
        assert resp.status_code == 404
        assert "ALICE" not in resp.get_data(as_text=True)

    def test_nodes_media_helper_is_gone(self, app, data):
        resp = _call(app, None, "GET",
                     f"/api/nodes/media/{data.files['private_tts']}")
        assert resp.status_code == 404
        assert resp.data != b"audio-bytes"


# ── /api/dashboard/<username> ────────────────────────────────────────────

class TestPublicDashboard:

    def test_profile_is_returned_only_to_its_owner(self, app, data):
        resp = _call(app, data.bob, "GET", "/api/dashboard/alice")
        assert resp.status_code == 200
        assert resp.get_json()["latest_profile"] is None
        assert "ALICE PROFILE TEXT" not in resp.get_data(as_text=True)

        resp = _call(app, data.alice, "GET", "/api/dashboard/alice")
        assert resp.get_json()["latest_profile"]["content"] == \
            "ALICE PROFILE TEXT"
        # The clients turn the speaker icon off for a 'none' version.
        assert resp.get_json()["latest_profile"]["ai_usage"] == "chat"

    def test_session_card_does_not_preview_a_private_first_message(
            self, app, data):
        root = _node(data.alice, "(system)", privacy_level="public",
                     prompt_key="textmode")
        _node(data.alice, "ALICE PRIVATE FIRST MESSAGE", parent=root)

        resp = _call(app, data.bob, "GET", "/api/dashboard/alice")
        assert resp.status_code == 200
        assert "ALICE PRIVATE FIRST MESSAGE" not in resp.get_data(as_text=True)

        resp = _call(app, data.alice, "GET", "/api/dashboard/alice")
        assert "ALICE PRIVATE FIRST MESSAGE" in resp.get_data(as_text=True)


# ── Creating nodes under a client-supplied parent ────────────────────────

class TestCreateUnderParent:

    def test_reply_under_private_node_of_another_user_is_refused(self, app, data):
        resp = _call(app, data.bob, "POST", "/api/nodes/",
                     json={"content": "bob reply",
                           "parent_id": data.private.id})
        assert resp.status_code == 404
        assert Node.query.filter_by(parent_id=data.private.id,
                                    user_id=data.bob.id).count() == 0

    def test_reply_under_visible_parents_still_works(self, app, data):
        resp = _call(app, data.bob, "POST", "/api/nodes/",
                     json={"content": "bob public reply",
                           "parent_id": data.public.id,
                           "privacy_level": "public"})
        assert resp.status_code == 201
        resp = _call(app, data.alice, "POST", "/api/nodes/",
                     json={"content": "alice reply",
                           "parent_id": data.private.id})
        assert resp.status_code == 201

    def test_link_requires_access_to_both_nodes(self, app, data):
        resp = _call(app, data.bob, "POST",
                     f"/api/nodes/{data.private.id}/link",
                     json={"linked_node_id": data.bob_own.id})
        assert resp.status_code == 404
        resp = _call(app, data.bob, "POST",
                     f"/api/nodes/{data.bob_own.id}/link",
                     json={"linked_node_id": data.private.id})
        assert resp.status_code == 404
        assert Node.query.filter_by(node_type="link").count() == 0

        resp = _call(app, data.bob, "POST",
                     f"/api/nodes/{data.bob_own.id}/link",
                     json={"linked_node_id": data.public.id})
        assert resp.status_code == 201

    def test_streaming_draft_init_refuses_invisible_parent(self, app, data):
        resp = _call(app, data.bob, "POST", "/api/drafts/streaming/init",
                     json={"parent_id": data.private.id})
        assert resp.status_code == 404
        assert Draft.query.filter_by(user_id=data.bob.id).count() == 0

    def test_streaming_finalize_refuses_invisible_parent(
            self, app, data, monkeypatch):
        fake = types.ModuleType("backend.tasks.streaming_transcription")
        fake.finalize_draft_streaming = MagicMock()
        fake.finalize_draft_streaming.delay.return_value.id = "task-1"
        monkeypatch.setitem(
            sys.modules, "backend.tasks.streaming_transcription", fake)
        draft = Draft(user_id=data.bob.id, session_id="session-b",
                      streaming_status="recording")
        draft.set_content("")
        _db.session.add(draft)
        _db.session.commit()

        resp = _call(app, data.bob, "POST",
                     "/api/drafts/streaming/session-b/finalize",
                     json={"total_chunks": 1, "label": "Voice",
                           "model": "gpt-5", "parent_id": data.private.id})
        assert resp.status_code == 404
        fake.finalize_draft_streaming.delay.assert_not_called()

    def test_save_as_node_refuses_invisible_parent(self, app, data):
        draft = Draft(user_id=data.bob.id, session_id="session-c",
                      streaming_status="completed",
                      parent_id=data.private.id)
        draft.set_content("bob recording")
        _db.session.add(draft)
        _db.session.commit()

        resp = _call(app, data.bob, "POST",
                     "/api/drafts/streaming/session-c/save-as-node", json={})
        assert resp.status_code == 404
        assert Node.query.filter_by(parent_id=data.private.id,
                                    user_id=data.bob.id).count() == 0

    def test_textmode_chain_stops_at_nodes_the_user_cannot_see(self, app, data):
        """A node of bob's that hangs below alice's private thread (e.g.
        created before parents were checked) must not hand bob the
        thread's content."""
        mid = _node(data.alice, "ALICE MID ENTRY", parent=data.private)
        bob_child = _node(data.bob, "bob child", parent=mid)

        resp = _call(app, data.bob, "GET",
                     f"/api/textmode/from-node/{bob_child.id}")
        assert resp.status_code == 200
        assert "ALICE" not in resp.get_data(as_text=True)
