"""The tweets-seed opt-in (prefill_consent) asked on /alpha-thank-you.

- PUT /dashboard/user accepts "yes"/"no" only and stamps prefill_consent_at.
- The dashboard user payload exposes twitter_login + prefill_consent so
  the card can gate itself (X-login users, unanswered).
- The admin user list carries the answer.
Follows the real-app + sqlite pattern from test_admin_access.py.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User  # noqa: E402
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

    from backend.routes.admin import admin_bp
    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
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
def users(app):
    tw = User(username="birdperson", twitter_id="12345", approved=False)
    mail = User(username="mailperson", email="m@example.com", approved=False)
    admin = User(username="boss", approved=True, is_admin=True)
    _db.session.add_all([tw, mail, admin])
    _db.session.commit()
    return {"tw": tw, "mail": mail, "admin": admin}


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


class TestPrefillConsent:
    def test_twitter_user_can_answer_yes(self, app, users):
        client = app.test_client()
        _login(client, users["tw"].id)
        resp = client.put("/api/dashboard/user", json={"prefill_consent": "yes"})
        assert resp.status_code == 200
        u = resp.get_json()["user"]
        assert u["prefill_consent"] == "yes"
        assert u["twitter_login"] is True
        assert users["tw"].prefill_consent_at is not None

    def test_no_is_a_real_answer(self, app, users):
        client = app.test_client()
        _login(client, users["tw"].id)
        resp = client.put("/api/dashboard/user", json={"prefill_consent": "no"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["prefill_consent"] == "no"

    def test_rejects_other_values(self, app, users):
        client = app.test_client()
        _login(client, users["tw"].id)
        for bad in ("maybe", True, None, ""):
            resp = client.put("/api/dashboard/user", json={"prefill_consent": bad})
            assert resp.status_code == 400, bad
        assert users["tw"].prefill_consent is None

    def test_email_user_is_flagged_non_twitter(self, app, users):
        client = app.test_client()
        _login(client, users["mail"].id)
        resp = client.put("/api/dashboard/user", json={"description": "hi"})
        assert resp.status_code == 200
        u = resp.get_json()["user"]
        assert u["twitter_login"] is False
        assert u["prefill_consent"] is None

    def test_admin_list_carries_answer(self, app, users):
        users["tw"].prefill_consent = "yes"
        _db.session.commit()
        client = app.test_client()
        _login(client, users["admin"].id)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        by_name = {u["username"]: u for u in resp.get_json()["users"]}
        assert by_name["birdperson"]["prefill_consent"] == "yes"
        assert by_name["mailperson"]["prefill_consent"] is None
