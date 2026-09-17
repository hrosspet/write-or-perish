"""Changing / adding the account email needs proof of control (#260).

POST /api/dashboard/email only mails a link to the NEW address. The address
binds when that link's token is POSTed to /api/dashboard/email/confirm from
inside a session of the account that asked: the link alone binds nothing
and signs nobody in, so a mistyped address, a mail scanner or a request for
someone else's address changes nothing.

Most tests use a bare app (auth + dashboard + admin blueprints). The
approval gate only exists on create_app(), so the waitlist tests use the
real app, following test_x_login_linking.py.
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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
from sqlalchemy.exc import IntegrityError  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

FRONTEND_URL = "https://loore.test"


def _make_app():
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.auth import auth_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.admin import admin_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    return app


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
        User(username="alice", email="alice@example.com", approved=True,
             plan="alpha"),
        User(username="bob", email="bob@example.com", approved=True),
        User(username="xonly", twitter_id="123", approved=True),
        User(username="root", email="root@example.com", approved=True,
             is_admin=True),
        # A waitlisted X signup: what a whitelist placeholder is once its
        # owner has logged in, and what any fresh X signup is.
        User(username="waiting", twitter_id="456", approved=False),
    ])
    _db.session.commit()


@pytest.fixture
def app():
    saved = _swap_in_real_modules()
    app = _make_app()
    with app.app_context():
        _db.create_all()
        _seed()
        yield app
        _db.session.remove()
        _db.drop_all()
    _restore_modules(saved)


@pytest.fixture
def real_app(monkeypatch):
    """create_app(): everything the bare app has plus the approval gate
    (block_unapproved_users). The database is forced to sqlite on the
    Config class itself — see the fixture of the same name in
    test_x_login_linking.py for why DATABASE_URL alone is not enough."""
    saved = _swap_in_real_modules()
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:", (
        "the real-app tests must never touch a real database")
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    with app.app_context():
        _db.create_all()
        _seed()
        yield app
        _db.session.remove()
        _db.drop_all()
    _restore_modules(saved)


@pytest.fixture
def mails(monkeypatch):
    """Every mail the app hands to SMTP: [{to, subject, text, html}]. Patched
    below the senders, so their real bodies are what the tests read."""
    sent = []
    import backend.utils.email as email_mod
    monkeypatch.setattr(
        email_mod, "_deliver",
        lambda to, subject, text, html: sent.append(
            {"to": to, "subject": subject, "text": text, "html": html}))
    return sent


CONFIRM_SUBJECT = "Confirm your email for Loore"
IN_USE_SUBJECT = "This address already signs in to Loore"
CHANGED_SUBJECT = "Your Loore sign-in email changed"


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


def _anonymous(app):
    from flask import g
    g.pop("_login_user", None)
    return app.test_client()


def _session_user_id(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def _token(mails):
    """The token in the last confirmation mail, and the address it went to."""
    mail = [m for m in mails if m["subject"] == CONFIRM_SUBJECT][-1]
    link = re.search(r"(\S+)/confirm-email\?token=(\S+)", mail["text"])
    assert link.group(1) == FRONTEND_URL  # a page of the app, not an API GET
    assert link.group(2) in mail["html"]
    return link.group(2), mail["to"]


def _request(client, address):
    return client.post("/api/dashboard/email", json={"email": address})


def _confirm(client, token):
    return client.post("/api/dashboard/email/confirm", json={"token": token})


def test_change_binds_only_when_confirmed_inside_the_account(app, mails):
    c = _client(app, "alice")
    r = _request(c, " New@Example.com ")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["pending_email"] == "new@example.com"
    assert r.get_json()["pending_email_expired"] is False
    alice = _user("alice")
    assert alice.email == "alice@example.com"          # nothing bound yet
    assert alice.pending_email == "new@example.com"
    assert alice.magic_link_token_hash is None         # its own columns
    token, to = _token(mails)
    assert to == "new@example.com"

    r = _confirm(c, token)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["email"] == "new@example.com"
    assert r.get_json()["pending_email"] is None
    _db.session.expire_all()
    alice = _user("alice")
    assert alice.email == "new@example.com"
    assert alice.pending_email is None
    assert alice.email_change_token_hash is None
    assert alice.email_change_expires_at is None
    notices = [m for m in mails if m["subject"] == CHANGED_SUBJECT]
    assert [m["to"] for m in notices] == ["alice@example.com"]
    assert "new@example.com" in notices[0]["text"]

    # A second click (or a double submit) says "confirmed" again, not
    # "invalid", and sends no second notice.
    r = _confirm(c, token)
    assert r.status_code == 200, r.get_json()
    assert len([m for m in mails if m["subject"] == CHANGED_SUBJECT]) == 1


def test_the_link_alone_binds_nothing_and_signs_nobody_in(app, mails):
    """Whoever receives the mail — a stranger at a mistyped address, a mail
    scanner, the owner of an address someone else asked for — can do
    nothing with it outside the account that asked."""
    _request(_client(app, "alice"), "new@example.com")
    token, _ = _token(mails)
    users_before = User.query.count()

    # Not signed in: refused.
    anon = _anonymous(app)
    assert _confirm(anon, token).status_code == 401
    # The sign-in route does not take it for a sign-in link: nobody is
    # signed in, and no account is created for the address.
    r = anon.get(f"/auth/magic-link/verify?token={token}")
    assert r.status_code == 302
    assert r.headers["Location"] == f"{FRONTEND_URL}/login?error=invalid_or_expired"
    assert _session_user_id(anon) is None
    # Signed in to another account: refused, and that session stays bob's.
    bob = _client(app, "bob")
    r = _confirm(bob, token)
    assert r.status_code == 403
    assert r.get_json()["reason"] == "other_account"
    assert bob.get("/api/dashboard/").get_json()["user"]["username"] == "bob"

    _db.session.expire_all()
    assert User.query.count() == users_before
    assert _user("alice").email == "alice@example.com"
    assert _user("alice").pending_email == "new@example.com"
    assert _user("bob").email == "bob@example.com"
    assert not [m for m in mails if m["subject"] == CHANGED_SUBJECT]
    # The link still works for alice afterwards: nothing above used it up.
    assert _confirm(_client(app, "alice"), token).status_code == 200


def test_x_login_user_adds_a_first_email_without_a_notice(app, mails):
    c = _client(app, "xonly")
    assert _request(c, "x@example.com").status_code == 200
    token, _ = _token(mails)
    assert _confirm(c, token).status_code == 200
    _db.session.expire_all()
    assert _user("xonly").email == "x@example.com"
    assert not [m for m in mails if m["subject"] == CHANGED_SUBJECT]


def test_an_address_in_use_gets_the_same_answer_and_no_link(app, mails):
    """The endpoint must not tell a logged-in user which addresses have
    accounts. Only the inbox's owner learns the address is in use."""
    c = _client(app, "alice")
    free = _request(c, "free@example.com")
    mails.clear()
    used = _request(c, "BOB@example.com")

    assert used.status_code == free.status_code == 200
    assert used.get_json() == {
        **free.get_json(),
        "message": free.get_json()["message"].replace(
            "free@example.com", "bob@example.com"),
        "pending_email": "bob@example.com"}
    assert [(m["to"], m["subject"]) for m in mails] == [
        ("bob@example.com", IN_USE_SUBJECT)]
    assert "token=" not in mails[0]["text"] + mails[0]["html"]
    alice = _user("alice")
    assert alice.pending_email == "bob@example.com"
    assert alice.email_change_token_hash is None  # nothing to confirm
    # The pending state reads the same as for a free address.
    state = c.get("/api/dashboard/").get_json()["user"]
    assert state["pending_email"] == "bob@example.com"
    assert state["pending_email_expired"] is False


