"""Tests for audio endpoint access control.

Verifies that:
- Any authenticated user can access audio on public nodes
- Only voice-mode users (admin or paid plan) can access audio on private nodes
- Free users are blocked from audio on private nodes
"""

import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

# Mock optional heavy deps that may not be installed locally
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest
from flask import Flask

# ── Force-import real modules ────────────────────────────────────────────
_SENTINEL = object()
for _mod in [k for k in list(sys.modules)
             if k == "flask_login" or k.startswith("backend.")]:
    _m = sys.modules[_mod]
    if isinstance(_m, MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402


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
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")

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
def data(app, tmp_path):
    """Create users and nodes with audio files on disk.

    - alice: free plan (no voice mode)
    - bob: alpha plan (has voice mode)
    - carol: author of nodes, alpha plan
    """
    alice = User(username="alice", approved=True, plan="free")
    bob = User(username="bob", approved=True, plan="alpha")
    carol = User(username="carol", approved=True, plan="alpha")
    _db.session.add_all([alice, bob, carol])
    _db.session.flush()

    # Create a public node with a TTS audio file
    public_node = Node(
        user_id=carol.id, content="Public post with audio",
        privacy_level="public", node_type="user",
        audio_tts_url=f"/media/user/{carol.id}/node/PLACEHOLDER/tts.mp3",
    )
    # Create a private node with a TTS audio file
    private_node = Node(
        user_id=carol.id, content="Private post with audio",
        privacy_level="private", node_type="user",
        audio_tts_url=f"/media/user/{carol.id}/node/PLACEHOLDER/tts.mp3",
    )
    # Public node without audio
    public_no_audio = Node(
        user_id=carol.id, content="Public post without audio",
        privacy_level="public", node_type="user",
    )

    _db.session.add_all([public_node, private_node, public_no_audio])
    _db.session.commit()

    # Fix up audio URLs now that we have node IDs
    public_node.audio_tts_url = f"/media/user/{carol.id}/node/{public_node.id}/tts.mp3"
    private_node.audio_tts_url = f"/media/user/{carol.id}/node/{private_node.id}/tts.mp3"
    _db.session.commit()

    # Create actual audio files on disk so the endpoint finds them
    from backend.routes.nodes import AUDIO_STORAGE_ROOT
    for node in [public_node, private_node]:
        audio_dir = AUDIO_STORAGE_ROOT / f"user/{carol.id}/node/{node.id}"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "tts.mp3").write_bytes(b"fake audio")

    return dict(
        alice_id=alice.id, bob_id=bob.id, carol_id=carol.id,
        public_node_id=public_node.id,
        private_node_id=private_node.id,
        public_no_audio_id=public_no_audio.id,
    )


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# ── GET /api/nodes/<id>/audio ────────────────────────────────────────────

class TestAudioAccessPublicNode:
    """Any authenticated user should be able to fetch audio URLs for public nodes."""

    def test_free_user_can_access_public_node_audio(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])  # alice is free plan

        resp = client.get(f"/api/nodes/{data['public_node_id']}/audio")
        assert resp.status_code == 200
        assert resp.json["tts_url"] is not None

    def test_paid_user_can_access_public_node_audio(self, app, data):
        client = app.test_client()
        _login(client, data["bob_id"])  # bob is pro plan

        resp = client.get(f"/api/nodes/{data['public_node_id']}/audio")
        assert resp.status_code == 200
        assert resp.json["tts_url"] is not None


class TestAudioAccessPrivateNode:
    """Only voice-mode users should be able to fetch audio for private nodes."""

    def test_free_user_blocked_from_private_node_audio(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])  # alice is free plan

        resp = client.get(f"/api/nodes/{data['private_node_id']}/audio")
        assert resp.status_code == 403

    def test_paid_user_can_access_private_node_audio(self, app, data):
        client = app.test_client()
        _login(client, data["bob_id"])  # bob is pro plan

        resp = client.get(f"/api/nodes/{data['private_node_id']}/audio")
        assert resp.status_code == 200
        assert resp.json["tts_url"] is not None


class TestAudioAccessUnauthenticated:
    """Unauthenticated requests should be rejected."""

    def test_unauthenticated_request_rejected(self, app, data):
        client = app.test_client()
        resp = client.get(f"/api/nodes/{data['public_node_id']}/audio")
        # Flask-Login returns 401 or redirects for unauthenticated users
        assert resp.status_code in (401, 302)
