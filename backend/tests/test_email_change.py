"""Changing / adding the account email needs proof of control (#260):
POST /api/dashboard/email only sends a link to the NEW address, and the
address binds when /auth/magic-link/verify sees the bind claim."""
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
    app.config["FRONTEND_URL"] = "https://loore.org"
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.auth import auth_bp
    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    app = _make_app()
    with app.app_context():
        _db.create_all()
        alice = User(username="alice", email="alice@example.com",
                     approved=True, plan="alpha")
        bob = User(username="bob", email="bob@example.com", approved=True)
        xonly = User(username="xonly", twitter_id="123", approved=True)
        _db.session.add_all([alice, bob, xonly])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


@pytest.fixture
def mails(monkeypatch):
    """Capture outgoing mail: [(kind, to, url_or_new)]."""
    sent = []
    import backend.utils.email as email_mod
    monkeypatch.setattr(email_mod, "send_email_change_email",
                        lambda to, url: sent.append(("verify", to, url)))
    monkeypatch.setattr(email_mod, "send_email_changed_notice",
                        lambda old, new: sent.append(("notice", old, new)))
    return sent


def _user(name):
    return User.query.filter_by(username=name).first()


def _client(app, name):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return c


def _verify_url(mails):
    kind, to, url = mails[-1]
    assert kind == "verify"
    return url.split("token=")[1], to


def test_change_binds_only_after_the_link_is_opened(app, mails):
    c = _client(app, "alice")
    r = c.post("/api/dashboard/email", json={"email": " New@Example.com "})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["pending_email"] == "new@example.com"
    alice = _user("alice")
    assert alice.email == "alice@example.com"          # nothing bound yet
    assert alice.pending_email == "new@example.com"
    token, to = _verify_url(mails)
    assert to == "new@example.com"

    r = app.test_client().get(f"/auth/magic-link/verify?token={token}")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/account?email=verified")
    _db.session.expire_all()
    alice = _user("alice")
    assert alice.email == "new@example.com"
    assert alice.pending_email is None
    assert alice.magic_link_token_hash is None
    assert ("notice", "alice@example.com", "new@example.com") in mails

    # The link is single-use.
    r = app.test_client().get(f"/auth/magic-link/verify?token={token}")
    assert r.headers["Location"].endswith("/account?email=invalid_or_expired")


def test_x_login_user_adds_a_first_email_without_a_notice(app, mails):
    c = _client(app, "xonly")
    assert c.post("/api/dashboard/email",
                  json={"email": "x@example.com"}).status_code == 200
    token, _ = _verify_url(mails)
    app.test_client().get(f"/auth/magic-link/verify?token={token}")
    _db.session.expire_all()
    assert _user("xonly").email == "x@example.com"
    assert not [m for m in mails if m[0] == "notice"]


def test_uniqueness_is_enforced_at_send_and_at_bind(app, mails):
    c = _client(app, "alice")
    r = c.post("/api/dashboard/email", json={"email": "BOB@example.com"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "That email is already in use."
    assert not mails
    # Race: the address is free when the link is sent, taken by the time
    # it is opened.
    assert c.post("/api/dashboard/email",
                  json={"email": "late@example.com"}).status_code == 200
    token, _ = _verify_url(mails)
    _user("bob").email = "late@example.com"
    _db.session.commit()
    r = app.test_client().get(f"/auth/magic-link/verify?token={token}")
    assert r.headers["Location"].endswith("/account?email=taken")
    _db.session.expire_all()
    assert _user("alice").email == "alice@example.com"
    assert _user("alice").pending_email is None


def test_send_rejects_same_and_invalid_addresses(app, mails):
    c = _client(app, "alice")
    assert c.post("/api/dashboard/email",
                  json={"email": "alice@example.com"}).status_code == 400
    assert c.post("/api/dashboard/email",
                  json={"email": "not-an-email"}).status_code == 400
    assert not mails


def test_direct_bind_through_put_user_is_refused(app):
    c = _client(app, "alice")
    r = c.put("/api/dashboard/user", json={"email": "evil@example.com"})
    assert r.status_code == 400
    assert _user("alice").email == "alice@example.com"


def test_a_newer_request_invalidates_the_older_link(app, mails):
    c = _client(app, "alice")
    c.post("/api/dashboard/email", json={"email": "first@example.com"})
    first, _ = _verify_url(mails)
    c.post("/api/dashboard/email", json={"email": "second@example.com"})
    r = app.test_client().get(f"/auth/magic-link/verify?token={first}")
    assert r.headers["Location"].endswith("/account?email=invalid_or_expired")
    assert _user("alice").email == "alice@example.com"


def test_remove_email_needs_another_way_in(app, mails):
    c = _client(app, "alice")
    r = c.delete("/api/dashboard/email", json={"remove_bound": True})
    assert r.status_code == 400
    assert _user("alice").email == "alice@example.com"
    # Cancelling a pending change is always allowed.
    c.post("/api/dashboard/email", json={"email": "p@example.com"})
    assert c.delete("/api/dashboard/email").get_json()["pending_email"] is None
    assert _user("alice").pending_email is None
    # An X-login account may drop its email.
    xc = _client(app, "xonly")
    _user("xonly").email = "x@example.com"
    _db.session.commit()
    r = xc.delete("/api/dashboard/email", json={"remove_bound": True})
    assert r.status_code == 200
    assert _user("xonly").email is None