def test_address_taken_between_the_request_and_the_confirmation(app, mails):
    c = _client(app, "alice")
    assert _request(c, "late@example.com").status_code == 200
    token, _ = _token(mails)
    _user("bob").email = "late@example.com"
    _db.session.commit()

    r = _confirm(c, token)

    # Holding the token proves control of the address: saying it is taken
    # tells them nothing they could not already find out.
    assert r.status_code == 409
    assert r.get_json()["reason"] == "taken"
    _db.session.expire_all()
    assert _user("alice").email == "alice@example.com"
    assert _user("alice").pending_email is None


def test_two_confirmations_racing_for_one_address(app, mails, monkeypatch):
    """The check passes, then the other account's commit lands first: the
    unique constraint answers, and the loser is told "taken", not 500."""
    c = _client(app, "alice")
    _request(c, "contested@example.com")
    token, _ = _token(mails)
    real_commit = _db.session.commit
    raced = {"done": False}

    def racing_commit():
        if raced["done"]:
            return real_commit()
        raced["done"] = True
        _db.session.rollback()
        raise IntegrityError('UPDATE "user"', {}, Exception(
            "UNIQUE constraint failed: user.email"))
    monkeypatch.setattr(_db.session, "commit", racing_commit)

    r = _confirm(c, token)

    monkeypatch.setattr(_db.session, "commit", real_commit)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["reason"] == "taken"
    _db.session.expire_all()
    assert _user("alice").email == "alice@example.com"
    assert _user("alice").pending_email is None
    assert not [m for m in mails if m["subject"] == CHANGED_SUBJECT]


