"""GET /profile/<id>/tts-chapters: chapters for a profile's TTS (#145).

Chapter titles are profile text, so the route has the audio route's
access rule: the owner only (403 for anyone else, admins included).
"""
import os
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]


@pytest.fixture
def env():
    from flask import Flask
    from flask_login import LoginManager
    from backend.extensions import db as _db
    from backend.models import User, UserProfile, TTSChunk

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.profile import profile_bp
    app.register_blueprint(profile_bp, url_prefix="/api/profile")

    with app.app_context():
        _db.create_all()
        owner = User(username="owner")
        other = User(username="other")
        admin = User(username="admin", is_admin=True)
        _db.session.add_all([owner, other, admin])
        _db.session.flush()
        profile = UserProfile(user_id=owner.id, generated_by="test",
                              tokens_used=0)
        profile.set_content("x")
        _db.session.add(profile)
        _db.session.flush()
        for idx, s_idx, s_title, dur in [
                (0, 0, "Who I am", 10.0), (1, 0, "Who I am", 20.0),
                (2, 1, "What I want", 5.0)]:
            _db.session.add(TTSChunk(
                profile_id=profile.id, chunk_index=idx, status="completed",
                section_index=s_idx, section_title=s_title, duration=dur))
        _db.session.commit()

        def get(user, path):
            client = app.test_client()
            if user is not None:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(user.id)
                    sess["_fresh"] = True
            return client.get(path)

        yield get, profile, owner, other, admin
        _db.session.rollback()
        _db.drop_all()


def test_owner_gets_chapters(env):
    get, profile, owner, _other, _admin = env
    resp = get(owner, f"/api/profile/{profile.id}/tts-chapters")
    assert resp.status_code == 200
    chapters = resp.get_json()["chapters"]
    assert [c["title"] for c in chapters] == ["Who I am", "What I want"]
    assert [c["start_time"] for c in chapters] == [0.0, 30.0]


def test_no_access_to_audio_means_no_chapters(env):
    get, profile, _owner, other, admin = env
    for user in (other, admin):
        audio = get(user, f"/api/profile/{profile.id}/audio")
        chapters = get(user, f"/api/profile/{profile.id}/tts-chapters")
        assert audio.status_code == 403
        assert chapters.status_code == audio.status_code
        assert "Who I am" not in chapters.get_data(as_text=True)


def test_missing_profile_404(env):
    get, _profile, owner, _other, _admin = env
    assert get(owner, "/api/profile/99999/tts-chapters").status_code == 404


def test_anonymous_gets_no_chapters(env):
    get, profile, _owner, _other, _admin = env
    resp = get(None, f"/api/profile/{profile.id}/tts-chapters")
    assert resp.status_code in (401, 302)
    assert "Who I am" not in resp.get_data(as_text=True)
