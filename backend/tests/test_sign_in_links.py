"""Sign-in emails: limits, and a link stays valid until it expires
(backend/utils/sign_in_links.py, backend/utils/send_limits.py); the same
limits on the email-change confirmation.

Most tests use a bare app (auth + dashboard + admin blueprints), as
test_email_change does; the client address behind the proxy and the account deletion use
create_app(). sqlite in memory; mail is captured below the senders.
"""
import os
import re
import sys
from datetime import datetime, timedelta
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
from backend.models import SignInLink, User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import send_limits, sign_in_links  # noqa: E402

FRONTEND_URL = "https://loore.test"
SIGN_IN_SUBJECT = "Your Loore sign-in link"
WELCOME_SUBJECT = "Welcome to Loore"
SENT = {
    "message": "If that email is associated with an account, "
               "you'll receive a sign-in link shortly. "
               "If you're new, a link has been sent to create your account."
}


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
        User(username="alice", email="alice@example.com", approved=True),
        User(username="bob", email="bob@example.com", approved=False),
        User(username="root", email="root@example.com", approved=True,
             is_admin=True),
    ])
    _db.session.commit()


def _bare_app():
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 900
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.auth import auth_bp
    from backend.routes.admin import admin_bp
    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    return app


@pytest.fixture
def app():
    saved = _swap_in_real_modules()
    app = _bare_app()
    with app.app_context():
        _db.create_all()
        _seed()
        yield app
        _db.session.remove()
        _db.drop_all()
    _restore_modules(saved)


@pytest.fixture
def real_app(monkeypatch):
    """create_app(): ProxyFix as deployed, and every blueprint. sqlite is
    forced on the Config class itself (see test_email_change)."""
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


@pytest.fixture
def clock(monkeypatch):
    """Moves the links' clock (sign_in_links._now) and the counters' clock
    (time.monotonic in send_limits) forward together. The signatures keep
    real time: the tests stay within a token's signed lifetime."""
    import time as _time
    state = {"offset": timedelta(0)}
    real_now, real_monotonic = sign_in_links._now, _time.monotonic
    monkeypatch.setattr(sign_in_links, "_now",
                        lambda: real_now() + state["offset"])
    monkeypatch.setattr(send_limits.time, "monotonic",
                        lambda: real_monotonic() + state["offset"].total_seconds())

    def advance(**kw):
        state["offset"] += timedelta(**kw)
    return advance


def _user(name):
    _db.session.expire_all()
    return User.query.filter_by(username=name).first()


def _anonymous(app, addr="203.0.113.1"):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = addr
    return c


def _signed_in(app, name, addr="203.0.113.1"):
    c = _anonymous(app, addr)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return c


def _admin(app):
    return _signed_in(app, "root")