def test_send_rejects_same_invalid_and_overlong_addresses(app, mails):
    c = _client(app, "alice")
    assert _request(c, "alice@example.com").status_code == 400
    assert _request(c, "not-an-email").status_code == 400
    assert c.post("/api/dashboard/email", json={"email": 7}).status_code == 400
    # Passes the pattern but not the column (String(128)): refused before
    # any mail goes out, instead of a delivered link that can never work.
    too_long = "a" * 120 + "@example.com"
    assert len(too_long) > 128
    r = _request(c, too_long)
    assert r.status_code == 400
    assert not mails
    assert _user("alice").pending_email is None
    # 128 exactly is fine.
    assert _request(c, "a" * 116 + "@example.com").status_code == 200


def test_a_failed_send_changes_nothing(app, mails, monkeypatch):
    c = _client(app, "alice")
    _request(c, "first@example.com")
    first_token, _ = _token(mails)
    import backend.utils.email as email_mod

    def relay_down(*a, **k):
        raise OSError("relay down")
    monkeypatch.setattr(email_mod, "_deliver", relay_down)

    r = _request(c, "second@example.com")

    assert r.status_code == 502
    _db.session.expire_all()
    assert _user("alice").pending_email == "first@example.com"
    # The earlier link was delivered and is still the pending one.
    assert _confirm(c, first_token).status_code == 200
    # With nothing pending before, nothing is pending after.
    assert _request(_client(app, "bob"), "b2@example.com").status_code == 502
    assert _user("bob").pending_email is None
    assert _user("bob").email_change_token_hash is None


def test_a_failed_send_does_not_undo_a_newer_request(app, mails, monkeypatch):
    """Two tabs: while this request's send hangs and fails, another request
    commits and mails its link. Putting back "what was pending before"
    would void that delivered link."""
    from sqlalchemy import text
    c = _client(app, "alice")
    import backend.utils.email as email_mod

    def newer_request_lands_then_relay_fails(*a, **k):
        _db.session.execute(
            text('UPDATE "user" SET pending_email = :p, '
                 'email_change_token_hash = :h WHERE username = :u'),
            {"p": "other-tab@example.com", "h": "other-tabs-hash", "u": "alice"})
        _db.session.commit()
        raise OSError("relay down")
    monkeypatch.setattr(email_mod, "_deliver", newer_request_lands_then_relay_fails)

    assert _request(c, "this-tab@example.com").status_code == 502

    _db.session.expire_all()
    alice = _user("alice")
    assert alice.pending_email == "other-tab@example.com"
    assert alice.email_change_token_hash == "other-tabs-hash"


def test_direct_bind_through_put_user_is_refused(app):
    c = _client(app, "alice")
    r = c.put("/api/dashboard/user", json={"email": "evil@example.com"})
    assert r.status_code == 400
    assert _user("alice").email == "alice@example.com"


def test_a_newer_request_invalidates_the_older_link(app, mails):
    c = _client(app, "alice")
    _request(c, "first@example.com")
    first, _ = _token(mails)
    _request(c, "second@example.com")
    r = _confirm(c, first)
    assert r.status_code == 400
    assert r.get_json()["reason"] == "invalid_or_expired"
    assert _user("alice").email == "alice@example.com"
    assert _user("alice").pending_email == "second@example.com"


@pytest.mark.parametrize("name", ["alice", "xonly"])
def test_cancel_never_touches_the_bound_email(app, mails, name):
    """The change was confirmed on another device; this tab still shows
    "pending" and the user clicks Cancel. The address they just confirmed
    must survive — for an X account the old single DELETE removed it."""
    c = _client(app, name)
    _request(c, "confirmed@example.com")
    token, _ = _token(mails)
    assert _confirm(c, token).status_code == 200

    r = c.delete("/api/dashboard/email/pending")

    assert r.status_code == 200
    assert r.get_json()["email"] == "confirmed@example.com"
    _db.session.expire_all()
    assert _user(name).email == "confirmed@example.com"


def test_cancel_drops_the_pending_change_and_its_link(app, mails):
    c = _client(app, "alice")
    _request(c, "p@example.com")
    token, _ = _token(mails)
    r = c.delete("/api/dashboard/email/pending")
    assert r.get_json()["pending_email"] is None
    assert _user("alice").pending_email is None
    assert _confirm(c, token).status_code == 400
    assert _user("alice").email == "alice@example.com"


