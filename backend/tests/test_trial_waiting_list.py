"""Payments on: from a day's number of new accounts that were not prepared
in advance, the next one waits for the admin's approval.

Peter, 2026-11-23: "A higher one -> hard stop, put them on a waiting list
as it was in Alpha." From OUTSIDE_TRIALS_HOLD_PER_DAY (20) places taken in
24 hours, a new account that was not prepared in advance is not let in at
its first request: it gets the waiting answer every account gets while
payments are off, the admin gets the email that asks for approval, and
Activate in the Users tab lets it in to the trial step. An account
prepared in advance, and one already approved, are never held. With
payments off nothing here does anything.

The approval gate is a before_request hook of create_app(), so this file
builds the real app (sqlite in memory, the celery glue stubbed, as
test_let_in_without_approval.py does). Stripe is never called.
"""
import os
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from sqlalchemy.orm.attributes import set_committed_value  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

WAITING = "Your account is not approved. Please wait for approval."
HOLD = 20


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


def _stubbed_backend_modules():
    return {k: v for k, v in sys.modules.items()
            if k.startswith("backend.") and isinstance(v, MagicMock)}


@pytest.fixture
def real_app(monkeypatch):
    stubs = _stubbed_backend_modules()
    for k in stubs:
        del sys.modules[k]
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["PAYMENTS_ENABLED"] = True
    app.config["DEPLOY_ENV"] = "testing"
    app.config["OUTSIDE_TRIALS_HOLD_PER_DAY"] = HOLD
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
    sys.modules.update(stubs)


@pytest.fixture
def payments_off(real_app):
    real_app.config["PAYMENTS_ENABLED"] = False
    return real_app


