"""Sign-in emails: limits, and a link stays valid until it is used or
expires (backend/utils/sign_in_links.py).

Most tests use a bare app (auth + admin blueprints), as test_email_change
does; the client address behind the proxy and the account deletion use
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
from backend.utils import sign_in_links  # noqa: E402

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
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
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
    """Moves the links' clock (sign_in_links._now) and the per-client
    counters' clock (time.monotonic) forward together. The signatures
    keep real time: the tests stay within a token's signed lifetime."""
    import time as _time
    state = {"offset": timedelta(0)}
    real_now, real_monotonic = sign_in_links._now, _time.monotonic
    monkeypatch.setattr(sign_in_links, "_now",
                        lambda: real_now() + state["offset"])
    monkeypatch.setattr(sign_in_links.time, "monotonic",
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


def _admin(app):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user("root").id)
        sess["_fresh"] = True
    return c


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


# ── A link stays valid until it is used or expires ──────────────────────

def test_a_new_request_leaves_the_links_already_sent_working(app, mails):
    _ask(app, "alice@example.com")
    first = _token(mails)
    _ask(app, "alice@example.com")
    second = _token(mails)
    assert first != second

    assert _open(app, first) == (f"{FRONTEND_URL}/dashboard", str(_user("alice").id))
    assert _open(app, second) == (f"{FRONTEND_URL}/dashboard", str(_user("alice").id))
    # Only hashes are kept: neither the token nor the address.
    rows = SignInLink.query.all()
    assert len(rows) == 2
    for row in rows:
        assert first not in (row.token_hash, row.address_hash)
        assert "alice" not in row.address_hash


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


def test_a_link_is_used_up_after_the_reuse_window(app, mails, clock):
    """The first use is recorded; the link keeps working for
    REUSE_SECONDS (a mail app's preview, then the person), then not."""
    r = _admin(app).post(f"/api/admin/users/{_user('bob').id}/activate_and_welcome")
    assert r.status_code == 200
    welcome = _token(mails, WELCOME_SUBJECT)

    assert _open(app, welcome)[1] == str(_user("bob").id)   # the preview
    clock(seconds=sign_in_links.REUSE_SECONDS - 60)
    assert _open(app, welcome)[1] == str(_user("bob").id)   # the person
    clock(seconds=120)
    assert _open(app, welcome) == (
        f"{FRONTEND_URL}/login?error=link_already_used", None)


def test_a_sign_in_link_is_used_up_too(app, mails, clock):
    app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 3600
    _ask(app, "newcomer@example.com")
    token = _token(mails)
    _, who = _open(app, token)
    assert who == str(User.query.filter_by(email="newcomer@example.com").one().id)
    clock(seconds=sign_in_links.REUSE_SECONDS + 1)
    assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)
    assert User.query.filter_by(email="newcomer@example.com").count() == 1