def test_remove_email_needs_another_way_in(app, mails):
    c = _client(app, "alice")
    r = c.delete("/api/dashboard/email")
    assert r.status_code == 400
    assert _user("alice").email == "alice@example.com"
    # An X-login account may drop its email; a pending change goes with it.
    xc = _client(app, "xonly")
    _user("xonly").email = "x@example.com"
    _db.session.commit()
    _request(xc, "x2@example.com")
    r = xc.delete("/api/dashboard/email")
    assert r.status_code == 200
    assert r.get_json() == {"message": "Email removed.", "email": None,
                            "pending_email": None,
                            "pending_email_expired": False}
    assert _user("xonly").email is None
    assert _user("xonly").pending_email is None


def test_sign_in_links_and_change_links_do_not_cancel_each_other(app, mails):
    """They shared one hash column: anyone could void a pending change with
    an unauthenticated /auth/magic-link/send for the account's address, and
    a change request voided an outstanding sign-in or welcome link."""
    c = _client(app, "alice")
    _request(c, "new@example.com")
    change_token, _ = _token(mails)

    r = _anonymous(app).post("/auth/magic-link/send",
                             json={"email": "alice@example.com"})
    assert r.status_code == 200
    sign_in_token = re.search(r"verify\?token=(\S+)", mails[-1]["text"]).group(1)

    # A second change request and a cancel leave the sign-in link alone...
    _request(c, "other@example.com")
    c.delete("/api/dashboard/email/pending")
    fresh = _anonymous(app)
    r = fresh.get(f"/auth/magic-link/verify?token={sign_in_token}")
    assert r.headers["Location"] == f"{FRONTEND_URL}/dashboard"
    assert _session_user_id(fresh) == str(_user("alice").id)

    # ...and a sign-in link sent meanwhile leaves the change link alone.
    _request(c, "new@example.com")
    change_token, _ = _token(mails)
    _anonymous(app).post("/auth/magic-link/send",
                         json={"email": "alice@example.com"})
    assert _confirm(c, change_token).status_code == 200
    _db.session.expire_all()
    assert _user("alice").email == "new@example.com"


def test_admin_set_email_clears_a_pending_change(app, mails):
    """A waitlisted user mistyped their address; the admin sets the right
    one. The still-valid link for the mistyped one must not replace it."""
    c = _client(app, "alice")
    _request(c, "typo@example.com")
    token, _ = _token(mails)

    admin = _client(app, "root")
    r = admin.put(f"/api/admin/users/{_user('alice').id}/update_email",
                  json={"email": " Right@Example.com "})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["email"] == "right@example.com"

    _db.session.expire_all()
    assert _user("alice").pending_email is None
    assert _user("alice").email_change_token_hash is None
    assert _confirm(_client(app, "alice"), token).status_code == 400
    _db.session.expire_all()
    assert _user("alice").email == "right@example.com"
    # An address another account holds is a readable 409.
    r = _client(app, "root").put(
        f"/api/admin/users/{_user('alice').id}/update_email",
        json={"email": "bob@example.com"})
    assert r.status_code == 409


def test_an_expired_link_is_reported_so_the_ui_can_offer_a_new_one(app, mails):
    c = _client(app, "alice")
    _request(c, "new@example.com")
    token, _ = _token(mails)
    assert c.get("/api/dashboard/").get_json()["user"]["pending_email_expired"] is False

    _user("alice").email_change_expires_at = datetime.utcnow() - timedelta(seconds=1)
    _db.session.commit()

    state = c.get("/api/dashboard/").get_json()["user"]
    assert state["pending_email"] == "new@example.com"
    assert state["pending_email_expired"] is True
    r = _confirm(c, token)
    assert r.status_code == 400
    assert r.get_json()["reason"] == "invalid_or_expired"
    assert _user("alice").email == "alice@example.com"
    # Asking again for the same address sends a fresh link that works.
    assert _request(c, "new@example.com").status_code == 200
    token, _ = _token(mails)
    assert _confirm(c, token).status_code == 200


def test_the_token_itself_expires(app, mails):
    """The signature carries the issue time, so a link older than
    EMAIL_CHANGE_EXPIRY_SECONDS dies even if the row says otherwise."""
    from backend.utils.magic_link import (
        generate_email_change_token, hash_token, email_change_expiry_seconds)
    alice = _user("alice")
    issued = int(time.time()) - email_change_expiry_seconds() - 60
    with patch("itsdangerous.timed.TimestampSigner.get_timestamp",
               return_value=issued):
        token = generate_email_change_token(alice.id, "new@example.com")
    alice.pending_email = "new@example.com"
    alice.email_change_token_hash = hash_token(token)
    alice.email_change_expires_at = datetime.utcnow() + timedelta(hours=1)
    _db.session.commit()

    r = _confirm(_client(app, "alice"), token)

    assert r.status_code == 400
    assert _user("alice").email == "alice@example.com"
    for junk in ("", "not-a-token", None, 7):
        r = _client(app, "alice").post("/api/dashboard/email/confirm",
                                       json={"token": junk})
        assert r.status_code == 400


