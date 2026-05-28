"""Tests for stale-TTS invalidation on content edit (#66).

When a node's (or profile's) text content is edited after TTS audio was
generated, the generated audio is stale and must be dropped — both the
scalar `audio_tts_url` AND the per-chunk `TTSChunk` rows used by the
streaming player (clearing only the URL would leave chunked replay
resurfacing the old audio). A privacy/ai_usage-only edit must NOT drop it.

Patterned after test_node_deletion.py: sqlite in-memory, minimal Flask
app, ENCRYPTION_DISABLED.
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

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

# ── Force-import real modules ────────────────────────────────────────────
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node, UserProfile, TTSChunk  # noqa: E402
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
    from backend.routes.profile import profile_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(profile_bp, url_prefix="/profile")

    return app


@pytest.fixture
def app():
    # `update_node` checks `can_user_edit_node(node)` WITHOUT an explicit
    # user_id, so it reads `current_user` bound inside backend.utils.privacy
    # at import time. Some other test files (e.g. test_profile_privacy.py)
    # leave `flask_login` swapped to a MagicMock in sys.modules, which makes
    # privacy.py's `current_user` a mock and turns every edit into a 403.
    # Re-import flask_login, backend.models, backend.utils.privacy, and
    # backend.routes.* fresh against the real flask_login; restore at teardown.
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


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _make_node_with_tts(user, content="hello world"):
    node = Node(
        user_id=user.id,
        human_owner_id=user.id,
        node_type="user",
        privacy_level="private",
        ai_usage="none",
        token_count=1,
        audio_tts_url="data/audio/node_1/tts.mp3",
        tts_task_status="completed",
    )
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    _db.session.add_all([
        TTSChunk(node_id=node.id, chunk_index=0,
                 audio_url="data/audio/node_1/chunk_0.mp3", status="completed"),
        TTSChunk(node_id=node.id, chunk_index=1,
                 audio_url="data/audio/node_1/chunk_1.mp3", status="completed"),
    ])
    _db.session.commit()
    return node


def _make_profile_with_tts(user, content="my profile"):
    profile = UserProfile(
        user_id=user.id,
        generated_by="user",
        tokens_used=0,
        privacy_level="private",
        ai_usage="chat",
        audio_tts_url="data/audio/profile_1/tts.mp3",
        tts_task_status="completed",
    )
    profile.set_content(content)
    _db.session.add(profile)
    _db.session.commit()
    _db.session.add(
        TTSChunk(profile_id=profile.id, chunk_index=0,
                 audio_url="data/audio/profile_1/chunk_0.mp3", status="completed")
    )
    _db.session.commit()
    return profile


# ── Node content edit clears TTS ─────────────────────────────────────────

def test_node_content_edit_clears_tts_url_and_chunks(app, alice):
    node = _make_node_with_tts(alice, content="original text")
    client = app.test_client()
    _login(client, alice)

    resp = client.put(f"/nodes/{node.id}", json={"content": "edited text"})
    assert resp.status_code == 200

    refreshed = Node.query.get(node.id)
    assert refreshed.audio_tts_url is None
    assert refreshed.tts_task_status is None
    assert TTSChunk.query.filter_by(node_id=node.id).count() == 0


def test_node_privacy_only_edit_preserves_tts(app, alice):
    node = _make_node_with_tts(alice, content="keep me")
    client = app.test_client()
    _login(client, alice)

    # Same content, only privacy changes — audio is still valid.
    resp = client.put(
        f"/nodes/{node.id}",
        json={"content": "keep me", "privacy_level": "public"},
    )
    assert resp.status_code == 200

    refreshed = Node.query.get(node.id)
    assert refreshed.audio_tts_url == "data/audio/node_1/tts.mp3"
    assert TTSChunk.query.filter_by(node_id=node.id).count() == 2


# ── Profile content edit clears TTS ──────────────────────────────────────

def test_profile_content_edit_clears_tts_url_and_chunks(app, alice):
    profile = _make_profile_with_tts(alice, content="original profile")
    client = app.test_client()
    _login(client, alice)

    resp = client.put(
        f"/profile/{profile.id}", json={"content": "edited profile"}
    )
    assert resp.status_code == 200

    refreshed = UserProfile.query.get(profile.id)
    assert refreshed.audio_tts_url is None
    assert TTSChunk.query.filter_by(profile_id=profile.id).count() == 0
