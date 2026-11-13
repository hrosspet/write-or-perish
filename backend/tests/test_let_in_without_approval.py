"""Payments on: nobody waits for approval; the admin can block (#697).

Peter, 2026-11-13: "waiting for my approval is a bad first UX". With
payments on, a new account goes from the terms straight to the subscribe
step, a whitelist or a pre-fill counts as the approval, and the Users
tab's switch blocks and unblocks. With payments off every path is what it
was: the tests marked "control" pass on the base branch too.

The approval gate is a before_request hook of create_app(), so this file
builds the real app (sqlite in memory, the celery glue stubbed, as
test_payments_gate.py does). Stripe is never called.
"""
import os
import sys
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import APICostLog, User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

FRONTEND_URL = "https://loore.test"
WAITING = "Your account is not approved. Please wait for approval."
NOT_ACTIVE = "This account is not active. Write to info@loore.org."


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
    app.config["FRONTEND_URL"] = FRONTEND_URL
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
    """An account as sign-up makes it: unapproved, the plan's default
    (``free`` while payments are on, ``alpha`` while off)."""
    kw.setdefault("approved", False)
    u = User(**kw)
    _db.session.add(u)
    _db.session.commit()
    return u


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


# ── 1. A new account is not held for approval ───────────────────────────

class TestNewAccountGoesToTheTrialStep:
    def test_first_request_lets_it_in_to_the_subscribe_step(self, real_app):
        u = _add(username="newcomer", email="new@example.com")
        assert u.plan == "free"   # payments on: the default
        c = _client(real_app, u)
        me = _me(c)
        assert me["approved"] is True
        assert me["account_blocked"] is False
        assert me["subscription"]["state"] == "none"   # the subscribe page
        user = _fresh(u)
        assert user.approved is True
        assert user.approved_at is not None
        # Anything else of the app: the subscription gate, not the waitlist.
        r = c.post("/api/nodes/", json={"content": "hello"})
        assert r.status_code == 402
        assert r.get_json()["error"] == "no_subscription"

    def test_control_payments_off_it_waits(self, payments_off):
        u = _add(username="newcomer", email="new@example.com")
        assert u.plan == "alpha"
        c = _client(payments_off, u)
        me = _me(c)
        assert me["approved"] is False
        assert "account_blocked" not in me
        r = c.post("/api/nodes/", json={"content": "hello"})
        assert r.status_code == 403
        assert r.get_json() == {"error": WAITING}
        assert _fresh(u).approved is False

    def test_x_sign_in_of_a_new_account(self, real_app):
        c = real_app.test_client()
        x = MagicMock()
        x.authorized = True
        x.get.return_value = MagicMock(
            ok=True, json=lambda: {"id": 777, "screen_name": "birdy"})
        with patch("backend.routes.auth.twitter", x):
            r = c.get("/auth/login")
        assert r.status_code == 302
        u = User.query.filter_by(twitter_id="777").one()
        me = _me(c)
        assert me["approved"] is True
        assert me["subscription"]["state"] == "none"
        assert _fresh(u).approved is True

    def test_email_sign_in_of_a_new_account(self, real_app):
        from backend.utils.magic_link import generate_magic_link_token
        c = real_app.test_client()
        token = generate_magic_link_token("fresh@example.com")
        r = c.get(f"/auth/magic-link/verify?token={token}")
        assert r.status_code == 302
        u = User.query.filter_by(email="fresh@example.com").one()
        me = _me(c)
        assert me["approved"] is True
        assert me["subscription"]["state"] == "none"
        assert _fresh(u).approved is True

    def test_an_alpha_signup_never_let_in_starts_at_the_trial(self, real_app):
        # Signed up during the alpha (plan "alpha", the default then),
        # never approved: let in like any new account, to the trial step,
        # not to the alpha plan's free access.
        u = _add(username="waited", plan="alpha", email="w@example.com")
        me = _me(_client(real_app, u))
        assert me["approved"] is True
        assert me["subscription"]["state"] == "none"
        user = _fresh(u)
        assert user.plan == "free"
        assert not user.on_paid_plan

    def test_a_plan_set_by_hand_stays(self, real_app):
        u = _add(username="friend", plan="pro")
        me = _me(_client(real_app, u))
        assert me["approved"] is True
        assert _fresh(u).plan == "pro"
        assert me["subscription"]["state"] == "set_by_hand"

    def test_a_system_account_is_never_let_in(self, real_app):
        from backend.utils.approval import let_in
        from backend.utils.system_accounts import POLL_SYSTEM_USERNAME
        u = _add(username=POLL_SYSTEM_USERNAME, plan="free")
        assert let_in(u) is False
        assert u.approved is False

    def test_the_consent_card_saves_on_the_subscribe_step(self, real_app):
        # "Start from your tweets?" moved from the waitlist to the
        # subscribe page: an account at the trial step may answer it.
        u = _add(username="birdy", twitter_id="778", approved=True)
        c = _client(real_app, u)
        assert _me(c)["subscription"]["state"] == "none"
        r = c.put("/api/dashboard/user", json={"prefill_consent": "yes"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["user"]["prefill_consent"] == "yes"
        assert _fresh(u).prefill_consent == "yes"


# ── 4. Nothing paid runs before a trial ─────────────────────────────────

def _routes(app):
    adapter = app.url_map.bind("localhost")
    out = []
    for rule in app.url_map.iter_rules():
        if not (rule.rule.startswith("/api") or rule.rule.startswith("/media")):
            continue
        values = {arg: 1 if type(conv).__name__ == "IntegerConverter" else "x"
                  for arg, conv in rule._converters.items()}
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((method, adapter.build(rule.endpoint, values,
                                              method=method)))
    return out


def test_an_account_let_in_reaches_nothing_paid(real_app):
    """A fresh account let in without approval: every /api and /media route
    outside the subscribe step's short list answers 402 before its view
    runs, no cost is logged, and no background job counts it."""
    from backend.utils.subscriptions import (
        ai_replies_allowed, open_without_subscription)
    u = _add(username="stranger", email="s@example.com")
    c = _client(real_app, u)
    assert _me(c)["approved"] is True
    refused = 0
    for method, url in _routes(real_app):
        if open_without_subscription(method, url):
            continue
        r = c.open(url, method=method, json={})
        assert r.status_code == 402, (method, url, r.status_code)
        refused += 1
    assert refused > 150
    assert APICostLog.query.filter_by(user_id=u.id).count() == 0
    user = _fresh(u)
    assert not ai_replies_allowed(user)
    assert user not in User.profile_eligible_query().all()
    assert User.query.filter(User.id == u.id,
                             User.paid_features_clause()).count() == 0


# ── 3. The block switch ─────────────────────────────────────────────────

def _blocked(**kw):
    return _add(deactivated_at=datetime.utcnow() - timedelta(days=1), **kw)


class TestBlocked:
    def test_sees_not_active_and_reaches_nothing(self, real_app):
        u = _blocked(username="blocked", email="b@example.com")
        c = _client(real_app, u)
        me = _me(c)
        assert me["approved"] is False
        assert me["account_blocked"] is True
        for method, url in (("POST", "/api/nodes/"),
                            ("POST", "/api/billing/checkout"),
                            ("GET", "/api/billing/"),
                            ("POST", "/api/billing/portal")):
            r = c.open(url, method=method, json={"level": "standard"})
            assert r.status_code == 403, (url, r.status_code)
            assert r.get_json() == {"error": NOT_ACTIVE,
                                    "code": "account_not_active"}
        assert _fresh(u).approved is False

    @pytest.mark.parametrize("kw", [
        {"subscription_status": "active", "subscription_level": "standard",
         "stripe_customer_id": "cus_b", "subscription_ever_paid": True},
        {"subscription_status": "trialing", "subscription_level": "supporter"},
        {"plan": "pro"},
    ])
    def test_whatever_its_subscription(self, real_app, monkeypatch, kw):
        from backend.utils import stripe_billing
        monkeypatch.setattr(stripe_billing, "get_gateway", _no_stripe)
        u = _blocked(username="payer", **kw)
        c = _client(real_app, u)
        assert _me(c)["account_blocked"] is True
        for url in ("/api/nodes/", "/api/billing/checkout",
                    "/api/billing/portal"):
            r = c.post(url, json={"level": "standard"})
            assert r.status_code == 403, (url, r.status_code)
            assert r.get_json()["code"] == "account_not_active"
        # Nothing of the subscription is touched.
        user = _fresh(u)
        assert user.subscription_status == kw.get("subscription_status",
                                                  "none")
        assert user.approved is False

    def test_control_profile_updates_already_skip_it(self, real_app):
        # The profile and recent-context jobs pick from
        # profile_eligible_query, which needs `approved`; the other jobs
        # use approval.background_work_allowed (their own tests).
        from backend.utils.approval import (
            background_work_allowed, background_work_clause)
        u = _blocked(username="payer", subscription_status="active",
                     subscription_level="standard", default_ai_usage="chat")
        assert u not in User.profile_eligible_query().all()
        assert not background_work_allowed(u)
        assert User.query.filter(User.id == u.id,
                                 *background_work_clause()).count() == 0
        u.approved = True   # unblocked
        _db.session.commit()
        assert background_work_allowed(u)
        assert User.query.filter(User.id == u.id,
                                 *background_work_clause()).count() == 1

    def test_control_deleting_itself_stays_open(self, real_app):
        u = _blocked(username="leaving", email="l@example.com")
        r = _client(real_app, u).post("/api/account/delete", json={})
        assert r.status_code != 403

    def test_control_payments_off_the_waiting_answer(self, payments_off):
        u = _blocked(username="blocked")
        c = _client(payments_off, u)
        assert "account_blocked" not in _me(c)
        r = c.post("/api/nodes/", json={})
        assert r.status_code == 403
        assert r.get_json() == {"error": WAITING}


def _no_stripe(*a, **k):
    raise AssertionError("Stripe must not be called")


def _admin(app):
    admin = User(username="root", approved=True, is_admin=True)
    _db.session.add(admin)
    _db.session.commit()
    return admin, _client(app, admin)


class TestTheSwitch:
    def test_blocks_and_unblocks(self, real_app):
        _admin_user, admin = _admin(real_app)
        u = _add(username="someone", approved=True)
        r = admin.post(f"/api/admin/users/{u.id}/toggle")
        assert r.get_json() == {"message": "User status updated",
                                "approved": False, "blocked": True}
        assert _fresh(u).deactivated_at is not None
        # The blocked account is not let in by its next request.
        assert _me(_client(real_app, u))["account_blocked"] is True
        assert _fresh(u).approved is False
        from flask import g
        g.pop("_login_user", None)   # the admin's request again
        r = admin.post(f"/api/admin/users/{u.id}/toggle")
        assert r.get_json()["blocked"] is False
        assert _fresh(u).approved is True

    def test_activate_by_hand_still_approves(self, real_app):
        # An account never approved keeps Activate: it approves by hand,
        # as before (the alpha free month's convert_if_alpha included),
        # instead of waiting for the account's next request.
        _admin_user, admin = _admin(real_app)
        u = _add(username="notyet", plan="alpha")
        r = admin.post(f"/api/admin/users/{u.id}/toggle")
        assert r.get_json() == {"message": "User status updated",
                                "approved": True, "blocked": False}
        user = _fresh(u)
        assert user.approved is True and user.deactivated_at is None
        assert user.plan == "alpha"

    def test_the_users_list_says_blocked(self, real_app):
        _admin_user, admin = _admin(real_app)
        _blocked(username="blocked")
        _add(username="notyet")
        rows = {r["username"]: r
                for r in admin.get("/api/admin/users").get_json()["users"]}
        assert rows["blocked"]["blocked"] is True
        assert rows["notyet"]["blocked"] is False
        assert rows["root"]["blocked"] is False

    def test_control_payments_off_the_toggle_approves(self, payments_off):
        _admin_user, admin = _admin(payments_off)
        u = _add(username="notyet")
        r = admin.post(f"/api/admin/users/{u.id}/toggle")
        assert r.get_json() == {"message": "User status updated",
                                "approved": True}
        assert _fresh(u).approved is True
        rows = admin.get("/api/admin/users").get_json()["users"]
        assert all("blocked" not in row for row in rows)


# ── 2. Whitelist and pre-fill count as the approval ─────────────────────

def _resolved(monkeypatch, x_id="555"):
    from backend.utils import x_identity
    monkeypatch.setattr(x_identity, "resolve_x_id", lambda *a, **k: MagicMock(
        x_id=x_id, source="community-archive", username="alice",
        display_name="Alice", paid_reads=0))


def _tasks(monkeypatch):
    import importlib
    tasks = {}
    for name in ("prefill_community_archive", "prefill_x_api"):
        task = MagicMock()
        task.delay.return_value.id = "task-1"
        monkeypatch.setattr(importlib.import_module("backend.tasks.imports"),
                            name, task)
        tasks[name] = task
    return tasks


class TestAheadOfTheOwner:
    def test_whitelist_is_the_approval(self, real_app, monkeypatch):
        _resolved(monkeypatch)
        _admin_user, admin = _admin(real_app)
        r = admin.post("/api/admin/whitelist", json={"handle": "alice"})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["user"]["approved"] is True
        u = User.query.filter_by(username="alice").one()
        assert u.approved_at is not None
        assert u.plan == "free"   # the trial step, not free access
        # The owner's X sign-in lands in it, ready: the subscribe step.
        c = real_app.test_client()
        x = MagicMock()
        x.authorized = True
        x.get.return_value = MagicMock(
            ok=True, json=lambda: {"id": 555, "screen_name": "alice"})
        with patch("backend.routes.auth.twitter", x):
            c.get("/auth/login")
        me = _me(c)
        assert me["id"] == u.id
        assert me["approved"] is True
        assert me["subscription"]["state"] == "none"

    def test_control_payments_off_whitelist_waits(self, payments_off,
                                                  monkeypatch):
        _resolved(monkeypatch)
        _admin_user, admin = _admin(payments_off)
        r = admin.post("/api/admin/whitelist", json={"handle": "alice"})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["user"]["approved"] is False

    @pytest.mark.parametrize("route,task", [
        ("prefill", "prefill_community_archive"),
        ("prefill-x", "prefill_x_api"),
    ])
    def test_starting_a_prefill_is_the_approval(self, real_app, monkeypatch,
                                                route, task):
        from backend.utils import email as email_mod
        sent = []
        monkeypatch.setattr(email_mod, "_send",
                            lambda *a, **k: sent.append(a))
        tasks = _tasks(monkeypatch)
        _admin_user, admin = _admin(real_app)
        u = _add(username="bird", twitter_id="556", plan="alpha")
        r = admin.post(f"/api/admin/users/{u.id}/{route}",
                       json={"handle": "bird"})
        assert r.status_code == 202, r.get_json()
        assert tasks[task].delay.called
        user = _fresh(u)
        assert user.approved is True and user.approved_at is not None
        assert user.plan == "free"
        assert sent == []   # no welcome email

    def test_control_a_prefill_does_not_unblock(self, real_app, monkeypatch):
        _tasks(monkeypatch)
        _admin_user, admin = _admin(real_app)
        u = _blocked(username="bird", twitter_id="557")
        admin.post(f"/api/admin/users/{u.id}/prefill", json={"handle": "bird"})
        assert _fresh(u).approved is False

    def test_control_payments_off_prefill_does_not_approve(
            self, payments_off, monkeypatch):
        _tasks(monkeypatch)
        _admin_user, admin = _admin(payments_off)
        u = _add(username="bird", twitter_id="558")
        r = admin.post(f"/api/admin/users/{u.id}/prefill",
                       json={"handle": "bird"})
        assert r.status_code == 202
        assert _fresh(u).approved is False


# ── The "New signup" email ──────────────────────────────────────────────

def _notices(monkeypatch):
    calls = []
    import backend.routes.terms as terms
    monkeypatch.setattr(terms, "send_admin_signup_notification",
                        lambda name, email, let_in=False:
                        calls.append((name, let_in)))
    return calls


class TestSignupEmail:
    def test_first_acceptance_is_information(self, real_app, monkeypatch):
        calls = _notices(monkeypatch)
        u = _add(username="newcomer", email="n@example.com")
        c = _client(real_app, u)
        assert c.post("/api/terms/accept").status_code == 200
        assert calls == [("newcomer", True)]
        # Accepting a newer version later sends nothing.
        assert c.post("/api/terms/accept").status_code == 200
        assert calls == [("newcomer", True)]

    def test_not_for_a_blocked_account(self, real_app, monkeypatch):
        calls = _notices(monkeypatch)
        u = _blocked(username="blocked")
        assert _client(real_app, u).post("/api/terms/accept").status_code \
            == 200
        assert calls == []

    def test_control_payments_off_asks_for_approval(self, payments_off,
                                                    monkeypatch):
        calls = _notices(monkeypatch)
        u = _add(username="newcomer")
        c = _client(payments_off, u)
        c.post("/api/terms/accept")
        c.post("/api/terms/accept")
        assert calls == [("newcomer", False), ("newcomer", False)]
        ok = _add(username="approved", approved=True)
        _client(payments_off, ok).post("/api/terms/accept")
        assert len(calls) == 2

    def test_the_wording(self, real_app, monkeypatch):
        from backend.utils import email as email_mod
        sent = []
        monkeypatch.setattr(email_mod, "_send",
                            lambda to, subject, text, html, *a, **k:
                            sent.append((subject, text, html)))
        email_mod.send_admin_signup_notification("ann", "a@x.org", let_in=True)
        email_mod.send_admin_signup_notification("bob", None)
        (s1, t1, h1), (s2, t2, h2) = sent
        assert s1 == "New signup: ann"
        assert "Nothing to approve." in t1 and "Nothing to approve." in h1
        assert "Review and approve" not in t1
        assert "Review and approve in the admin dashboard" in t2
        assert "Nothing to approve" not in t2 + h2


# ── 4. One trial per account, also for accounts let in ──────────────────

class _Gateway:
    def __init__(self):
        self.cancelled = []

    def update_subscription(self, sub_id, params):
        return {"id": sub_id, **params}

    def cancel_subscription(self, sub_id):
        self.cancelled.append(sub_id)
        return {"id": sub_id, "status": "canceled", "customer": "cus_t",
                "items": {"data": []}, "metadata": {}}

    def retrieve_invoice(self, *a, **k):
        return None


def test_one_trial_per_account_holds_for_an_account_let_in(real_app):
    from backend.utils import stripe_billing
    u = _add(username="trialist", email="t@example.com")
    _me(_client(real_app, u))
    user = _fresh(u)
    assert user.approved is True
    params = stripe_billing.checkout_session_params(user, "standard", "p")
    assert params["subscription_data"]["trial_period_days"] == 14
    user.stripe_customer_id = "cus_t"
    user.subscription_trial_used = True
    user.subscription_trial_sub_id = "sub_first"
    user.subscription_status = "ended"
    _db.session.commit()
    params = stripe_billing.checkout_session_params(user, "standard", "p")
    assert "trial_period_days" not in params["subscription_data"]
    # A second trialing subscription for it is cancelled.
    gw = _Gateway()
    stripe_billing.settle_subscription(gw, {
        "id": "sub_second", "status": "trialing", "customer": "cus_t",
        "created": 1, "trial_start": 1, "trial_end": 2, "latest_invoice": None,
        "metadata": {}, "items": {"data": [{"price": {
            "id": "p", "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[
                "standard"]}}]}}, lock=False)
    assert gw.cancelled == ["sub_second"]