class TestWaitlistedAccountOnTheRealApp:
    """create_app() carries the approval gate. A waitlisted signup leaves
    their address on /alpha-thank-you while still unapproved — and since
    every whitelisted account starts unapproved, so does the owner of a
    placeholder. Activate & Welcome refuses an account without an email, so
    a 403 here leaves no way to reach them."""

    def test_the_email_flow_is_open_before_approval(self, real_app, mails):
        c = _client(real_app, "waiting")

        r = _request(c, "me@example.com")
        assert r.status_code == 200, r.get_json()
        token, _ = _token(mails)
        r = _confirm(c, token)
        assert r.status_code == 200, r.get_json()
        _db.session.expire_all()
        waiting = _user("waiting")
        assert waiting.email == "me@example.com"
        assert waiting.approved is False

        _request(c, "other@example.com")
        assert c.delete("/api/dashboard/email/pending").status_code == 200
        assert c.delete("/api/dashboard/email").status_code == 200
        assert _user("waiting").email is None

    def test_everything_else_stays_gated(self, real_app, mails):
        c = _client(real_app, "waiting")
        r = c.get("/api/nodes/1")
        assert r.status_code == 403
        assert "not approved" in r.get_json()["error"]
        # The old way in, a direct bind, is closed for them too.
        r = c.put("/api/dashboard/user", json={"email": "me@example.com"})
        assert r.status_code == 400
        # Only the email routes opened, not every POST under the dashboard.
        assert c.post("/api/dashboard/emails", json={}).status_code == 403
        assert c.post("/api/dashboard/timezone", json={}).status_code == 403
        assert _user("waiting").email is None

    def test_the_link_alone_does_nothing_here_either(self, real_app, mails):
        _request(_client(real_app, "waiting"), "me@example.com")
        token, _ = _token(mails)
        anon = _anonymous(real_app)
        assert _confirm(anon, token).status_code == 401
        anon.get(f"/auth/magic-link/verify?token={token}")
        assert _session_user_id(anon) is None
        _db.session.expire_all()
        assert _user("waiting").email is None


class TestMails:
    def test_every_mail_goes_out_with_a_timeout(self, app):
        """smtplib's default is none: a stalled relay held the request."""
        import backend.utils.email as m
        app.config["MAIL_TIMEOUT_SECONDS"] = 7
        calls = []

        class FakeSMTP:
            def __init__(self, *a, **k):
                calls.append(k)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, *a):
                pass

            def sendmail(self, *a):
                pass

        with patch("smtplib.SMTP", FakeSMTP):
            m.send_magic_link_email("a@example.com", "https://x/verify")
            m.send_welcome_email("a@example.com", "https://x/verify")
            m.send_email_change_email("a@example.com", "https://x/c", 86400)
            m.send_email_in_use_notice("a@example.com")
            m.send_email_changed_notice("old@example.com", "a@example.com")
            m.send_spend_alert_email("a@example.com", "anthropic", 1.0, 2.0, 0.5)
            m.send_user_spend_block_email("a@example.com", "alice", 1.0, 2.0)
            m.send_admin_signup_notification("alice", "a@example.com")
            m.send_admin_prefill_complete_notification("alice", "alice", 1, 10, True)
        assert len(calls) == 9
        assert all(k.get("timeout") == 7 for k in calls)

    def test_the_confirmation_mail_states_the_configured_lifetime(self, app, mails):
        import backend.utils.email as m
        m.send_email_change_email("a@example.com", "https://x/c", 86400)
        m.send_email_change_email("a@example.com", "https://x/c", 900)
        assert "expires in 24 hours" in mails[0]["text"]
        assert "expires in 24 hours" in mails[0]["html"]
        assert "expires in 15 minutes" in mails[1]["text"]

    def test_a_typed_address_is_escaped_in_the_notice(self, app, mails):
        """The address pattern allows angle brackets, and the notice to the
        old address prints the new one."""
        import backend.utils.email as m
        m.send_email_changed_notice("old@example.com", "<b>x</b>@example.com")
        assert "<b>x</b>" not in mails[0]["html"]
        assert "&lt;b&gt;x&lt;/b&gt;@example.com" in mails[0]["html"]
