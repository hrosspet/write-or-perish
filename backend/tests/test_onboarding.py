"""Tests for first-login onboarding completion tracking (#147)."""
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User  # noqa: E402


@pytest.fixture
def client():
    from flask_login import LoginManager
    from backend.routes.dashboard import dashboard_bp

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    with app.app_context():
        _db.create_all()
        user = User(username="newbie", approved=True)
        _db.session.add(user)
        _db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield client
        _db.session.rollback()
        _db.drop_all()


def test_complete_endpoint_idempotent(client):
    r1 = client.post("/api/dashboard/onboarding/complete")
    assert r1.status_code == 200
    user = User.query.first()
    first_stamp = user.onboarding_completed_at
    assert first_stamp is not None

    r2 = client.post("/api/dashboard/onboarding/complete")
    assert r2.status_code == 200
    assert User.query.first().onboarding_completed_at == first_stamp