def _add(**kw):
    """An account as sign-up makes it: unapproved."""
    kw.setdefault("approved", False)
    u = User(**kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _trials(count, hours_ago=2, prefix="t", **kw):
    """Accounts the gate let in that started a trial *hours_ago*."""
    started = datetime.utcnow() - timedelta(hours=hours_ago)
    kw.setdefault("let_in_at", started)
    kw.setdefault("plan", "free")
    for i in range(count):
        _db.session.add(User(
            username=f"{prefix}{i}", approved=True,
            approved_at=started, subscription_level="standard",
            subscription_status="trialing",
            subscription_trial_start=started, **kw))
    _db.session.commit()


def _client(app, user):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _me(client):
    from flask import g
    g.pop("_login_user", None)
    r = client.get("/api/dashboard/?profile=0")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["user"]


def _fresh(user):
    _db.session.expire_all()
    return _db.session.get(User, user.id)


def _admin(app):
    admin = User(username="root", approved=True, is_admin=True)
    _db.session.add(admin)
    _db.session.commit()
    return admin, _client(app, admin)


def _as_admin(admin):
    from flask import g
    g.pop("_login_user", None)
    return admin


# ── The 21st of the day waits; the admin approves it ────────────────────

class TestTheNextAccountWaits:
    def test_below_the_number_a_new_account_goes_to_the_trial_step(
            self, real_app):
        _trials(HOLD - 1)
        u = _add(username="twentieth", email="a@example.com")
        me = _me(_client(real_app, u))
        assert me["approved"] is True
        assert me["subscription"]["state"] == "none"   # the subscribe page
        user = _fresh(u)
        assert user.let_in_at is not None
        assert user.approval_hold_at is None

    def test_at_the_number_a_new_account_gets_the_waiting_answer(
            self, real_app):
        _trials(HOLD)
        u = _add(username="twentyfirst", email="b@example.com")
        c = _client(real_app, u)
        me = _me(c)
        # What both apps key their waiting page on: not approved, and not
        # the "not active" state of an account the admin blocked.
        assert me["approved"] is False
        assert me["account_blocked"] is False
        r = c.post("/api/nodes/", json={"content": "hello"})
        assert r.status_code == 403
        assert r.get_json() == {"error": WAITING}
        # No way to the checkout either: the same answer, not a 402.
        r = c.post("/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 403
        assert r.get_json() == {"error": WAITING}
        user = _fresh(u)
        assert user.approved is False
        assert user.approval_hold_at is not None
        assert user.let_in_at is None and user.deactivated_at is None

    def test_the_admin_approves_it_and_it_goes_to_the_trial_step(
            self, real_app):
        _trials(HOLD)
        _admin_user, admin = _admin(real_app)
        u = _add(username="twentyfirst", email="b@example.com")
        c = _client(real_app, u)
        assert _me(c)["approved"] is False

        rows = _as_admin(admin).get("/api/admin/users").get_json()["users"]
        row = next(r for r in rows if r["username"] == "twentyfirst")
        assert row["approved"] is False
        assert row["blocked"] is False
        assert row["waits_for_approval"] is True
        other = next(r for r in rows if r["username"] == "t0")
        assert other["waits_for_approval"] is False

        r = _as_admin(admin).post(f"/api/admin/users/{u.id}/toggle")
        assert r.status_code == 200 and r.get_json()["approved"] is True
        user = _fresh(u)
        assert user.approved is True
        assert user.approval_hold_at is None and user.let_in_at is None

        me = _me(c)
        assert me["approved"] is True
        assert me["subscription"]["state"] == "none"   # the subscribe page
        rows = _as_admin(admin).get("/api/admin/users").get_json()["users"]
        row = next(r for r in rows if r["username"] == "twentyfirst")
        assert row["approved"] is True
        assert row["waits_for_approval"] is False

    def test_accounts_let_in_without_a_trial_yet_take_places_too(
            self, real_app):
        now = datetime.utcnow()
        _trials(HOLD - 5)
        for i in range(5):
            _add(username=f"in{i}", plan="free", approved=True,
                 let_in_at=now - timedelta(minutes=5))
        u = _add(username="next")
        assert _me(_client(real_app, u))["approved"] is False
        assert _fresh(u).approval_hold_at is not None

    def test_the_number_comes_from_the_settings(self, real_app):
        real_app.config["OUTSIDE_TRIALS_HOLD_PER_DAY"] = 2
        _trials(2)
        u = _add(username="third")
        assert _me(_client(real_app, u))["approved"] is False

    def test_the_number_zero_holds_nobody(self, real_app):
        real_app.config["OUTSIDE_TRIALS_HOLD_PER_DAY"] = 0
        _trials(HOLD + 5)
        u = _add(username="anyone")
        assert _me(_client(real_app, u))["approved"] is True
        assert _fresh(u).approval_hold_at is None

    def test_the_admin_is_asked_for_approval_by_email(self, real_app,
                                                      monkeypatch):
        import backend.routes.terms as terms
        asked, told = [], []
        monkeypatch.setattr(terms, "send_admin_signup_notification",
                            lambda name, email: asked.append(name))
        monkeypatch.setattr(terms, "send_admin_signup_info",
                            lambda user: told.append(user.username))
        _admin(real_app)
        _trials(HOLD - 1)
        first = _add(username="twentieth")
        c = _client(real_app, first)
        _me(c)
        assert c.post("/api/terms/accept").status_code == 200
        second = _add(username="twentyfirst")
        c = _client(real_app, second)
        _me(c)
        assert c.post("/api/terms/accept").status_code == 200
        # Let in: the information email. Waiting: the approval request.
        assert told == ["twentieth"]
        assert asked == ["twentyfirst"]


# ── Who is never held ───────────────────────────────────────────────────

class TestNeverHeld:
    def test_an_account_prepared_in_advance_is_never_held(self, real_app):
        _trials(HOLD + 5)
        now = datetime.utcnow()
        both = _add(username="prepared", prefilled_handle="prepared",
                    prefilled_at=now - timedelta(days=3))
        early = _add(username="early", prefilled_handle="early")
        again = _add(username="again", prefilled_at=now - timedelta(days=1))
        for u in (both, early, again):
            me = _me(_client(real_app, u))
            assert me["approved"] is True, u.username
            assert _fresh(u).approval_hold_at is None

    def test_trials_of_accounts_prepared_in_advance_take_no_place(
            self, real_app):
        _trials(HOLD + 5, prefix="ca", prefilled_handle="h",
                prefilled_at=datetime.utcnow() - timedelta(days=5))
        _trials(HOLD - 1, prefix="out")
        u = _add(username="outside")
        assert _me(_client(real_app, u))["approved"] is True

    def test_an_account_let_in_earlier_is_not_put_back(self, real_app):
        now = datetime.utcnow()
        waiting_to_start = _add(
            username="letin", plan="free", approved=True,
            approved_at=now - timedelta(hours=3),
            let_in_at=now - timedelta(hours=3))
        by_hand = _add(username="byhand", plan="free", approved=True)
        _trials(HOLD + 5)
        for u in (waiting_to_start, by_hand):
            me = _me(_client(real_app, u))
            assert me["approved"] is True
            assert me["subscription"]["state"] == "none"
            user = _fresh(u)
            assert user.approved is True and user.approval_hold_at is None

    def test_the_alpha_testers_free_month_takes_no_place(self, real_app):
        _trials(HOLD + 5, prefix="alpha", plan="alpha", let_in_at=None,
                alpha_free_granted_at=datetime.utcnow() - timedelta(days=2))
        u = _add(username="outside")
        assert _me(_client(real_app, u))["approved"] is True


# ── The 24 hours move ───────────────────────────────────────────────────

class TestTheWindowMoves:
    def test_trials_older_than_24_hours_take_no_place(self, real_app):
        _trials(HOLD + 5, hours_ago=25, prefix="old")
        _trials(HOLD - 1, hours_ago=23, prefix="new")
        u = _add(username="newcomer")
        assert _me(_client(real_app, u))["approved"] is True

    def test_a_waiting_account_keeps_waiting_until_the_admin_approves(
            self, real_app):
        _trials(HOLD)
        u = _add(username="held")
        c = _client(real_app, u)
        assert _me(c)["approved"] is False
        # A day later no place is taken any more.
        long_ago = datetime.utcnow() - timedelta(hours=30)
        User.query.filter(User.username.like("t%")).update(
            {"subscription_trial_start": long_ago, "let_in_at": long_ago},
            synchronize_session=False)
        _db.session.commit()
        assert _me(c)["approved"] is False
        # A new account is let in again; the waiting one is the admin's.
        newer = _add(username="newer")
        assert _me(_client(real_app, newer))["approved"] is True
        assert _me(c)["approved"] is False
        _admin_user, admin = _admin(real_app)
        _as_admin(admin).post(f"/api/admin/users/{u.id}/toggle")
        assert _me(c)["approved"] is True


# ── Payments off: as before ─────────────────────────────────────────────

class TestPaymentsOff:
    def test_control_a_new_account_waits_as_it_always_did(self,
                                                          payments_off):
        _trials(HOLD + 5)
        u = _add(username="newcomer")
        c = _client(payments_off, u)
        me = _me(c)
        assert me["approved"] is False
        assert "account_blocked" not in me
        r = c.post("/api/nodes/", json={"content": "hello"})
        assert r.status_code == 403 and r.get_json() == {"error": WAITING}
        # Not marked by this rule: only the approval of before payments.
        assert _fresh(u).approval_hold_at is None

    def test_the_count_is_not_taken(self, payments_off, monkeypatch):
        from backend.utils import trial_alerts

        def never(*a, **kw):
            raise AssertionError("not counted while payments are off")
        monkeypatch.setattr(trial_alerts, "outside_places_since", never)
        monkeypatch.setattr(trial_alerts, "held_at_gate", never)
        u = _add(username="newcomer")
        assert _me(_client(payments_off, u))["approved"] is False
        ok = _add(username="member", approved=True)
        assert _me(_client(payments_off, ok))["approved"] is True

    def test_the_users_list_is_what_it_was(self, payments_off):
        _admin_user, admin = _admin(payments_off)
        _add(username="newcomer")
        rows = _as_admin(admin).get("/api/admin/users").get_json()["users"]
        assert all("waits_for_approval" not in r for r in rows)

    def test_an_account_held_while_payments_were_on_waits_and_is_approved(
            self, real_app):
        _trials(HOLD)
        u = _add(username="held")
        c = _client(real_app, u)
        assert _me(c)["approved"] is False
        real_app.config["PAYMENTS_ENABLED"] = False
        assert _me(c)["approved"] is False
        _admin_user, admin = _admin(real_app)
        _as_admin(admin).post(f"/api/admin/users/{u.id}/toggle")
        assert _me(c)["approved"] is True
        assert _fresh(u).approval_hold_at is None


# ── Two sign-ups at the same moment ─────────────────────────────────────

class TestTwoAtOnce:
    def test_two_new_accounts_at_the_last_place_one_is_let_in(
            self, real_app):
        """Both requests read their account before either was decided.
        Each decides under the lock, in turn: the first takes the last
        place, the second counts it and waits."""
        from backend.utils import approval
        _trials(HOLD - 1)
        a = _add(username="first")
        b = _add(username="second")
        assert not a.approved and not b.approved   # both read, undecided
        with real_app.test_request_context():
            assert approval.let_in_at_gate(a) is True
            _db.session.commit()
            assert approval.let_in_at_gate(b) is False
        assert _fresh(a).approved is True
        held = _fresh(b)
        assert held.approved is False and held.approval_hold_at is not None

    def test_the_place_is_counted_under_the_lock_and_taken_before_release(
            self, real_app, monkeypatch):
        from backend.utils import approval, trial_alerts
        steps = []
        count = trial_alerts.outside_places_since
        monkeypatch.setattr(trial_alerts, "_lock_gate",
                            lambda: steps.append("lock"))
        monkeypatch.setattr(
            trial_alerts, "outside_places_since",
            lambda since: (steps.append("count"), count(since))[1])
        _trials(HOLD - 1)
        u = _add(username="newcomer")
        with real_app.test_request_context():
            assert approval.let_in_at_gate(u) is True
            # Let in within the transaction that holds the lock: nothing
            # was committed between the count and the approval.
            assert u in _db.session.dirty
            assert steps == ["lock", "count"]
            _db.session.commit()
        assert _fresh(u).let_in_at is not None

    def test_two_requests_of_one_account_decide_once(self, real_app):
        """The second request read the account before the first let it
        in. Under the lock it reads the account again: it is let in, it
        takes no second place and is not held by its own place."""
        from backend.utils import approval
        _trials(HOLD - 1)
        u = _add(username="newcomer")
        with real_app.test_request_context():
            assert approval.let_in_at_gate(u) is True
            _db.session.commit()
            first_let_in = u.let_in_at
            # What the second request holds: the row as it was.
            set_committed_value(u, "approved", False)
            set_committed_value(u, "let_in_at", None)
            assert approval.let_in_at_gate(u) is True
            _db.session.commit()
        user = _fresh(u)
        assert user.approved is True and user.approval_hold_at is None
        assert user.let_in_at == first_let_in

    def test_on_postgresql_the_lock_is_one_for_all_accounts(self, real_app,
                                                            monkeypatch):
        from backend.utils import trial_alerts
        executed = []

        class _Bind:
            class dialect:
                name = "postgresql"
        monkeypatch.setattr(_db.session, "get_bind", lambda: _Bind)
        monkeypatch.setattr(
            _db.session, "execute",
            lambda stmt, params=None: executed.append((str(stmt), params)))
        trial_alerts._lock_gate()
        assert executed == [(
            "SELECT pg_advisory_xact_lock(:namespace, 0)",
            {"namespace": trial_alerts._GATE_LOCK_NAMESPACE})]