def test_a_link_past_its_expiry_does_not_sign_in(app, mails):
    _ask(app, "alice@example.com")
    token = _token(mails)
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
    They sign in once, as they would have before, and are used up like
    any link after that."""

    def test_an_accounts_link_signs_in_once(self, app, clock):
        # Longer-lived than the reuse window, so "used up" can show.
        app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 3600
        alice = _user("alice")
        token = _earlier_format_token("alice@example.com")
        from backend.utils.magic_link import hash_token
        expiry = datetime.utcnow().replace(microsecond=0) + timedelta(minutes=50)
        alice.magic_link_token_hash = hash_token(token)
        alice.magic_link_expires_at = expiry
        _db.session.commit()

        assert _open(app, token) == (f"{FRONTEND_URL}/dashboard", str(alice.id))
        alice = _user("alice")
        assert alice.magic_link_token_hash is None
        row = SignInLink.query.one()
        assert row.used_at is not None
        assert row.expires_at == expiry            # the account's, kept
        clock(seconds=sign_in_links.REUSE_SECONDS + 1)
        assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)

    def test_the_welcome_link_of_before_signs_in_once(self, app, clock):
        bob = _user("bob")
        token = _earlier_format_token("bob@example.com", "/welcome",
                                      max_age=30 * 24 * 3600)
        from backend.utils.magic_link import hash_token
        bob.magic_link_token_hash = hash_token(token)
        bob.magic_link_expires_at = datetime.utcnow() + timedelta(days=30)
        _db.session.commit()

        assert _open(app, token) == (f"{FRONTEND_URL}/welcome", str(bob.id))
        clock(hours=1)
        assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)

    def test_a_sign_up_link_signs_in_once(self, app, clock):
        app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 3600
        token = _earlier_format_token("fresh@example.com")
        location, who = _open(app, token)
        assert location == f"{FRONTEND_URL}/dashboard"
        assert who == str(User.query.filter_by(email="fresh@example.com").one().id)
        clock(seconds=sign_in_links.REUSE_SECONDS + 1)
        assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)

    def test_a_link_replaced_before_the_change_stays_refused(self, app):
        """Control: as before, only the newest link sent to an account
        then works."""
        alice = _user("alice")
        alice.magic_link_token_hash = "the hash of a later link"
        _db.session.commit()
        token = _earlier_format_token("alice@example.com")
        assert _open(app, token) == (f"{FRONTEND_URL}/login?error=link_already_used", None)


# ── Limits ───────────────────────────────────────────────────────────────

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


def test_a_client_gets_ten_requests_in_fifteen_minutes(app, mails, clock):
    for i in range(10):
        assert _ask(app, f"person{i}@example.com", addr="203.0.113.9").status_code == 200
    assert len(mails) == 10

    r = _ask(app, "person10@example.com", addr="203.0.113.9")
    assert r.status_code == 429
    assert r.get_json() == {"error": sign_in_links.WAIT_MESSAGE}
    assert len(mails) == 10
    # Another client is not affected.
    assert _ask(app, "person11@example.com", addr="203.0.113.10").status_code == 200
    assert len(mails) == 11
    # A few minutes later this one can ask again.
    clock(minutes=16)
    assert _ask(app, "person12@example.com", addr="203.0.113.9").status_code == 200


def test_a_client_gets_thirty_requests_a_day(app, mails, clock):
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


def test_an_ipv6_client_is_counted_by_its_64(app, mails):
    for i in range(10):
        assert _ask(app, f"v6-{i}@example.com", addr=f"2001:db8::{i + 1:x}").status_code == 200
    assert _ask(app, "v6-x@example.com", addr="2001:db8::ffff").status_code == 429
    assert _ask(app, "v6-y@example.com", addr="2001:db8:0:1::1").status_code == 200


def test_client_buckets():
    b = sign_in_links.client_bucket
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
    answers = [_ask(app, "alice@example.com", addr="192.0.2.1"),
               _ask(app, "nobody@example.com", addr="192.0.2.2")]
    for i in range(5):
        _ask(app, "bob@example.com", addr=f"192.0.2.{10 + i}")
    sent = len(mails)
    answers.append(_ask(app, "bob@example.com", addr="192.0.2.20"))
    assert len(mails) == sent                   # over the address's limit

    import backend.utils.email as email_mod

    def fail(*a):
        raise OSError("relay down")
    monkeypatch.setattr(email_mod, "_deliver", fail)
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
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakePipeline(self.store)


def test_the_client_counters_live_in_redis_with_a_ttl(app, monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sign_in_links, "_redis", lambda: fake)
    for _ in range(10):
        assert sign_in_links.client_may_ask("203.0.113.77")
    assert not sign_in_links.client_may_ask("203.0.113.77")
    assert sorted(ttl for _, ttl in fake.store.values()) == [15 * 60, 24 * 3600]
    assert not any("203.0.113.77" in name for name in fake.store)
    assert all(name.startswith(sign_in_links.CLIENT_KEY_PREFIX) for name in fake.store)


def test_without_redis_each_process_counts_on_its_own(app, monkeypatch):
    def unreachable():
        raise ConnectionError("no redis")
    monkeypatch.setattr(sign_in_links, "_redis", unreachable)
    for _ in range(10):
        assert sign_in_links.client_may_ask("203.0.113.78")
    assert not sign_in_links.client_may_ask("203.0.113.78")


# ── Behind the proxy, and account deletion (create_app) ─────────────────

def test_behind_the_proxy_the_client_is_the_address_the_proxy_saw(real_app, mails):
    """ProxyFix takes the last X-Forwarded-For entry, the one the proxy
    appended; whatever the client wrote before it does not count."""
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