def _session_user_id(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def _ask(app, email, addr="203.0.113.1", **body):
    return _anonymous(app, addr).post("/auth/magic-link/send",
                                      json={"email": email, **body})


def _token(mails, subject=SIGN_IN_SUBJECT):
    mail = [m for m in mails if m["subject"] == subject][-1]
    return re.search(r"verify\?token=(\S+)", mail["text"]).group(1)


def _open(app, token, addr="198.51.100.7"):
    c = _anonymous(app, addr)
    r = c.get(f"/auth/magic-link/verify?token={token}")
    assert r.status_code == 302
    return r.headers["Location"], _session_user_id(c)


def _earlier_format_token(email, next_url=None, max_age=None):
    """A token as the code before sign-in links had rows made it: no
    nonce in the payload."""
    from backend.utils.magic_link import _get_serializer
    payload = {"email": email}
    if next_url:
        payload["next_url"] = next_url
    if max_age:
        payload["max_age"] = max_age
    return _get_serializer().dumps(payload, salt="magic-link")


# ── A link stays valid until it expires ──────────────────────────────────

def test_a_new_request_leaves_the_links_already_sent_working(app, mails):
    _ask(app, "alice@example.com")
    first = _token(mails)
    _ask(app, "alice@example.com")
    second = _token(mails)
    assert first != second

    assert _open(app, first) == (f"{FRONTEND_URL}/dashboard", str(_user("alice").id))
    assert _open(app, second) == (f"{FRONTEND_URL}/dashboard", str(_user("alice").id))
    # Only hashes are kept: neither the token nor the address.
    from backend.utils.magic_link import hash_token
    rows = SignInLink.query.all()
    assert {r.token_hash for r in rows} == {hash_token(first), hash_token(second)}
    assert {r.address_hash for r in rows} == {
        send_limits.address_key("alice@example.com")}
    assert send_limits.address_key("alice@example.com") != hash_token(
        "alice@example.com")                    # keyed, not a plain sha256


def test_two_tokens_for_the_same_address_in_the_same_second_differ(app):
    from backend.utils.magic_link import generate_magic_link_token
    tokens = {generate_magic_link_token("a@example.com", "/x") for _ in range(5)}
    assert len(tokens) == 5


def test_the_welcome_link_survives_later_sign_in_requests(app, mails):
    r = _admin(app).post(f"/api/admin/users/{_user('bob').id}/activate_and_welcome")
    assert r.get_json()["email_sent"] is True
    welcome = _token(mails, WELCOME_SUBJECT)
    for _ in range(3):
        assert _ask(app, "bob@example.com").status_code == 200

    assert _open(app, welcome) == (f"{FRONTEND_URL}/welcome", str(_user("bob").id))


def test_a_link_works_every_time_it_is_opened_until_it_expires(app, mails, clock):
    """Mail apps and scanners open links before the person does: opening
    one uses nothing up. The welcome link, opened on arrival by a scanner,
    still signs the person in days later. Control: as before the change."""
    r = _admin(app).post(f"/api/admin/users/{_user('bob').id}/activate_and_welcome")
    assert r.status_code == 200
    welcome = _token(mails, WELCOME_SUBJECT)
    bob = str(_user("bob").id)
    assert _open(app, welcome)[1] == bob        # the scanner, on arrival
    clock(days=3)
    assert _open(app, welcome) == (f"{FRONTEND_URL}/welcome", bob)   # the person
    assert _open(app, welcome)[1] == bob        # and again, on the phone
    clock(days=28)
    assert _open(app, welcome) == (
        f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


def test_a_sign_in_link_opened_twice_signs_in_both_times(app, mails):
    _ask(app, "newcomer@example.com")
    token = _token(mails)
    first = _open(app, token)
    second = _open(app, token)
    newcomer = str(User.query.filter_by(email="newcomer@example.com").one().id)
    assert first == second == (f"{FRONTEND_URL}/dashboard", newcomer)
    assert User.query.filter_by(email="newcomer@example.com").count() == 1


def test_a_link_past_its_expiry_does_not_sign_in(app, mails):
    token = sign_in_links.make_link_token("alice@example.com")
    row = SignInLink.query.one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    _db.session.commit()
    assert _open(app, token) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


def test_a_link_without_its_row_does_not_sign_in(app, mails):
    """The rows of an address are withdrawn when its account is deleted."""
    _ask(app, "alice@example.com")
    token = _token(mails)
    sign_in_links.withdraw_links("Alice@Example.com ")
    _db.session.commit()
    assert SignInLink.query.count() == 0
    assert _open(app, token) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


class TestLinksSentBeforeTheChange:
    """Tokens in inboxes at deploy time carry no nonce and have no row.
    They work as they did: controls (they pass before the change too),
    except that a later request no longer replaces them."""

    def _earlier_link(self, name, email, **kw):
        from backend.utils.magic_link import hash_token
        user = _user(name)
        token = _earlier_format_token(email, **kw)
        user.magic_link_token_hash = hash_token(token)
        user.magic_link_expires_at = datetime.utcnow() + timedelta(minutes=10)
        _db.session.commit()
        return token, str(user.id)

    def test_an_accounts_link_works_until_it_expires(self, app):
        token, alice = self._earlier_link("alice", "alice@example.com")
        assert _open(app, token) == (f"{FRONTEND_URL}/dashboard", alice)
        assert _open(app, token) == (f"{FRONTEND_URL}/dashboard", alice)

    def test_a_later_request_leaves_it_working(self, app, mails):
        """Not a control: before the change, the new request replaced it."""
        token, alice = self._earlier_link("alice", "alice@example.com")
        _ask(app, "alice@example.com")
        assert _open(app, token) == (f"{FRONTEND_URL}/dashboard", alice)

    def test_the_welcome_link_of_before(self, app):
        token, bob = self._earlier_link("bob", "bob@example.com",
                                        next_url="/welcome",
                                        max_age=30 * 24 * 3600)
        assert _open(app, token) == (f"{FRONTEND_URL}/welcome", bob)
        assert _open(app, token) == (f"{FRONTEND_URL}/welcome", bob)

    def test_a_sign_up_link(self, app):
        """As before: it makes the account, and then the account holds no
        hash for it. (A sign-up link sent now has a row and works again.)"""
        token = _earlier_format_token("fresh@example.com")
        location, who = _open(app, token)
        assert location == f"{FRONTEND_URL}/dashboard"
        assert who == str(User.query.filter_by(email="fresh@example.com").one().id)
        assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)
        assert User.query.filter_by(email="fresh@example.com").count() == 1

    def test_a_link_replaced_before_the_change_stays_refused(self, app):
        alice = _user("alice")
        alice.magic_link_token_hash = "the hash of a later link"
        _db.session.commit()
        token = _earlier_format_token("alice@example.com")
        assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)


