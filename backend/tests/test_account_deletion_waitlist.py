"""A waitlisted (unapproved) account can delete itself (#269).

Every account that can be created can be deleted by its owner (the
Deletion rule; App Store guideline 5.1.1(v)), and every new account starts
on the waitlist. The deletion request, the emailed link's confirmation and
the restore question are open past the approval gate; nothing else is.

The approval gate only exists on create_app(), so these tests use the real
app, as the waitlist tests of test_email_change.py do. sqlite in memory;
mail and the public-page cache are stubbed.
"""
import os
import re
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, UserDataPurge  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

FRONTEND_URL = "https://loore.test"
CONFIRM_SUBJECT = "Confirm the deletion of your Loore account"
SIGN_IN_SUBJECT = "Your Loore sign-in link"
NOT_APPROVED = "Your account is not approved. Please wait for approval."


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


def _swap_in_real_modules():
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    return saved


def _restore_modules(saved):
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _seed():
    _db.session.add_all([
        # Waitlisted signups: by email link, and by X.
        User(username="waiting", email="waiting@example.com", approved=False),
        User(username="waiting_x", twitter_id="456", approved=False),
        User(username="root", email="root@example.com", approved=True,
             is_admin=True),
    ])
    _db.session.commit()


@pytest.fixture
def real_app(monkeypatch):
    """create_app() with the approval gate (block_unapproved_users), the
    database forced to sqlite on the Config class (see test_email_change)."""
    saved = _swap_in_real_modules()
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:", (
        "the real-app tests must never touch a real database")
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    import backend.utils.public_cache as public_cache
    monkeypatch.setattr(public_cache, "invalidate_for_user", lambda user: None)
    with app.app_context():
        _db.create_all()
        _seed()
        yield app
        _db.session.remove()
        _db.drop_all()
    _restore_modules(saved)


@pytest.fixture
def mails(monkeypatch):
    sent = []
    import backend.utils.email as email_mod
    monkeypatch.setattr(
        email_mod, "_deliver",
        lambda to, subject, text, html: sent.append(
            {"to": to, "subject": subject, "text": text}))
    return sent


def _user(name):
    _db.session.expire_all()
    return User.query.filter_by(username=name).first()


def _client(app, name):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return c


def _anonymous(app):
    from flask import g
    g.pop("_login_user", None)
    return app.test_client()


def _session_user_id(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def _link_token(mails, subject, path):
    mail = [m for m in mails if m["subject"] == subject][-1]
    return re.search(rf"{re.escape(path)}\?token=(\S+)", mail["text"]).group(1)


def _sign_in_by_link(client, mails, email):
    """The magic-link sign-in a deleted account makes to be offered a restore."""
    assert client.post("/auth/magic-link/send", json={"email": email}).status_code == 200
    token = _link_token(mails, SIGN_IN_SUBJECT, "/auth/magic-link/verify")
    r = client.get(f"/auth/magic-link/verify?token={token}")
    assert r.status_code == 302
    assert r.headers["Location"] == f"{FRONTEND_URL}/account-restore"


def _delete_by_email(app, mails, name):
    c = _client(app, name)
    r = c.post("/api/account/delete", json={"confirm": name})
    assert r.status_code == 202, r.get_json()
    token = _link_token(mails, CONFIRM_SUBJECT, "/confirm-account-deletion")
    r = c.post("/api/account/delete/confirm", json={"token": token})
    assert r.status_code == 202, r.get_json()
    return c


class TestWaitlistedAccountDeletesItself:
    def test_an_email_account_requests_and_confirms(self, real_app, mails):
        c = _client(real_app, "waiting")
        r = c.post("/api/account/delete", json={"confirm": "waiting"})
        assert r.status_code == 202, r.get_json()
        assert r.get_json()["status"] == "confirm_email"
        assert _user("waiting").deleted_at is None, "the request alone changes nothing"

        token = _link_token(mails, CONFIRM_SUBJECT, "/confirm-account-deletion")
        r = c.post("/api/account/delete/confirm", json={"token": token})
        assert r.status_code == 202, r.get_json()
        assert r.get_json()["status"] == "scheduled"
        waiting = _user("waiting")
        assert waiting.deleted_at is not None
        assert waiting.approved is False
        job = UserDataPurge.query.filter_by(user_id=waiting.id).one()
        assert job.delete_account is True and job.status == "scheduled"
        assert _session_user_id(c) is None, "signed out"

    def test_an_x_account_is_scheduled_at_once(self, real_app, mails):
        c = _client(real_app, "waiting_x")
        r = c.post("/api/account/delete", json={"confirm": "waiting_x"})
        assert r.status_code == 202, r.get_json()
        assert r.get_json()["status"] == "scheduled"
        assert _user("waiting_x").deleted_at is not None

    def test_the_typed_username_is_still_required(self, real_app, mails):
        c = _client(real_app, "waiting_x")
        r = c.post("/api/account/delete", json={"confirm": "someone"})
        assert r.status_code == 400
        assert _user("waiting_x").deleted_at is None

    def test_signing_in_during_the_grace_period_restores(self, real_app, mails):
        _delete_by_email(real_app, mails, "waiting")
        anon = _anonymous(real_app)
        _sign_in_by_link(anon, mails, "waiting@example.com")
        r = anon.get("/api/account/restore")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["restorable"] is True
        r = anon.post("/api/account/restore")
        assert r.status_code == 200, r.get_json()
        waiting = _user("waiting")
        assert waiting.deleted_at is None
        assert _session_user_id(anon) == str(waiting.id)
        # Restored to where it was: on the waitlist.
        r = anon.get("/api/nodes/1")
        assert r.status_code == 403
        assert r.get_json()["error"] == NOT_APPROVED

    def test_the_restore_question_works_with_another_waitlisted_account_signed_in(
            self, real_app, mails):
        # The restore routes act on the offer in the session. A browser
        # still signed in to another waitlisted account was refused by the
        # gate before the routes were exempt.
        _delete_by_email(real_app, mails, "waiting")
        c = _client(real_app, "waiting_x")
        _sign_in_by_link(c, mails, "waiting@example.com")
        r = c.get("/api/account/restore")
        assert r.status_code == 200, r.get_json()
        assert c.post("/api/account/restore/decline").status_code == 200
        assert c.get("/api/account/restore").status_code == 404, "the offer is gone"

        _sign_in_by_link(c, mails, "waiting@example.com")
        r = c.post("/api/account/restore")
        assert r.status_code == 200, r.get_json()
        assert _user("waiting").deleted_at is None
        assert _session_user_id(c) == str(_user("waiting").id)

    def test_everything_else_stays_gated(self, real_app, mails):
        c = _client(real_app, "waiting")
        for method, path in [
            ("GET", "/api/nodes/1"),
            # "Delete all my writing" is not part of deleting the account.
            ("GET", "/api/account/data"),
            ("DELETE", "/api/account/data"),
            ("POST", "/api/account/data/cancel"),
            # Only the exact routes and methods opened.
            ("GET", "/api/account/delete"),
            ("POST", "/api/account/deletes"),
            ("POST", "/api/account/delete/confirm/more"),
            ("DELETE", "/api/account/restore"),
            ("POST", "/api/dashboard/timezone"),
        ]:
            r = c.open(path, method=method, json={})
            assert r.status_code == 403, (method, path, r.status_code)
            assert r.get_json()["error"] == NOT_APPROVED, (method, path)
        assert _user("waiting").deleted_at is None