# ── A link signs in only to the account it was sent for ─────────────────

CONFIRM_SUBJECT = "Confirm your email for Loore"
IN_USE_SUBJECT = "This address already signs in to Loore"


def _change(app, name, address, addr="203.0.113.1"):
    return _signed_in(app, name, addr).post("/api/dashboard/email",
                                            json={"email": address})


def _change_token(mails):
    mail = [m for m in mails if m["subject"] == CONFIRM_SUBJECT][-1]
    return re.search(r"/confirm-email\?token=(\S+)", mail["text"]).group(1)


def _confirm(app, name, token):
    return _signed_in(app, name).post("/api/dashboard/email/confirm",
                                      json={"token": token})


def test_a_link_sent_before_an_account_took_the_address_does_not_sign_in_to_it(app, mails):
    """A link sent to an address without an account; Bob then changes his
    address to it and confirms. The link does not open Bob's account."""
    _ask(app, "free@example.com")
    link = _token(mails)
    assert _change(app, "bob", "free@example.com").status_code == 200
    assert _confirm(app, "bob", _change_token(mails)).status_code == 200
    assert _open(app, link) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


def test_a_link_sent_for_no_account_never_opens_an_account_that_has_the_address_later(app, mails):
    """However the address got to the account (here written directly, as
    an admin's edit would), a link sent while it had no account cannot
    sign in to it: it could only have made the account."""
    _ask(app, "free@example.com")
    link = _token(mails)
    bob = _user("bob")
    bob.email = "free@example.com"
    _db.session.commit()
    assert _open(app, link) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


def test_the_open_that_makes_the_account_binds_the_addresss_links_to_it(app, mails):
    """Two links asked for a new address: the first open makes the
    account, and both links then sign in to it (they went to the same
    inbox), on any device, however often."""
    _ask(app, "new@example.com")
    first = _token(mails)
    _ask(app, "new@example.com")
    second = _token(mails)
    _, made = _open(app, first)
    new = User.query.filter_by(email="new@example.com").one()
    assert made == str(new.id)
    assert _open(app, second) == (f"{FRONTEND_URL}/dashboard", str(new.id))
    assert _open(app, first) == (f"{FRONTEND_URL}/dashboard", str(new.id))
    assert {r.user_id for r in SignInLink.query.all()} == {new.id}


def test_a_link_for_an_account_does_not_follow_the_address_to_another(app, mails):
    """A link sent for Alice, then Alice moves to a new address and Bob
    takes her old one: neither the old link nor the address opens either
    account."""
    _ask(app, "alice@example.com")
    link = _token(mails)
    assert _change(app, "alice", "alice2@example.com").status_code == 200
    assert _confirm(app, "alice", _change_token(mails)).status_code == 200
    assert _open(app, link) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)
    assert _change(app, "bob", "alice@example.com").status_code == 200
    assert _confirm(app, "bob", _change_token(mails)).status_code == 200
    assert _open(app, link) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


def test_confirming_an_email_change_withdraws_both_addresses_links(app, mails):
    _ask(app, "alice@example.com")
    _ask(app, "alice2@example.com")
    assert SignInLink.query.count() == 2
    assert _change(app, "alice", "alice2@example.com").status_code == 200
    assert _confirm(app, "alice", _change_token(mails)).status_code == 200
    assert SignInLink.query.count() == 0


def test_make_link_token_issues_a_link_that_works(app):
    """The supported way to issue a link outside the routes (tests, dev
    scripts): a token minted by hand has no row and does not sign in."""
    from backend.utils.magic_link import generate_magic_link_token
    token = sign_in_links.make_link_token("alice@example.com", "/welcome")
    assert _open(app, token) == (f"{FRONTEND_URL}/welcome", str(_user("alice").id))
    fresh = sign_in_links.make_link_token("fresh@example.com")
    assert _open(app, fresh)[1] == str(
        User.query.filter_by(email="fresh@example.com").one().id)
    by_hand = generate_magic_link_token("alice@example.com")
    assert _open(app, by_hand) == (f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


# ── Limits ───────────────────────────────────────────────────────────────

def _small_client_limits(app):
    app.config["SEND_LIMIT_CLIENT_PER_15_MIN"] = 10
    app.config["SEND_LIMIT_CLIENT_PER_DAY"] = 30


def _relay_down(monkeypatch):
    import backend.utils.email as email_mod

    def fail(*a):
        raise OSError("relay down")
    real = email_mod._deliver
    monkeypatch.setattr(email_mod, "_deliver", fail)
    return lambda: monkeypatch.setattr(email_mod, "_deliver", real)


def test_an_address_gets_at_most_five_an_hour_and_ten_a_day(app, mails, clock):
    """Asked from different clients, so only the address limit counts. A
    request over it gets the same answer, and nothing is sent."""
    def ask(i):
        r = _ask(app, "alice@example.com", addr=f"192.0.2.{i}")
        assert r.status_code == 200
        assert r.get_json() == SENT
    for i in range(5):
        ask(i)
    assert len(mails) == 5
    ask(5)
    assert len(mails) == 5
    assert SignInLink.query.count() == 5        # no link for the refused one

    clock(minutes=61)
    for i in range(10, 15):
        ask(i)
    assert len(mails) == 10
    ask(15)
    clock(minutes=61)
    ask(16)
    assert len(mails) == 10                     # ten in the day

    clock(hours=23)
    ask(17)
    assert len(mails) == 11
    assert all(m["to"] == "alice@example.com" for m in mails)


def test_a_failed_send_neither_works_nor_counts(app, mails, monkeypatch):
    """The relay is down: five requests, five failures. Once it is back,
    the next request sends; the links of the failed sends do not exist."""
    back = _relay_down(monkeypatch)
    for i in range(5):
        assert _ask(app, "alice@example.com", addr=f"192.0.2.{i}").get_json() == SENT
    assert SignInLink.query.count() == 0
    back()
    assert _ask(app, "alice@example.com", addr="192.0.2.9").get_json() == SENT
    assert len(mails) == 1
    assert _open(app, _token(mails))[1] == str(_user("alice").id)


def test_parallel_requests_never_exceed_the_address_limit(app):
    """The count and its check are one step: twenty requests at once for
    one address get five claims."""
    import threading
    got = []
    start = threading.Barrier(20)

    def ask():
        with app.app_context():
            start.wait()
            got.append(send_limits.claim_address("sign-in", "a@example.com"))
    threads = [threading.Thread(target=ask) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for c in got if c is not None) == 5


def test_a_client_gets_the_configured_requests_in_fifteen_minutes(app, mails, clock):
    _small_client_limits(app)
    for i in range(10):
        assert _ask(app, f"person{i}@example.com", addr="203.0.113.9").status_code == 200
    assert len(mails) == 10

    r = _ask(app, "person10@example.com", addr="203.0.113.9")
    assert r.status_code == 429
    assert r.get_json() == {"error": send_limits.WAIT_MESSAGE}
    assert len(mails) == 10
    # Another client is not affected.
    assert _ask(app, "person11@example.com", addr="203.0.113.10").status_code == 200
    assert len(mails) == 11
    # A few minutes later this one can ask again.
    clock(minutes=16)
    assert _ask(app, "person12@example.com", addr="203.0.113.9").status_code == 200


def test_a_client_gets_the_configured_requests_a_day(app, mails, clock):
    _small_client_limits(app)
    n = 0
    for _ in range(3):
        for _ in range(10):
            assert _ask(app, f"p{n}@example.com", addr="203.0.113.9").status_code == 200
            n += 1
        clock(minutes=16)
    assert _ask(app, f"p{n}@example.com", addr="203.0.113.9").status_code == 429
    clock(hours=24)
    assert _ask(app, f"p{n}@example.com", addr="203.0.113.9").status_code == 200
    assert len(mails) == 31


def test_unconfigured_a_shared_network_gets_sixty_requests_in_fifteen_minutes(app, mails):
    """The default per-client bound, sized for a workshop signing up on one
    network."""
    for i in range(60):
        assert _ask(app, f"w{i}@example.com", addr="203.0.113.60").status_code == 200
    assert _ask(app, "w60@example.com", addr="203.0.113.60").status_code == 429
    assert send_limits.client_limits() == ((15 * 60, 60), (24 * 3600, 200))


def test_an_ipv6_client_is_counted_by_its_64(app, mails):
    _small_client_limits(app)
    for i in range(10):
        assert _ask(app, f"v6-{i}@example.com", addr=f"2001:db8::{i + 1:x}").status_code == 200
    assert _ask(app, "v6-x@example.com", addr="2001:db8::ffff").status_code == 429
    assert _ask(app, "v6-y@example.com", addr="2001:db8:0:1::1").status_code == 200


def test_client_buckets():
    b = send_limits.client_bucket
    assert b("203.0.113.9") == "203.0.113.9"
    assert b("2001:db8::1") == b("2001:db8::abcd") == "2001:db8::/64"
    assert b("2001:db8:0:1::1") != b("2001:db8::1")
    assert b("::ffff:203.0.113.9") == "203.0.113.9"
    assert b(None) == "unknown"
    assert b("not an address") == "not an address"


def test_the_same_answer_in_every_case(app, mails, monkeypatch):
    """An address with an account, without one, over its limit, a send
    that fails: one answer. The client's own limit says nothing about any
    address."""
    _small_client_limits(app)
    answers = [_ask(app, "alice@example.com", addr="192.0.2.1"),
               _ask(app, "nobody@example.com", addr="192.0.2.2")]
    for i in range(5):
        _ask(app, "bob@example.com", addr=f"192.0.2.{10 + i}")
    sent = len(mails)
    answers.append(_ask(app, "bob@example.com", addr="192.0.2.20"))
    assert len(mails) == sent                   # over the address's limit
    _relay_down(monkeypatch)
    answers.append(_ask(app, "alice@example.com", addr="192.0.2.30"))

    assert [(r.status_code, r.get_json()) for r in answers] == [(200, SENT)] * 4

    for i in range(10):
        _ask(app, f"x{i}@example.com", addr="192.0.2.40")
    refused = _ask(app, "alice@example.com", addr="192.0.2.40")
    assert refused.status_code == 429
    assert "alice" not in refused.get_data(as_text=True)


def test_an_invalid_address_is_refused_before_anything_is_counted(app, mails):
    """Control (passes before the change too): a malformed address is a
    400, and those do not use up the client's requests."""
    _small_client_limits(app)
    for _ in range(12):
        r = _ask(app, "not-an-address", addr="203.0.113.50")
        assert r.status_code == 400
    assert _ask(app, "alice@example.com", addr="203.0.113.50").status_code == 200


# ── Counting in Redis ────────────────────────────────────────────────────

class _FakePipeline:
    def __init__(self, store):
        self.store, self.ops = store, []

    def set(self, name, value, ex=None, nx=False):
        self.ops.append(("set", name, value, ex, nx))

    def incr(self, name):
        self.ops.append(("incr", name))

    def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "set":
                _, name, value, ex, nx = op
                if nx and name in self.store:
                    out.append(None)
                else:
                    self.store[name] = [int(value), ex]
                    out.append(True)
            else:
                self.store[op[1]][0] += 1
                out.append(self.store[op[1]][0])
        return out


class _FakeRedis:
    """SET NX EX + INCR in a pipeline, and the give-back script."""

    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakePipeline(self.store)

    def eval(self, script, numkeys, name):
        assert "decr" in script
        if name in self.store and self.store[name][0] > 0:
            self.store[name][0] -= 1
        return 0


def test_the_counters_live_in_redis_with_a_ttl(app, monkeypatch):
    _small_client_limits(app)
    fake = _FakeRedis()
    monkeypatch.setattr(send_limits, "_redis", lambda: fake)
    for _ in range(10):
        assert send_limits.client_may_ask("sign-in", "203.0.113.77")
    assert not send_limits.client_may_ask("sign-in", "203.0.113.77")
    assert sorted(ttl for _, ttl in fake.store.values()) == [15 * 60, 24 * 3600]
    claims = [send_limits.claim_address("email-change", "alice@example.com")
              for _ in range(6)]
    assert [c is not None for c in claims] == [True] * 5 + [False]
    send_limits.release(claims[0])              # a send that failed
    assert send_limits.claim_address("email-change", "alice@example.com")
    names = list(fake.store)
    assert not any("203.0.113.77" in n or "alice" in n for n in names)
    assert all(n.startswith(send_limits.KEY_PREFIX) for n in names)


def test_without_redis_each_process_counts_on_its_own(app, monkeypatch):
    _small_client_limits(app)

    def unreachable():
        raise ConnectionError("no redis")
    monkeypatch.setattr(send_limits, "_redis", unreachable)
    for _ in range(10):
        assert send_limits.client_may_ask("sign-in", "203.0.113.78")
    assert not send_limits.client_may_ask("sign-in", "203.0.113.78")


# ── Behind the proxy, and account deletion (create_app) ─────────────────

def test_behind_the_proxy_the_client_is_the_address_the_proxy_saw(real_app, mails):
    """ProxyFix takes the last X-Forwarded-For entry, the one the proxy
    appended; whatever the client wrote before it does not count."""
    _small_client_limits(real_app)
    for i in range(10):
        r = real_app.test_client().post(
            "/auth/magic-link/send", json={"email": f"q{i}@example.com"},
            headers={"X-Forwarded-For": f"10.9.9.{i}, 203.0.113.200"})
        assert r.status_code == 200
    r = real_app.test_client().post(
        "/auth/magic-link/send", json={"email": "q10@example.com"},
        headers={"X-Forwarded-For": "10.9.9.99, 203.0.113.200"})
    assert r.status_code == 429
    r = real_app.test_client().post(
        "/auth/magic-link/send", json={"email": "q11@example.com"},
        headers={"X-Forwarded-For": "203.0.113.201"})
    assert r.status_code == 200


def test_deleting_the_account_withdraws_its_links(real_app, mails):
    """That the link stops working is a control (it did before, through
    the account's hash column); that its row goes is new."""
    from backend.utils.account_deletion import schedule_account_deletion
    _ask(real_app, "alice@example.com")
    token = _token(mails)
    alice = _user("alice")
    schedule_account_deletion(alice, requested_by_id=alice.id, source="self")
    assert SignInLink.query.count() == 0
    assert _open(real_app, token) == (
        f"{FRONTEND_URL}/login?error=invalid_or_expired", None)


# ── The same limits on the email-change confirmation ─────────────────────

def _accounts(n):
    """n more accounts, named u0, u1, …"""
    for i in range(n):
        _db.session.add(User(username=f"u{i}", email=f"u{i}@example.com",
                             approved=True))
    _db.session.commit()
    return [f"u{i}" for i in range(n)]


def _sent_answer(name, address):
    return {
        "message": f"Confirmation link sent to {address}. The address "
                   "becomes yours once you confirm it.",
        "email": _user(name).email,
        "pending_email": address,
        "pending_email_expired": False,
    }


def test_an_address_gets_at_most_five_change_links_an_hour(app, mails):
    """From different accounts and clients, so only the address limit
    counts. Over it nothing is sent, the answer is the one a sent link
    gets, and a link already out for the address keeps working."""
    for i, name in enumerate(_accounts(4)):
        assert _change(app, name, "target@example.com",
                       addr=f"192.0.2.{i}").status_code == 200
    r = _change(app, "alice", "target@example.com", addr="192.0.2.50")
    assert r.get_json() == _sent_answer("alice", "target@example.com")
    link = _change_token(mails)
    assert len(mails) == 5

    r = _change(app, "alice", "target@example.com", addr="192.0.2.51")
    assert r.status_code == 200
    assert r.get_json() == _sent_answer("alice", "target@example.com")
    assert len(mails) == 5
    r = _confirm(app, "alice", link)
    assert r.status_code == 200, r.get_json()
    assert _user("alice").email == "target@example.com"


def test_over_the_address_limit_a_new_pending_address_has_no_usable_link(app, mails):
    for i, name in enumerate(_accounts(5)):
        _change(app, name, "busy@example.com", addr=f"192.0.2.{i}")
    assert len(mails) == 5
    r = _change(app, "bob", "busy@example.com", addr="192.0.2.60")
    assert r.get_json() == _sent_answer("bob", "busy@example.com")
    assert len(mails) == 5
    bob = _user("bob")
    assert bob.pending_email == "busy@example.com"
    assert bob.email_change_token_hash is None


def test_the_same_answer_for_a_taken_address_over_its_limit(app, mails):
    """A taken address gets an "in use" notice instead of a link; over the
    limit it gets nothing, and the asker cannot tell either case."""
    for i, name in enumerate(_accounts(5)):
        _change(app, name, "alice@example.com", addr=f"192.0.2.{i}")
    assert [m["subject"] for m in mails] == [IN_USE_SUBJECT] * 5
    r = _change(app, "bob", "alice@example.com", addr="192.0.2.70")
    assert r.get_json() == _sent_answer("bob", "alice@example.com")
    assert len(mails) == 5


def test_an_account_asks_at_most_five_times_an_hour(app, mails):
    for i in range(5):
        assert _change(app, "alice", f"new{i}@example.com",
                       addr=f"192.0.2.{i}").status_code == 200
    r = _change(app, "alice", "new5@example.com", addr="192.0.2.9")
    assert r.status_code == 429
    assert r.get_json() == {"error": send_limits.WAIT_MESSAGE}
    assert len(mails) == 5
    assert _user("alice").pending_email == "new4@example.com"


def test_failed_change_sends_count_toward_nothing(app, mails, monkeypatch):
    """Five 502s while the relay is down; once it is back the next request
    sends (not a 429)."""
    back = _relay_down(monkeypatch)
    for i in range(5):
        r = _change(app, "alice", "new@example.com", addr=f"192.0.2.{i}")
        assert r.status_code == 502
    back()
    r = _change(app, "alice", "new@example.com", addr="192.0.2.9")
    assert r.status_code == 200, r.get_json()
    assert len(mails) == 1


def test_a_client_asks_at_most_the_configured_times(app, mails, clock):
    _small_client_limits(app)
    names = _accounts(11)
    for name in names[:10]:
        assert _change(app, name, f"{name}-new@example.com",
                       addr="203.0.113.99").status_code == 200
    r = _change(app, names[10], "late@example.com", addr="203.0.113.99")
    assert r.status_code == 429
    assert "late" not in r.get_data(as_text=True)
    assert len(mails) == 10
    clock(minutes=16)
    assert _change(app, names[10], "late@example.com",
                   addr="203.0.113.99").status_code == 200


def test_the_two_routes_count_separately(app, mails):
    """A person's sign-in requests do not use up their email change."""
    _small_client_limits(app)
    for i in range(10):
        _ask(app, f"s{i}@example.com", addr="203.0.113.120")
    assert _change(app, "alice", "mine@example.com",
                   addr="203.0.113.120").status_code == 200
