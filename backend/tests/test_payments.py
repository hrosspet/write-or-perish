"""Payments (#438): the switch, the subscription on the account, Checkout,
the webhook, discount codes, the catalog sync and the admin lists.

Minimal app on sqlite in memory with the billing, webhook, admin and
dashboard blueprints. Stripe is never called: get_gateway is replaced by
FakeGateway, and webhook events are signed here with a test secret the
way Stripe signs them.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta

import pytest
from flask import Flask, g

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from backend.config import Config  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    CheckoutSession, DiscountCodeAttempt, DiscountRedemption, StripeEvent,
    User)
from backend.utils import stripe_billing  # noqa: E402

SECRET = "whsec_test_secret"
NOW = int(time.time())


class FakeGateway:
    """Stands in for StripeGateway: records calls, returns plain dicts."""

    def __init__(self):
        self.calls = []
        self.subscriptions = {}
        self.sessions = {}
        self.products = {}
        self.prices = []
        self.portal_configs = []
        self.invoices = {}
        self.refunds = {}
        self.charges = {}
        self.fail = False
        self.fail_refunds = 0
        self.cancel_error = None
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}_{self._n}"

    def _call(self, name, *args):
        self.calls.append((name,) + args)
        if self.fail:
            raise RuntimeError("stripe down")

    def names(self):
        return [c[0] for c in self.calls]

    # catalog
    def retrieve_product(self, pid):
        self._call("retrieve_product", pid)
        return self.products.get(pid)

    def create_product(self, params):
        self._call("create_product", params)
        self.products[params["id"]] = {"active": True, **params}
        return self.products[params["id"]]

    def update_product(self, pid, params):
        self._call("update_product", pid, params)
        self.products[pid].update(params)
        return self.products[pid]

    def list_prices(self, lookup_keys):
        self._call("list_prices", list(lookup_keys))
        keys = set(lookup_keys)
        return [p for p in self.prices
                if p["active"] and p.get("lookup_key") in keys]

    def create_price(self, params):
        self._call("create_price", params)
        if params.get("transfer_lookup_key"):
            for p in self.prices:
                if p.get("lookup_key") == params["lookup_key"]:
                    p["lookup_key"] = None
        price = {"id": self._id("price"), "active": True,
                 "unit_amount": params["unit_amount"],
                 "currency": params["currency"],
                 "recurring": {"interval": "month", "interval_count": 1},
                 "product": params["product"],
                 "tax_behavior": params["tax_behavior"],
                 "lookup_key": params["lookup_key"],
                 "metadata": params.get("metadata", {})}
        self.prices.append(price)
        return price

    def update_price(self, price_id, params):
        self._call("update_price", price_id, params)
        for p in self.prices:
            if p["id"] == price_id:
                p.update(params)
                return p

    def list_portal_configurations(self):
        self._call("list_portal_configurations")
        return list(self.portal_configs)

    def create_portal_configuration(self, params):
        self._call("create_portal_configuration", params)
        conf = {"id": self._id("bpc"), **params}
        self.portal_configs.append(conf)
        return conf

    def update_portal_configuration(self, cid, params):
        self._call("update_portal_configuration", cid, params)
        return {"id": cid, **params}

    # customers, sessions, subscriptions
    def create_customer(self, params, idempotency_key=None):
        self._call("create_customer", params, idempotency_key)
        return {"id": self._id("cus")}

    def create_checkout_session(self, params):
        self._call("create_checkout_session", params)
        sid = self._id("cs_test")
        self.sessions[sid] = {"id": sid, "status": "open", **params}
        return {"id": sid, "url": f"https://checkout.example/{sid}"}

    def retrieve_checkout_session(self, sid):
        self._call("retrieve_checkout_session", sid)
        return self.sessions[sid]

    def expire_checkout_session(self, sid):
        self._call("expire_checkout_session", sid)
        # Stripe refuses to expire a session that is no longer open.
        if self.sessions[sid]["status"] != "open":
            raise RuntimeError("Only open sessions can be expired")
        self.sessions[sid]["status"] = "expired"
        return self.sessions[sid]

    def retrieve_invoice(self, invoice_id):
        self._call("retrieve_invoice", invoice_id)
        return self.invoices.get(invoice_id, {"id": invoice_id,
                                              "amount_paid": 0})

    def create_refund(self, params, idempotency_key=None):
        self._call("create_refund", params, idempotency_key)
        if self.fail_refunds:
            self.fail_refunds -= 1
            raise RuntimeError("refund failed")
        refund = {"id": self._id("re"), **params}
        self.refunds.setdefault(params["payment_intent"], []).append(refund)
        return refund

    def list_refunds(self, payment_intent):
        self._call("list_refunds", payment_intent)
        return list(self.refunds.get(payment_intent, []))

    def retrieve_charge(self, charge_id):
        self._call("retrieve_charge", charge_id)
        return self.charges[charge_id]

    def delete_customer(self, customer_id):
        self._call("delete_customer", customer_id)
        if self.fail:
            raise RuntimeError("stripe down")
        return {"id": customer_id, "deleted": True}

    def create_portal_session(self, params):
        self._call("create_portal_session", params)
        return {"url": "https://billing.example/session"}

    def retrieve_subscription(self, sub_id):
        self._call("retrieve_subscription", sub_id)
        return self.subscriptions[sub_id]

    def update_subscription(self, sub_id, params):
        self._call("update_subscription", sub_id, params)
        sub = self.subscriptions[sub_id]
        params = dict(params)
        if "metadata" in params:  # Stripe merges metadata keys
            sub["metadata"] = {**(sub.get("metadata") or {}),
                               **params.pop("metadata")}
        sub.update(params)
        return sub

    def cancel_subscription(self, sub_id):
        self._call("cancel_subscription", sub_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        self.subscriptions[sub_id]["status"] = "canceled"
        return self.subscriptions[sub_id]

    def release_subscription_schedule(self, schedule_id):
        self._call("release_subscription_schedule", schedule_id)
        for sub in self.subscriptions.values():
            if sub.get("schedule") == schedule_id:
                sub["schedule"] = None
        return {"id": schedule_id, "status": "released"}


def subscription(sub_id="sub_1", customer="cus_1", status="active",
                 level="standard", trial=False, cancel_at_period_end=False,
                 user_id=None, period_end=NOW + 30 * 86400, created=NOW,
                 latest_invoice=None, code_id=None):
    meta = {"loore_user_id": str(user_id)} if user_id else {}
    if code_id is not None:
        meta["loore_code_id"] = str(code_id)
    return {
        "id": sub_id, "object": "subscription", "customer": customer,
        "status": status, "created": created,
        "latest_invoice": latest_invoice,
        "cancel_at_period_end": cancel_at_period_end,
        "cancel_at": period_end if cancel_at_period_end else None,
        "trial_start": NOW if trial else None,
        "trial_end": NOW + 14 * 86400 if trial else None,
        "metadata": meta,
        "items": {"data": [{
            "current_period_end": period_end,
            "price": {"id": f"price_{level}",
                      "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]},
        }]},
    }


def _make_app(payments=True, secret=SECRET, db_uri="sqlite:///:memory:"):
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["PAYMENTS_ENABLED"] = payments
    app.config["STRIPE_SECRET_KEY"] = "sk_test_fake"
    app.config["STRIPE_WEBHOOK_SECRET"] = secret
    app.config["FRONTEND_URL"] = "https://loore.test"
    app.config["PER_USER_MONTHLY_LIMIT_USD"] = 20.0
    app.config["DEPLOY_ENV"] = "testing"
    _db.init_app(app)

    from flask_login import LoginManager
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.admin import admin_bp
    from backend.routes.billing import billing_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.webhooks import webhooks_bp
    app.register_blueprint(billing_bp, url_prefix="/api/billing")
    app.register_blueprint(webhooks_bp, url_prefix="/api/webhooks")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    return app


@pytest.fixture
def gateway(monkeypatch):
    gw = FakeGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    stripe_billing._price_cache.clear()
    for level in ("standard", "supporter", "discounted"):
        gw.prices.append({
            "id": f"price_{level}", "active": True,
            "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]})
    return gw


def _app_fixture(payments, secret=SECRET):
    app = _make_app(payments=payments, secret=secret)
    ctx = app.app_context()
    ctx.push()
    _db.create_all()
    return app, ctx


@pytest.fixture
def app(gateway):
    app, ctx = _app_fixture(True)
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


@pytest.fixture
def app_off(gateway):
    app, ctx = _app_fixture(False, secret=None)
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


def _user(username="alice", plan=None, **kw):
    u = User(username=username, approved=True, **kw)
    if plan is not None:
        u.plan = plan
    _db.session.add(u)
    _db.session.commit()
    return u


def _client(app, user):
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _sign(body, secret=SECRET, t=None):
    t = t or int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.{body}".encode(),
                   hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _post_event(app, event, secret=SECRET, signature=None):
    body = json.dumps(event)
    return app.test_client().post(
        "/api/webhooks/stripe", data=body,
        headers={"Content-Type": "application/json",
                 "Stripe-Signature": signature or _sign(body, secret)})


def _event(etype, obj, event_id="evt_1"):
    return {"id": event_id, "object": "event", "type": etype,
            "data": {"object": obj}}


# ── The switch off: nothing differs ──────────────────────────────────────

class TestSwitchOff:
    def test_billing_routes_answer_404(self, app_off):
        c = _client(app_off, _user())
        assert c.get("/api/billing").status_code == 404
        assert c.post("/api/billing/checkout",
                      json={"level": "standard"}).status_code == 404
        assert c.post("/api/billing/portal").status_code == 404
        assert c.post("/api/billing/sync").status_code == 404

    def test_webhook_answers_404_without_a_secret(self, app_off, gateway):
        r = _post_event(app_off, _event("customer.subscription.updated",
                                        {"id": "sub_1"}))
        assert r.status_code == 404
        assert gateway.calls == []

    def test_admin_lists_answer_404(self, app_off):
        admin = _user("root", is_admin=True)
        c = _client(app_off, admin)
        assert c.get("/api/admin/subscriptions").status_code == 404
        assert c.get("/api/admin/discount-codes").status_code == 404
        assert c.post("/api/admin/discount-codes",
                      json={}).status_code == 404

    def test_user_payload_has_no_payment_keys(self, app_off):
        c = _client(app_off, _user())
        user = c.get("/api/dashboard/?profile=0").get_json()["user"]
        assert "payments_enabled" not in user
        assert "subscription" not in user

    def test_new_accounts_start_on_alpha(self, app_off):
        assert _user().plan == "alpha"

    def test_free_accounts_keep_ai_replies(self, app_off):
        from backend.utils.subscriptions import (
            ai_replies_allowed, check_ai_replies,
            subscription_required_fields)
        u = _user(plan="free")
        assert ai_replies_allowed(u)
        check_ai_replies(u)  # no raise
        assert subscription_required_fields(u) is None

    def test_spend_cap_wording_is_the_free_alphas(self, app_off):
        from backend.utils.spend import spend_cap_message
        assert spend_cap_message().startswith(
            "You've reached your monthly usage limit for the free alpha.")

    def test_spend_limit_is_the_global_default(self, app_off):
        from backend.utils.spend import get_user_spend_limit_usd
        assert get_user_spend_limit_usd(_user(), app_off.config) == 20.0
        assert get_user_spend_limit_usd(
            _user("bob", plan="free"), app_off.config) == 20.0

    def test_profile_cohort_is_the_plan_check(self, app_off):
        alpha = _user("a")
        _user("f", plan="free")
        pro = _user("p", plan="pro")
        ids = {u.id for u in User.profile_eligible_query().all()}
        assert ids == {alpha.id, pro.id}


# ── The switch on ────────────────────────────────────────────────────────

class TestAccess:
    def test_new_accounts_start_free(self, app):
        assert _user().plan == "free"

    def test_hand_set_accounts_keep_their_plan_and_features(self, app):
        from backend.utils.subscriptions import ai_replies_allowed
        a = _user("a", plan="alpha")
        p = _user("p", plan="pro")
        assert a.on_paid_plan and p.on_paid_plan
        assert ai_replies_allowed(a) and ai_replies_allowed(p)

    def test_free_and_ended_accounts_get_no_replies(self, app):
        from backend.utils.subscriptions import (
            SubscriptionRequired, check_ai_replies,
            subscription_required_fields)
        free = _user("f")
        ended = _user("e", subscription_status="ended",
                      subscription_level="standard")
        for u in (free, ended):
            with pytest.raises(SubscriptionRequired):
                check_ai_replies(u)
            fields = subscription_required_fields(u)
            assert fields["subscription_required"] is True
        assert "ended" in subscription_required_fields(ended)[
            "subscription_message"]
        assert not free.has_voice_mode and not ended.has_voice_mode

    def test_admins_always_get_replies(self, app):
        from backend.utils.subscriptions import ai_replies_allowed
        assert ai_replies_allowed(_user("root", is_admin=True))

    @pytest.mark.parametrize("status", ["trialing", "active"])
    def test_a_live_subscription_gives_the_paid_set(self, app, status):
        from backend.utils.subscriptions import ai_replies_allowed
        u = _user(subscription_status=status, subscription_level="standard")
        assert u.on_paid_plan and u.has_voice_mode
        assert ai_replies_allowed(u)

    def test_profile_cohort_follows_the_subscription(self, app):
        alpha = _user("a", plan="alpha")
        _user("f")
        sub = _user("s", subscription_status="active",
                    subscription_level="supporter")
        _user("e", subscription_status="ended",
              subscription_level="standard")
        ids = {u.id for u in User.profile_eligible_query().all()}
        assert ids == {alpha.id, sub.id}

    def test_spend_limits(self, app):
        from backend.utils.spend import get_user_spend_limit_usd
        cfg = app.config
        std = _user("s", subscription_status="active",
                    subscription_level="standard")
        sup = _user("u", subscription_status="trialing",
                    subscription_level="supporter")
        disc = _user("d", subscription_status="active",
                     subscription_level="discounted")
        alpha = _user("a", plan="alpha", subscription_status="active",
                      subscription_level="supporter")
        override = _user("o", subscription_status="active",
                         subscription_level="standard",
                         monthly_spend_limit_usd=7.0)
        ended = _user("e", subscription_status="ended",
                      subscription_level="supporter")
        assert get_user_spend_limit_usd(std, cfg) == 50.0
        assert get_user_spend_limit_usd(sup, cfg) == 100.0
        assert get_user_spend_limit_usd(disc, cfg) == 50.0
        # Set by hand: the subscription changes nothing.
        assert get_user_spend_limit_usd(alpha, cfg) == 20.0
        # The admin's per-user limit wins.
        assert get_user_spend_limit_usd(override, cfg) == 7.0
        assert get_user_spend_limit_usd(ended, cfg) == 20.0

    def test_spend_cap_wording(self, app):
        from backend.utils.spend import spend_cap_message
        msg = spend_cap_message()
        assert "free alpha" not in msg
        assert msg.startswith("You've reached your monthly usage limit.")

    def test_user_payload(self, app):
        u = _user(subscription_status="trialing",
                  subscription_level="standard",
                  subscription_trial_used=True)
        data = _client(app, u).get("/api/dashboard/?profile=0").get_json()
        user = data["user"]
        assert user["payments_enabled"] is True
        sub = user["subscription"]
        assert sub["status"] == "trialing" and sub["level"] == "standard"
        assert sub["ai_replies"] is True
        assert sub["trial_available"] is False
        assert "stripe_customer_id" not in sub


# ── Checkout, portal, sync ───────────────────────────────────────────────

class TestCheckout:
    def test_state(self, app):
        data = _client(app, _user()).get("/api/billing").get_json()
        assert [lv["level"] for lv in data["levels"]] == [
            "standard", "supporter"]
        assert [lv["amount_cents"] for lv in data["levels"]] == [3000, 9900]
        assert data["subscription"]["trial_available"] is True
        assert data["subscription"]["trial_days"] == 14

    def test_standard_with_a_trial(self, app, gateway):
        u = _user()
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "standard"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["url"].startswith("https://checkout.example/")
        params = [c for c in gateway.calls
                  if c[0] == "create_checkout_session"][0][1]
        assert params["mode"] == "subscription"
        assert params["managed_payments"] == {"enabled": True}
        assert params["line_items"] == [{"price": "price_standard",
                                         "quantity": 1}]
        assert params["subscription_data"]["trial_period_days"] == 14
        assert params["client_reference_id"] == str(u.id)
        assert params["customer"] == _db.session.get(
            User, u.id).stripe_customer_id
        assert params["success_url"].startswith(
            "https://loore.test/account?subscription=success")
        assert "{CHECKOUT_SESSION_ID}" in params["success_url"]
        assert "automatic_tax" not in params
        assert "discounts" not in params

    def test_the_customer_is_made_once(self, app, gateway):
        c = _client(app, _user())
        c.post("/api/billing/checkout", json={"level": "standard"})
        c.post("/api/billing/checkout", json={"level": "supporter"})
        assert gateway.names().count("create_customer") == 1

    def test_no_second_trial(self, app, gateway):
        u = _user(subscription_trial_used=True,
                  subscription_status="ended", subscription_level="standard")
        _client(app, u).post("/api/billing/checkout",
                             json={"level": "supporter"})
        params = [c for c in gateway.calls
                  if c[0] == "create_checkout_session"][0][1]
        assert "trial_period_days" not in params["subscription_data"]
        assert params["line_items"][0]["price"] == "price_supporter"

    def test_refusals(self, app, gateway):
        c = _client(app, _user())
        assert c.post("/api/billing/checkout",
                      json={"level": "discounted"}).status_code == 400
        assert c.post("/api/billing/checkout", json={}).status_code == 400
        alpha = _client(app, _user("a", plan="alpha"))
        r = alpha.post("/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 409
        assert r.get_json()["error"] == "plan_set_by_hand"
        sub = _client(app, _user("s", subscription_status="active",
                                 subscription_level="standard"))
        r = sub.post("/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 409
        assert "create_checkout_session" not in gateway.names()

    def test_stripe_down_is_a_503(self, app, gateway):
        gateway.fail = True
        r = _client(app, _user()).post("/api/billing/checkout",
                                       json={"level": "standard"})
        assert r.status_code == 503
        assert r.get_json()["error"] == "billing_unavailable"

    def test_portal(self, app, gateway):
        assert _client(app, _user("n")).post(
            "/api/billing/portal").status_code == 409
        u = _user(stripe_customer_id="cus_9")
        r = _client(app, u).post("/api/billing/portal")
        assert r.status_code == 200
        params = [c for c in gateway.calls
                  if c[0] == "create_portal_session"][0][1]
        assert params["customer"] == "cus_9"
        assert params["return_url"] == "https://loore.test/account#subscription"

    def test_sync_reads_the_users_own_session_only(self, app, gateway):
        alice = _user(stripe_customer_id="cus_a")
        bob = _user("bob", stripe_customer_id="cus_b")
        gateway.sessions["cs_bob"] = {"id": "cs_bob",
                                      "client_reference_id": str(bob.id),
                                      "subscription": "sub_b"}
        gateway.subscriptions["sub_b"] = subscription("sub_b", "cus_b")
        _client(app, alice).post("/api/billing/sync",
                                 json={"session_id": "cs_bob"})
        assert "retrieve_subscription" not in gateway.names()
        assert _db.session.get(User, bob.id).subscription_status == "none"
        _client(app, bob).post("/api/billing/sync",
                               json={"session_id": "cs_bob"})
        assert _db.session.get(User, bob.id).subscription_status == "active"


# ── Discount codes ───────────────────────────────────────────────────────

def _completed(app, user, sub_id, session_id, event_id):
    """Stripe reports the checkout *session_id* completed with *sub_id*."""
    return _post_event(app, _event("checkout.session.completed", {
        "id": session_id, "mode": "subscription",
        "client_reference_id": str(user.id),
        "customer": user.stripe_customer_id, "subscription": sub_id},
        event_id))


class TestDiscountCodes:
    def _code(self, **kw):
        from backend.utils.discount_codes import create_code
        return create_code(code=kw.pop("code", "LOWER-1"), **kw)

    def _session_id(self, r):
        return r.get_json()["url"].rsplit("/")[-1]

    def test_a_code_opens_the_discounted_price(self, app, gateway):
        code = self._code()
        u = _user()
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"code": " lower-1 "})
        assert r.status_code == 200, r.get_data(as_text=True)
        params = [c for c in gateway.calls
                  if c[0] == "create_checkout_session"][0][1]
        assert params["line_items"][0]["price"] == "price_discounted"
        assert params["metadata"]["loore_level"] == "discounted"
        assert params["metadata"]["loore_code_id"] == str(code.id)
        assert params["subscription_data"]["metadata"]["loore_code_id"] == \
            str(code.id)
        # Opening a checkout takes no use.
        assert DiscountRedemption.query.count() == 0
        row = _db.session.get(CheckoutSession, self._session_id(r))
        assert row.code_id == code.id and row.state == "open"

    @pytest.mark.parametrize("kind", ["unknown", "expired", "inactive",
                                      "used_up", "malformed"])
    def test_every_refusal_reads_the_same(self, app, gateway, kind):
        if kind == "expired":
            self._code(expires_at=datetime.utcnow() - timedelta(days=1))
        elif kind == "inactive":
            self._code().active = False
            _db.session.commit()
        elif kind == "used_up":
            code = self._code(max_uses=1)
            other = _user("other")
            _db.session.add(DiscountRedemption(
                code_id=code.id, user_id=other.id,
                stripe_subscription_id="sub_other", state="redeemed"))
            _db.session.commit()
        else:
            self._code()
        sent = {"unknown": "NOPE-NOPE", "malformed": "!"}.get(kind, "LOWER-1")
        r = _client(app, _user()).post("/api/billing/checkout",
                                       json={"code": sent})
        assert r.status_code == 400
        assert r.get_json() == {"error": "code_not_valid",
                                "message": "That code is not valid."}
        assert "create_checkout_session" not in gateway.names()

    def test_an_abandoned_checkout_takes_nothing(self, app, gateway):
        self._code(max_uses=1)
        assert _client(app, _user("a")).post(
            "/api/billing/checkout",
            json={"code": "LOWER-1"}).status_code == 200
        # Never completed: the next account still gets the code.
        assert _client(app, _user("b")).post(
            "/api/billing/checkout",
            json={"code": "LOWER-1"}).status_code == 200
        assert DiscountRedemption.query.count() == 0

    def test_rate_limit(self, app, gateway):
        app.config["DISCOUNT_CODE_ATTEMPTS_PER_HOUR"] = 3
        self._code()
        c = _client(app, _user())
        for _ in range(3):
            assert c.post("/api/billing/checkout",
                          json={"code": "WRONG-1"}).status_code == 400
        r = c.post("/api/billing/checkout", json={"code": "LOWER-1"})
        assert r.status_code == 429
        assert r.get_json()["error"] == "too_many_attempts"
        assert DiscountCodeAttempt.query.count() == 3

    def test_the_use_is_counted_once_when_the_subscription_starts(
            self, app, gateway):
        from backend.utils.discount_codes import uses
        code = self._code(max_uses=5)
        u = _user()
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"code": "LOWER-1"})
        sid = self._session_id(r)
        u = _db.session.get(User, u.id)
        gateway.subscriptions["sub_1"] = subscription(
            customer=u.stripe_customer_id, level="discounted",
            code_id=code.id)
        assert _completed(app, u, "sub_1", sid, "evt_1").status_code == 200
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_1"}, "evt_2"))
        _post_event(app, _event("invoice.paid", {
            "id": "in_1", "subscription": "sub_1"}, "evt_3"))
        assert uses(code) == 1
        red = DiscountRedemption.query.one()
        assert red.stripe_subscription_id == "sub_1" and red.user_id == u.id
        assert _db.session.get(CheckoutSession, sid).state == "settled"
        user = _db.session.get(User, u.id)
        assert user.subscription_level == "discounted"

    def test_one_account_two_checkouts_one_use(self, app, gateway):
        from backend.utils.discount_codes import uses
        code = self._code(max_uses=2)
        u = _user()
        c = _client(app, u)
        first = self._session_id(c.post("/api/billing/checkout",
                                        json={"code": "LOWER-1"}))
        second = self._session_id(c.post("/api/billing/checkout",
                                         json={"code": "LOWER-1"}))
        # The second checkout expired the first at Stripe.
        assert ("expire_checkout_session", first) in gateway.calls
        assert gateway.sessions[first]["status"] == "expired"
        assert _db.session.get(CheckoutSession, first).state == "expired"
        u = _db.session.get(User, u.id)
        gateway.subscriptions["sub_1"] = subscription(
            customer=u.stripe_customer_id, level="discounted",
            code_id=code.id)
        _completed(app, u, "sub_1", second, "evt_1")
        assert uses(code) == 1

    def test_code_with_one_use_left_opened_in_two_accounts(self, app,
                                                           gateway):
        from backend.utils.discount_codes import uses
        code = self._code(max_uses=1)
        a, b = _user("a"), _user("b")
        sa = self._session_id(_client(app, a).post(
            "/api/billing/checkout", json={"code": "LOWER-1"}))
        sb = self._session_id(_client(app, b).post(
            "/api/billing/checkout", json={"code": "LOWER-1"}))
        a = _db.session.get(User, a.id)
        gateway.subscriptions["sub_a"] = subscription(
            "sub_a", a.stripe_customer_id, level="discounted",
            code_id=code.id)
        _completed(app, a, "sub_a", sa, "evt_a")
        assert uses(code) == 1
        # The last use went to a: b's open checkout is expired at Stripe.
        assert gateway.sessions[sb]["status"] == "expired"
        assert _db.session.get(CheckoutSession, sb).state == "expired"
        # A third account is told the code is not valid.
        r = _client(app, _user("c")).post("/api/billing/checkout",
                                          json={"code": "LOWER-1"})
        assert r.status_code == 400

    def test_two_accounts_completing_at_once_are_both_kept(
            self, app, gateway):
        # b finished before a's use expired b's checkout: b paid the price
        # Checkout showed, so it is kept and counted over the limit.
        from backend.utils.discount_codes import uses
        code = self._code(max_uses=1)
        a, b = _user("a"), _user("b")
        sa = self._session_id(_client(app, a).post(
            "/api/billing/checkout", json={"code": "LOWER-1"}))
        sb = self._session_id(_client(app, b).post(
            "/api/billing/checkout", json={"code": "LOWER-1"}))
        gateway.sessions[sb]["status"] = "complete"
        a = _db.session.get(User, a.id)
        b = _db.session.get(User, b.id)
        gateway.subscriptions["sub_a"] = subscription(
            "sub_a", a.stripe_customer_id, level="discounted",
            code_id=code.id)
        gateway.subscriptions["sub_b"] = subscription(
            "sub_b", b.stripe_customer_id, level="discounted",
            code_id=code.id)
        _completed(app, a, "sub_a", sa, "evt_a")
        assert _db.session.get(CheckoutSession, sb).state == "complete"
        _completed(app, b, "sub_b", sb, "evt_b")
        assert uses(code) == 2
        assert "cancel_subscription" not in gateway.names()
        assert _db.session.get(User, b.id).subscription_status == "active"


# ── One subscription and one trial per account ──────────────────────────

class TestOneSubscription:
    def test_a_new_checkout_expires_the_open_one(self, app, gateway):
        u = _user()
        c = _client(app, u)
        r1 = c.post("/api/billing/checkout", json={"level": "standard"})
        r2 = c.post("/api/billing/checkout", json={"level": "supporter"})
        s1 = r1.get_json()["url"].rsplit("/")[-1]
        s2 = r2.get_json()["url"].rsplit("/")[-1]
        assert gateway.sessions[s1]["status"] == "expired"
        assert gateway.sessions[s2]["status"] == "open"
        states = {row.id: row.state for row in CheckoutSession.query.all()}
        assert states == {s1: "expired", s2: "open"}

    def test_a_completed_checkout_blocks_every_next_press(self, app,
                                                          gateway):
        # Completed in another tab, the tab closed at Stripe, no webhook
        # yet: the subscription is read from Stripe at the next press, and
        # every later press is refused too.
        u = _user()
        c = _client(app, u)
        r1 = c.post("/api/billing/checkout", json={"level": "standard"})
        s1 = r1.get_json()["url"].rsplit("/")[-1]
        cus = _db.session.get(User, u.id).stripe_customer_id
        gateway.sessions[s1].update(status="complete", subscription="sub_1")
        gateway.subscriptions["sub_1"] = subscription(
            "sub_1", cus, status="trialing", trial=True)
        for _ in range(3):
            r = c.post("/api/billing/checkout", json={"level": "standard"})
            assert r.status_code == 409
            assert r.get_json()["error"] == "already_subscribed"
        assert gateway.names().count("create_checkout_session") == 1
        assert _db.session.get(CheckoutSession, s1).state == "settled"
        user = _db.session.get(User, u.id)
        assert user.subscription_status == "trialing"
        assert user.subscription_trial_used

    def test_stripe_down_while_closing_the_open_one(self, app, gateway):
        u = _user()
        c = _client(app, u)
        c.post("/api/billing/checkout", json={"level": "standard"})
        gateway.fail = True
        r = c.post("/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 503
        gateway.fail = False
        assert gateway.names().count("create_checkout_session") == 1

    @pytest.mark.parametrize("order", ["older_first", "newer_first"])
    def test_two_sessions_both_completed(self, app, gateway, order):
        # Two subscriptions for one account, whatever slipped through: the
        # one created first is kept, the later one is cancelled and its
        # payment refunded, in either arrival order.
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_old"] = subscription(
            "sub_old", created=NOW - 60, latest_invoice="in_old")
        gateway.subscriptions["sub_new"] = subscription(
            "sub_new", created=NOW, latest_invoice="in_new")
        gateway.invoices["in_new"] = {
            "id": "in_new", "amount_paid": 3000,
            "payments": {"data": [{"payment": {
                "type": "payment_intent", "payment_intent": "pi_new"}}]}}
        gateway.invoices["in_old"] = {"id": "in_old", "amount_paid": 3000}
        first, second = (("sub_old", "sub_new") if order == "older_first"
                         else ("sub_new", "sub_old"))
        _completed(app, u, first, "cs_x", "evt_1")
        _completed(app, u, second, "cs_y", "evt_2")
        # The cancellation's own event changes nothing more.
        _post_event(app, _event("customer.subscription.deleted",
                                {"id": "sub_new"}, "evt_3"))
        user = _db.session.get(User, u.id)
        assert user.stripe_subscription_id == "sub_old"
        assert user.subscription_status == "active"
        assert gateway.subscriptions["sub_new"]["status"] == "canceled"
        assert gateway.subscriptions["sub_old"]["status"] == "active"
        cancels = [c for c in gateway.calls if c[0] == "cancel_subscription"]
        assert cancels == [("cancel_subscription", "sub_new")]
        refunds = [c for c in gateway.calls if c[0] == "create_refund"]
        assert refunds == [("create_refund",
                            {"payment_intent": "pi_new",
                             "reason": "duplicate"},
                            "loore-duplicate-sub_new")]

    def test_a_duplicate_gives_its_code_use_back(self, app, gateway):
        from backend.utils.discount_codes import create_code, uses
        code = create_code(code="LOWER-1", max_uses=3)
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_old"] = subscription(
            "sub_old", created=NOW - 60, level="discounted", code_id=code.id)
        gateway.subscriptions["sub_new"] = subscription(
            "sub_new", created=NOW, level="discounted", code_id=code.id)
        _completed(app, u, "sub_new", "cs_x", "evt_1")
        assert uses(code) == 1
        _completed(app, u, "sub_old", "cs_y", "evt_2")
        assert uses(code) == 1
        states = {r.stripe_subscription_id: r.state
                  for r in DiscountRedemption.query.all()}
        assert states == {"sub_new": "void", "sub_old": "redeemed"}

    def test_trial_used_then_a_second_checkout(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(
            "sub_1", status="trialing", trial=True)
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_1"}, "evt_1"))
        user = _db.session.get(User, u.id)
        assert user.subscription_trial_used is True
        assert user.subscription_trial_sub_id == "sub_1"
        # The trial ends without payment; the next checkout has no trial.
        gateway.subscriptions["sub_1"]["status"] = "canceled"
        _post_event(app, _event("customer.subscription.deleted",
                                {"id": "sub_1"}, "evt_2"))
        _client(app, user).post("/api/billing/checkout",
                                json={"level": "standard"})
        params = [c for c in gateway.calls
                  if c[0] == "create_checkout_session"][-1][1]
        assert "trial_period_days" not in params["subscription_data"]

    def test_a_second_trial_is_cancelled(self, app, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="ended", subscription_level="standard",
                  subscription_trial_used=True,
                  subscription_trial_sub_id="sub_1")
        gateway.subscriptions["sub_1"] = subscription(
            "sub_1", status="canceled", trial=True)
        gateway.subscriptions["sub_2"] = subscription(
            "sub_2", status="trialing", trial=True, latest_invoice="in_2")
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_2"}, "evt_1"))
        user = _db.session.get(User, u.id)
        assert gateway.subscriptions["sub_2"]["status"] == "canceled"
        assert user.subscription_status == "ended"
        assert not user.on_paid_plan
        # Nothing was charged during the trial: nothing to refund.
        assert "create_refund" not in gateway.names()
        assert user.subscription_trial_sub_id == "sub_1"

    def test_the_trial_holder_keeps_its_trial(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(
            "sub_1", status="trialing", trial=True)
        for n in range(3):
            _post_event(app, _event("customer.subscription.updated",
                                    {"id": "sub_1"}, f"evt_{n}"))
        assert "cancel_subscription" not in gateway.names()
        assert _db.session.get(User, u.id).subscription_status == "trialing"


# ── The webhook ──────────────────────────────────────────────────────────

class TestWebhook:
    def test_bad_signature(self, app, gateway):
        r = _post_event(app, _event("customer.subscription.updated",
                                    {"id": "sub_1"}), signature="t=1,v1=00")
        assert r.status_code == 400
        assert StripeEvent.query.count() == 0
        r = _post_event(app, _event("customer.subscription.updated",
                                    {"id": "sub_1"}), secret="whsec_other")
        assert r.status_code == 400

    def test_no_secret_while_on_is_a_503(self, gateway):
        app, ctx = _app_fixture(True, secret=None)
        try:
            r = _post_event(app, _event("x", {}))
            assert r.status_code == 503
        finally:
            _db.session.remove()
            _db.drop_all()
            ctx.pop()

    def test_an_event_is_applied_once(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(trial=True,
                                                      status="trialing")
        ev = _event("customer.subscription.created", {"id": "sub_1"})
        r = _post_event(app, ev)
        assert r.status_code == 200 and r.get_json()["status"] == "applied"
        user = _db.session.get(User, u.id)
        assert user.subscription_status == "trialing"
        assert user.subscription_level == "standard"
        assert user.subscription_trial_used is True
        assert user.subscription_trial_end is not None
        assert user.stripe_subscription_id == "sub_1"
        r = _post_event(app, ev)
        assert r.get_json()["status"] == "duplicate"
        assert gateway.names().count("retrieve_subscription") == 1

    def test_state_comes_from_stripe_not_from_the_event(self, app, gateway):
        # A "deleted" event delivered after the customer subscribed again
        # on the same subscription id would be wrong to trust; Stripe's
        # current state wins.
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(status="active")
        stale = {"id": "sub_1", "status": "canceled"}
        _post_event(app, _event("customer.subscription.deleted", stale))
        assert _db.session.get(User, u.id).subscription_status == "active"

    def test_out_of_order_events_end_in_the_current_state(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(
            status="active", cancel_at_period_end=True)
        _post_event(app, _event("customer.subscription.updated",
                                {"id": "sub_1"}, "evt_b"))
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_1", "status": "trialing"},
                                "evt_a"))
        user = _db.session.get(User, u.id)
        assert user.subscription_status == "active"
        assert user.subscription_cancel_at_period_end is True

    def test_unknown_events_are_acknowledged(self, app, gateway):
        r = _post_event(app, _event("charge.refund.updated", {"id": "re_1"}))
        assert r.status_code == 200 and r.get_json()["status"] == "ignored"
        assert gateway.calls == []
        r = _post_event(app, {"id": "evt_x", "type": "weird.thing",
                              "data": None})
        assert r.status_code == 200

    def test_a_failure_is_retried(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription()
        gateway.fail = True
        ev = _event("customer.subscription.updated", {"id": "sub_1"})
        assert _post_event(app, ev).status_code == 503
        assert StripeEvent.query.count() == 0
        gateway.fail = False
        assert _post_event(app, ev).status_code == 200
        assert _db.session.get(User, u.id).subscription_status == "active"

    def test_invoice_events_find_their_subscription(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(status="past_due")
        inv = {"id": "in_1", "parent": {
            "subscription_details": {"subscription": "sub_1"}}}
        _post_event(app, _event("invoice.payment_failed", inv))
        user = _db.session.get(User, u.id)
        assert user.subscription_status == "past_due"
        # Never paid (a trial whose first payment failed): nothing.
        assert not user.on_paid_plan

    @pytest.mark.parametrize("stripe_status,ours", [
        ("canceled", "ended"), ("unpaid", "ended"),
        ("incomplete_expired", "ended"), ("paused", "ended"),
        ("incomplete", "none")])
    def test_status_map(self, app, gateway, stripe_status, ours):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(status=stripe_status)
        _post_event(app, _event("customer.subscription.updated",
                                {"id": "sub_1"}))
        user = _db.session.get(User, u.id)
        assert user.subscription_status == ours
        assert not user.on_paid_plan
        assert not user.subscription_cancel_at_period_end

    def test_a_hand_set_account_is_never_changed(self, app, gateway):
        a = _user(plan="alpha", stripe_customer_id="cus_1",
                  monthly_spend_limit_usd=None)
        for status in ("active", "canceled"):
            gateway.subscriptions["sub_1"] = subscription(
                status=status, level="supporter")
            _post_event(app, _event("customer.subscription.updated",
                                    {"id": "sub_1"}, f"evt_{status}"))
            user = _db.session.get(User, a.id)
            assert user.plan == "alpha"
            assert user.monthly_spend_limit_usd is None
            assert user.on_paid_plan
            from backend.utils.spend import get_user_spend_limit_usd
            assert get_user_spend_limit_usd(user, app.config) == 20.0

    def test_a_free_account_keeps_its_plan_value(self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription()
        _post_event(app, _event("customer.subscription.updated",
                                {"id": "sub_1"}))
        user = _db.session.get(User, u.id)
        assert user.plan == "free" and user.on_paid_plan

    def test_a_late_event_of_an_older_subscription(self, app, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_2",
                  subscription_status="active",
                  subscription_level="supporter")
        gateway.subscriptions["sub_1"] = subscription(status="canceled")
        _post_event(app, _event("customer.subscription.deleted",
                                {"id": "sub_1"}))
        user = _db.session.get(User, u.id)
        assert user.stripe_subscription_id == "sub_2"
        assert user.subscription_status == "active"

    def test_a_subscription_of_no_account(self, app, gateway):
        gateway.subscriptions["sub_9"] = subscription("sub_9", "cus_9")
        r = _post_event(app, _event("customer.subscription.updated",
                                    {"id": "sub_9"}))
        assert r.status_code == 200
        assert r.get_json()["status"] == "no_account"

    def test_the_ceiling_unblocks_a_new_subscriber(self, app, gateway,
                                                   monkeypatch):
        # Blocked under the 20 USD default; a Standard subscription's 50
        # USD ceiling lifts the block when it starts.
        from backend.utils import spend
        monkeypatch.setattr(spend, "get_user_month_spend_microdollars",
                            lambda *_a, **_k: 25_000_000)
        u = _user(stripe_customer_id="cus_1",
                  spend_blocked_month=spend.current_month())
        gateway.subscriptions["sub_1"] = subscription()
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_1"}))
        assert _db.session.get(User, u.id).spend_blocked_month is None


# ── Refunds, payments that fail, the trial ceiling, disputes ─────────────

def _cost(user_id, usd, at):
    """A cost row written with a Core insert, so no cap check is queued."""
    from backend.models import APICostLog
    _db.session.execute(APICostLog.__table__.insert().values(
        user_id=user_id, model_id="m", request_type="chat",
        cost_microdollars=int(usd * 1_000_000), created_at=at))
    _db.session.commit()


class TestDuplicateRefunds:
    def _two(self, gateway):
        gateway.subscriptions["sub_old"] = subscription(
            "sub_old", created=NOW - 60)
        gateway.subscriptions["sub_new"] = subscription(
            "sub_new", created=NOW, latest_invoice="in_new")
        gateway.invoices["in_new"] = {
            "id": "in_new", "amount_paid": 3000,
            "payments": {"data": [{"payment": {
                "type": "payment_intent", "payment_intent": "pi_new"}}]}}

    def test_a_failed_refund_cancels_nothing_and_the_retry_finishes(
            self, app, gateway):
        u = _user(stripe_customer_id="cus_1")
        self._two(gateway)
        _completed(app, u, "sub_old", "cs_x", "evt_1")
        gateway.fail_refunds = 1
        ev = _event("customer.subscription.created", {"id": "sub_new"},
                    "evt_2")
        assert _post_event(app, ev).status_code == 503
        # Not cancelled without its refund; the event is sent again.
        assert gateway.subscriptions["sub_new"]["status"] == "active"
        assert _db.session.get(StripeEvent, "evt_2") is None
        assert _post_event(app, ev).status_code == 200
        assert gateway.subscriptions["sub_new"]["status"] == "canceled"
        assert len(gateway.refunds["pi_new"]) == 1
        assert gateway.subscriptions["sub_new"]["metadata"][
            "loore_duplicate"] == "1"

    def test_a_cancelled_duplicate_without_its_refund_gets_it(self, app,
                                                              gateway):
        u = _user(stripe_customer_id="cus_1")
        self._two(gateway)
        _completed(app, u, "sub_old", "cs_x", "evt_1")
        # Cancelled as Loore's duplicate, refund missing (any cause).
        gateway.subscriptions["sub_new"].update(
            status="canceled", metadata={"loore_duplicate": "1"})
        for n in (2, 3):
            _post_event(app, _event("customer.subscription.updated",
                                    {"id": "sub_new"}, f"evt_{n}"))
        assert len(gateway.refunds["pi_new"]) == 1
        user = _db.session.get(User, u.id)
        assert user.stripe_subscription_id == "sub_old"

    def test_an_old_subscription_that_ended_is_never_refunded(self, app,
                                                              gateway):
        u = _user(stripe_customer_id="cus_1")
        self._two(gateway)
        _completed(app, u, "sub_old", "cs_x", "evt_1")
        gateway.subscriptions["sub_new"]["status"] = "canceled"
        _post_event(app, _event("customer.subscription.deleted",
                                {"id": "sub_new"}, "evt_2"))
        assert "create_refund" not in gateway.names()


class TestPastDue:
    """Peter, 2026-11-12: a failed payment gives "zero days": lapsed at
    once for an account that paid at least once, "none" for one that
    never paid; a retry that succeeds makes it active again."""

    def test_a_failed_renewal_lapses_at_once_and_a_retry_restores(
            self, app, gateway):
        from backend.utils.subscriptions import (
            access_state, subscription_required_message)
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(status="active")
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_1"}, "evt_1"))
        assert access_state(_db.session.get(User, u.id)) == "active"
        gateway.subscriptions["sub_1"]["status"] = "past_due"
        _post_event(app, _event("invoice.payment_failed", {
            "id": "in_2", "subscription": "sub_1"}, "evt_2"))
        user = _db.session.get(User, u.id)
        assert user.subscription_ever_paid
        assert access_state(user) == "lapsed"
        assert not user.on_paid_plan
        assert u.id not in {x.id for x in User.profile_eligible_query()}
        assert "last payment" in subscription_required_message(user)
        # The retry succeeds: active again.
        gateway.subscriptions["sub_1"]["status"] = "active"
        _post_event(app, _event("invoice.paid", {
            "id": "in_2", "subscription": "sub_1", "amount_paid": 3000},
            "evt_3"))
        user = _db.session.get(User, u.id)
        assert access_state(user) == "active" and user.on_paid_plan

    def test_a_trial_whose_first_payment_fails_is_not_let_in(self, app,
                                                             gateway):
        from backend.utils.subscriptions import access_state
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(
            status="trialing", trial=True)
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_1"}, "evt_1"))
        assert access_state(_db.session.get(User, u.id)) == "active"
        gateway.subscriptions["sub_1"]["status"] = "past_due"
        _post_event(app, _event("invoice.payment_failed", {
            "id": "in_1", "subscription": "sub_1"}, "evt_2"))
        user = _db.session.get(User, u.id)
        assert user.subscription_status == "past_due"
        assert access_state(user) == "none"
        # Stripe gives up after its retries: still "none".
        gateway.subscriptions["sub_1"].update(
            status="canceled",
            cancellation_details={"reason": "payment_failed"})
        _post_event(app, _event("customer.subscription.deleted",
                                {"id": "sub_1"}, "evt_3"))
        assert access_state(_db.session.get(User, u.id)) == "none"

    def test_a_trial_its_owner_cancelled_lapses(self, app, gateway):
        from backend.utils.subscriptions import access_state
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(
            status="canceled", trial=True,
            ) | {"cancellation_details": {"reason": "cancellation_requested"}}
        _post_event(app, _event("customer.subscription.deleted",
                                {"id": "sub_1"}))
        user = _db.session.get(User, u.id)
        assert user.subscription_end_reason == "cancellation_requested"
        assert access_state(user) == "lapsed"

    def test_a_paid_invoice_event_marks_the_account_paid(self, app,
                                                         gateway):
        u = _user(stripe_customer_id="cus_1")
        gateway.subscriptions["sub_1"] = subscription(status="past_due")
        _post_event(app, _event("invoice.paid", {
            "id": "in_1", "subscription": "sub_1", "amount_paid": 3000}))
        assert _db.session.get(User, u.id).subscription_ever_paid

    def test_paid_once_counts_across_subscriptions(self, app, gateway):
        from backend.utils.subscriptions import access_state
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="ended", subscription_level="standard",
                  subscription_ever_paid=True)
        gateway.subscriptions["sub_1"] = subscription(status="canceled")
        gateway.subscriptions["sub_2"] = subscription(
            "sub_2", status="past_due")
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_2"}))
        assert access_state(_db.session.get(User, u.id)) == "lapsed"


class TestAccessStates:
    """The three states while payments are on, and accounts set by hand."""

    def test_new_accounts_start_in_none(self, app):
        from backend.utils.subscriptions import access_state
        assert access_state(_user()) == "none"

    @pytest.mark.parametrize("kw", [{"plan": "alpha"}, {"plan": "pro"},
                                    {"is_admin": True}])
    def test_set_by_hand_is_untouched(self, app, kw):
        from backend.utils.subscriptions import access_state
        u = _user(**kw)
        assert access_state(u) == "full"
        user = _client(app, u).get("/api/dashboard/?profile=0").get_json()[
            "user"]
        assert user["subscription"]["state"] == "set_by_hand"

    def test_payments_off_everyone_is_full(self, app_off):
        from backend.utils.subscriptions import access_state
        assert access_state(_user()) == "full"
        assert access_state(_user("e", subscription_status="ended",
                                  subscription_level="standard")) == "full"

    def test_the_dashboard_gives_no_profile_in_none(self, app):
        u = _user()
        data = _client(app, u).get("/api/dashboard/").get_json()
        assert "latest_profile" not in data
        assert data["user"]["subscription"]["state"] == "none"

    def test_a_lapsed_account_keeps_its_profile(self, app):
        u = _user(subscription_status="ended", subscription_level="standard",
                  subscription_ever_paid=True)
        data = _client(app, u).get("/api/dashboard/").get_json()
        assert "latest_profile" in data
        assert data["user"]["subscription"]["state"] == "lapsed"


class TestTrialCeiling:
    def _enforce(self, app, user_id, now=None):
        from backend.utils.spend import enforce_user_spend_cap
        return enforce_user_spend_cap(user_id, app.config, now=now,
                                      send_email=lambda **_k: None)

    def test_the_trial_has_its_own_ceiling_across_a_month(self, app):
        from backend.utils.spend import spend_cap_payload, user_is_capped
        start = datetime(2026, 11, 25)
        u = _user(subscription_status="trialing",
                  subscription_level="supporter",
                  subscription_trial_start=start,
                  subscription_trial_end=datetime(2026, 12, 9))
        _cost(u.id, 20, datetime(2026, 11, 28))
        r = self._enforce(app, u.id, now=datetime(2026, 11, 28, 12))
        assert r.get("status") != "trial_capped"
        assert not user_is_capped(_db.session.get(User, u.id))
        # December: 10 more makes 30 over the whole trial (the default),
        # whatever the level's monthly ceiling (100 for Supporter).
        _cost(u.id, 10, datetime(2026, 12, 2))
        r = self._enforce(app, u.id, now=datetime(2026, 12, 2, 12))
        assert r["status"] == "trial_capped"
        user = _db.session.get(User, u.id)
        assert user_is_capped(user, now=datetime(2026, 12, 3))
        assert user_is_capped(user, now=datetime(2027, 1, 1))  # no reset
        payload = spend_cap_payload(user)
        assert payload["lifts_on"] == "2026-12-09"
        assert "trial" in payload["message"]
        assert "free alpha" not in payload["message"]

    def test_the_ceiling_ends_with_the_trial(self, app, gateway):
        from backend.utils.spend import user_is_capped
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="trialing",
                  subscription_level="standard",
                  subscription_trial_capped=True)
        assert user_is_capped(u)
        gateway.subscriptions["sub_1"] = subscription(status="active")
        _post_event(app, _event("customer.subscription.updated",
                                {"id": "sub_1"}))
        user = _db.session.get(User, u.id)
        assert not user.subscription_trial_capped
        assert not user_is_capped(user)

    def test_trial_usage_counts_toward_the_first_paid_month(self, app):
        # The level's ceiling is per calendar month: once the first payment
        # went through, the trial's usage in that month counts toward it.
        from backend.utils.spend import user_is_capped
        u = _user(subscription_status="trialing",
                  subscription_level="standard",
                  subscription_trial_start=datetime(2026, 11, 1),
                  subscription_trial_end=datetime(2026, 11, 15))
        _cost(u.id, 29, datetime(2026, 11, 10))
        self._enforce(app, u.id, now=datetime(2026, 11, 10, 12))
        assert not user_is_capped(_db.session.get(User, u.id),
                                  now=datetime(2026, 11, 10, 12))
        user = _db.session.get(User, u.id)
        user.subscription_status = "active"
        user.subscription_ever_paid = True
        _db.session.commit()
        _cost(u.id, 20, datetime(2026, 11, 20))
        r = self._enforce(app, u.id, now=datetime(2026, 11, 20, 12))
        assert r.get("status") != "trial_capped"
        assert not user_is_capped(_db.session.get(User, u.id),
                                  now=datetime(2026, 11, 20, 12))
        # 29 + 20 + 1 reaches Standard's 50 for November.
        _cost(u.id, 1, datetime(2026, 11, 21))
        self._enforce(app, u.id, now=datetime(2026, 11, 21, 12))
        assert user_is_capped(_db.session.get(User, u.id),
                              now=datetime(2026, 11, 21, 12))

    def test_an_admins_limit_replaces_the_trial_ceiling(self, app):
        from backend.utils.spend import user_is_capped
        u = _user(subscription_status="trialing",
                  subscription_level="standard",
                  subscription_trial_start=datetime(2026, 11, 1),
                  monthly_spend_limit_usd=200.0)
        _cost(u.id, 30, datetime(2026, 11, 2))
        self._enforce(app, u.id, now=datetime(2026, 11, 3))
        assert not user_is_capped(_db.session.get(User, u.id),
                                  now=datetime(2026, 11, 3))

    def test_accounts_set_by_hand_have_no_trial_ceiling(self, app):
        from backend.utils.spend import trial_limit_usd
        u = _user(plan="alpha", subscription_status="trialing",
                  subscription_level="standard")
        assert trial_limit_usd(u, app.config) == 0


class TestWithdrawn:
    def _active(self, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="active",
                  subscription_level="standard", subscription_ever_paid=True)
        gateway.subscriptions["sub_1"] = subscription(
            status="active", latest_invoice="in_1")
        gateway.invoices["in_1"] = {
            "id": "in_1", "amount_paid": 3000,
            "payments": {"data": [{"payment": {"payment_intent": "pi_1"}}]}}
        return u

    def test_a_dispute_ends_access_and_the_subscription(self, app, gateway):
        u = self._active(gateway)
        gateway.charges["ch_1"] = {"id": "ch_1", "customer": "cus_1",
                                   "payment_intent": "pi_1"}
        r = _post_event(app, _event("charge.dispute.created", {
            "id": "dp_1", "charge": "ch_1"}))
        assert r.get_json()["status"] == "applied"
        user = _db.session.get(User, u.id)
        assert user.subscription_revoked == "dispute"
        assert not user.on_paid_plan
        assert gateway.subscriptions["sub_1"]["status"] == "canceled"
        admin = _client(app, _user("root", is_admin=True))
        row = admin.get("/api/admin/subscriptions").get_json()[
            "subscriptions"][0]
        assert row["revoked"] == "dispute" and row["access"] is False

    def test_a_full_refund_of_the_latest_payment_ends_access(self, app,
                                                             gateway):
        u = self._active(gateway)
        _post_event(app, _event("charge.refunded", {
            "id": "ch_1", "customer": "cus_1", "payment_intent": "pi_1",
            "amount": 3000, "amount_refunded": 3000, "refunded": True}))
        user = _db.session.get(User, u.id)
        assert user.subscription_revoked == "refund"
        assert not user.on_paid_plan

    @pytest.mark.parametrize("charge", [
        {"payment_intent": "pi_1", "amount": 3000, "amount_refunded": 1000,
         "refunded": False},
        {"payment_intent": "pi_other", "amount": 3000,
         "amount_refunded": 3000, "refunded": True}])
    def test_a_partial_or_other_refund_changes_nothing(self, app, gateway,
                                                       charge):
        u = self._active(gateway)
        _post_event(app, _event("charge.refunded", {
            "id": "ch_1", "customer": "cus_1", **charge}))
        user = _db.session.get(User, u.id)
        assert user.subscription_revoked is None and user.on_paid_plan
        assert "cancel_subscription" not in gateway.names()

    def test_a_new_subscription_starts_clean(self, app, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="ended", subscription_level="standard",
                  subscription_revoked="dispute")
        gateway.subscriptions["sub_1"] = subscription(status="canceled")
        gateway.subscriptions["sub_2"] = subscription("sub_2")
        _post_event(app, _event("customer.subscription.created",
                                {"id": "sub_2"}))
        user = _db.session.get(User, u.id)
        assert user.subscription_revoked is None and user.on_paid_plan


class TestKeysAndModes:
    @pytest.mark.parametrize("env,expected", [
        ("development", "sk_test_sandbox"), ("testing", "sk_test_sandbox"),
        ("staging", None), ("production", None), (None, None)])
    def test_the_sandbox_key_is_a_fallback_in_development_only(
            self, env, expected):
        from backend.config import stripe_key_from_env
        environ = {"STRIPE_SANDBOX_PRIVKEY": "sk_test_sandbox"}
        if env:
            environ["FLASK_ENV"] = env
        assert stripe_key_from_env(
            "STRIPE_SECRET_KEY", "STRIPE_SANDBOX_PRIVKEY", environ) == expected
        environ["STRIPE_SECRET_KEY"] = "sk_live_x"
        assert stripe_key_from_env(
            "STRIPE_SECRET_KEY", "STRIPE_SANDBOX_PRIVKEY",
            environ) == "sk_live_x"

    @pytest.mark.parametrize("env,key,stays_on", [
        ("production", "sk_test_x", False), ("production", "rk_test_x", False),
        ("production", None, False), ("production", "sk_live_x", True),
        ("production", "rk_live_x", True), ("staging", "sk_test_x", True),
        ("development", "sk_test_x", True)])
    def test_production_needs_a_live_key(self, env, key, stays_on):
        from backend.utils.subscriptions import check_payments_config
        config = {"PAYMENTS_ENABLED": True, "DEPLOY_ENV": env,
                  "STRIPE_SECRET_KEY": key}
        reason = check_payments_config(config)
        assert config["PAYMENTS_ENABLED"] is stays_on
        assert (reason is None) is stays_on

    @pytest.mark.parametrize("env,livemode,status", [
        ("production", False, 400), ("production", None, 400),
        ("production", True, 200), ("staging", False, 200),
        ("testing", False, 200)])
    def test_production_takes_live_events_only(self, app, gateway, env,
                                               livemode, status):
        app.config["DEPLOY_ENV"] = env
        ev = _event("charge.refund.updated", {"id": "re_1"})
        if livemode is not None:
            ev["livemode"] = livemode
        assert _post_event(app, ev).status_code == status


class TestPurgeAndInputs:
    def test_a_subscription_stripe_already_ended(self, app, gateway):
        import stripe
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="active",
                  subscription_level="standard")
        gateway.subscriptions["sub_1"] = subscription(status="canceled")
        gateway.cancel_error = stripe.InvalidRequestError(
            "No such subscription", "id", code="resource_missing",
            http_status=404)
        assert stripe_billing.cancel_for_purge(u) is True
        assert u.subscription_status == "ended"

    def test_a_subscription_stripe_does_not_know(self, app, gateway):
        import stripe
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_x",
                  subscription_status="trialing",
                  subscription_level="standard")
        gateway.cancel_error = stripe.InvalidRequestError(
            "No such subscription", "id", code="resource_missing",
            http_status=404)

        def missing(_sub_id):
            raise stripe.InvalidRequestError(
                "No such subscription", "id", code="resource_missing",
                http_status=404)
        gateway.retrieve_subscription = missing
        assert stripe_billing.cancel_for_purge(u) is True
        assert u.subscription_status == "ended"

    def test_stripe_down_still_stops_the_purge(self, app, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="active",
                  subscription_level="standard")
        gateway.subscriptions["sub_1"] = subscription(status="active")
        gateway.cancel_error = RuntimeError("stripe down")
        with pytest.raises(RuntimeError):
            stripe_billing.cancel_for_purge(u)

    def test_the_purge_deletes_the_customer(self, app, gateway):
        import stripe
        u = _user(stripe_customer_id="cus_1")
        assert stripe_billing.delete_customer_for_purge(u) is True
        assert ("delete_customer", "cus_1") in gateway.calls
        assert u.stripe_customer_id is None
        u2 = _user("b", stripe_customer_id="cus_gone")

        def gone(_cid):
            raise stripe.InvalidRequestError(
                "No such customer", "id", code="resource_missing",
                http_status=404)
        gateway.delete_customer = gone
        assert stripe_billing.delete_customer_for_purge(u2) is True
        gateway.fail = True
        del gateway.delete_customer
        u3 = _user("c", stripe_customer_id="cus_3")
        with pytest.raises(RuntimeError):
            stripe_billing.delete_customer_for_purge(u3)

    @pytest.mark.parametrize("body", [
        {"code": 5}, {"code": ["a"]}, {"code": {"x": 1}}])
    def test_a_code_that_is_not_text_is_a_400(self, app, gateway, body):
        r = _client(app, _user()).post("/api/billing/checkout", json=body)
        assert r.status_code == 400

    def test_a_session_id_that_is_not_text_is_a_400(self, app, gateway):
        r = _client(app, _user()).post("/api/billing/sync",
                                       json={"session_id": 7})
        assert r.status_code == 400

    def test_a_body_that_is_not_an_object(self, app, gateway):
        r = _client(app, _user()).post("/api/billing/checkout",
                                       json=["standard"])
        assert r.status_code == 400

    def test_an_unapproved_account_cannot_check_out(self, app, gateway):
        u = User(username="waiting", approved=False)
        _db.session.add(u)
        _db.session.commit()
        c = _client(app, u)
        assert c.post("/api/billing/checkout",
                      json={"level": "standard"}).status_code == 403
        assert "create_checkout_session" not in gateway.names()


# ── The catalog ──────────────────────────────────────────────────────────

class TestCatalog:
    def test_sync_is_idempotent(self, app, gateway):
        gateway.prices.clear()
        first = stripe_billing.sync_catalog(gateway, app.config)
        for level, (pid, _name) in stripe_billing.PRODUCTS.items():
            assert gateway.products[pid]["tax_code"] == "txcd_10105001"
        amounts = {p["lookup_key"]: p["unit_amount"] for p in gateway.prices}
        assert amounts == {"loore_standard_monthly": 3000,
                           "loore_supporter_monthly": 9900,
                           "loore_discounted_monthly": 1200}
        # Peter, 2026-11-12: "tax should be on top of our pricing".
        assert all(p["tax_behavior"] == "exclusive" for p in gateway.prices)
        assert {p["lookup_key"]: p["product"] for p in gateway.prices} == {
            "loore_standard_monthly": "loore_standard",
            "loore_supporter_monthly": "loore_supporter",
            "loore_discounted_monthly": "loore_reduced"}
        assert len(gateway.portal_configs) == 1
        conf = gateway.portal_configs[0]
        assert conf["features"]["subscription_cancel"]["mode"] == \
            "at_period_end"
        # Standard <-> Supporter in the portal; the reduced price never.
        switch = conf["features"]["subscription_update"]
        assert switch["enabled"] is True
        assert switch["products"] == [
            {"product": "loore_standard",
             "prices": [first["prices"]["standard"]]},
            {"product": "loore_supporter",
             "prices": [first["prices"]["supporter"]]}]
        # An upgrade is charged at the switch; a downgrade waits for the
        # end of the period, nothing refunded.
        assert switch["proration_behavior"] == "always_invoice"
        assert switch["schedule_at_period_end"] == {
            "conditions": [{"type": "decreasing_item_amount"}]}
        assert switch["trial_update_behavior"] == "continue_trial"
        assert len(first["changes"]) == 7   # 3 products, 3 prices, portal
        second = stripe_billing.sync_catalog(gateway, app.config)
        assert second["changes"] == []
        assert second["prices"] == first["prices"]

    def test_the_first_versions_product_is_archived(self, app, gateway):
        gateway.prices.clear()
        gateway.products["loore_subscription"] = {
            "id": "loore_subscription", "active": True}
        old = gateway.create_price({
            "product": "loore_subscription", "unit_amount": 3000,
            "currency": "usd", "tax_behavior": "inclusive",
            "lookup_key": "loore_standard_monthly",
            "metadata": {"loore_level": "standard"}})
        result = stripe_billing.sync_catalog(gateway, app.config)
        assert gateway.products["loore_subscription"]["active"] is False
        assert old["active"] is False and old["lookup_key"] is None
        assert result["prices"]["standard"] != old["id"]
        # A subscription still on the old price keeps its level.
        assert stripe_billing.level_for_price(old) == "standard"

    def test_a_changed_amount_makes_a_new_price(self, app, gateway):
        gateway.prices.clear()
        first = stripe_billing.sync_catalog(gateway, app.config)
        app.config["SUBSCRIPTION_PRICE_CENTS_STANDARD"] = 3500
        second = stripe_billing.sync_catalog(gateway, app.config)
        assert second["prices"]["standard"] != first["prices"]["standard"]
        assert second["prices"]["supporter"] == first["prices"]["supporter"]
        old = [p for p in gateway.prices
               if p["id"] == first["prices"]["standard"]][0]
        assert old["active"] is False and old["lookup_key"] is None
        assert stripe_billing.price_id_for_level(gateway, "standard") == \
            second["prices"]["standard"]

    def test_checkout_without_a_catalog_is_a_503(self, app, gateway):
        gateway.prices.clear()
        r = _client(app, _user()).post("/api/billing/checkout",
                                       json={"level": "standard"})
        assert r.status_code == 503


# ── Admin ────────────────────────────────────────────────────────────────

class TestAdmin:
    def test_codes(self, app):
        admin = _client(app, _user("root", is_admin=True))
        r = admin.post("/api/admin/discount-codes",
                       json={"max_uses": 3, "expires_at": "2030-01-31",
                             "note": "for a friend"})
        assert r.status_code == 201
        code = r.get_json()["code"]
        assert len(code["code"]) == 8 and code["uses"] == 0
        assert code["expires_at"].startswith("2030-02-01")
        assert code["expires_on"] == "2030-01-31"
        r = admin.post("/api/admin/discount-codes", json={"code": "my code"})
        assert r.get_json()["code"]["code"] == "MYCODE"
        assert admin.post("/api/admin/discount-codes",
                          json={"code": "mycode"}).status_code == 400
        listed = admin.get("/api/admin/discount-codes").get_json()["codes"]
        assert {c["code"] for c in listed} == {code["code"], "MYCODE"}
        r = admin.patch(f"/api/admin/discount-codes/{code['id']}",
                        json={"active": False})
        assert r.get_json()["code"]["active"] is False

    def test_codes_and_lists_are_admin_only(self, app):
        c = _client(app, _user())
        assert c.get("/api/admin/discount-codes").status_code == 403
        assert c.get("/api/admin/subscriptions").status_code == 403

    def test_subscriptions_list_has_no_payment_details(self, app):
        _user("s", stripe_customer_id="cus_1", subscription_status="active",
              subscription_level="standard")
        _user("n")
        admin = _client(app, _user("root", is_admin=True))
        rows = admin.get("/api/admin/subscriptions").get_json()[
            "subscriptions"]
        assert [r["username"] for r in rows] == ["s"]
        assert set(rows[0]) == {
            "user_id", "username", "plan", "level", "status",
            "current_period_end", "trial_end", "cancel_at_period_end",
            "trial_used", "access", "revoked"}


# ── Account deletion ─────────────────────────────────────────────────────

class TestDeletion:
    def test_a_scheduled_deletion_stops_the_renewal(self, app, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="active",
                  subscription_level="standard")
        gateway.subscriptions["sub_1"] = subscription()
        assert stripe_billing.stop_renewal_for_deletion(u) is True
        assert ("update_subscription", "sub_1",
                {"cancel_at_period_end": True}) in gateway.calls
        assert _db.session.get(User, u.id).subscription_cancel_at_period_end

    def test_a_waiting_downgrade_does_not_block_the_stop(self, app, gateway):
        # A downgrade waits for the period end as a subscription schedule;
        # Stripe refuses cancel_at_period_end while one manages the
        # subscription (seen in the sandbox), so it is released first.
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="active",
                  subscription_level="supporter")
        gateway.subscriptions["sub_1"] = {**subscription(level="supporter"),
                                          "schedule": "sub_sched_1"}
        assert stripe_billing.stop_renewal_for_deletion(u) is True
        names = [c[0] for c in gateway.calls]
        assert names.index("release_subscription_schedule") < names.index(
            "update_subscription")
        assert gateway.subscriptions["sub_1"]["schedule"] is None
        assert _db.session.get(User, u.id).subscription_cancel_at_period_end

    def test_nothing_to_stop(self, app, gateway):
        assert stripe_billing.stop_renewal_for_deletion(_user()) is False
        assert gateway.calls == []

    def test_the_purge_cancels_what_still_runs(self, app, gateway):
        u = _user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  subscription_status="active",
                  subscription_level="standard")
        gateway.subscriptions["sub_1"] = subscription()
        assert stripe_billing.cancel_for_purge(u) is True
        assert u.subscription_status == "ended"
        gateway.fail = True
        u2 = _user("b", stripe_customer_id="cus_2",
                   stripe_subscription_id="sub_2",
                   subscription_status="trialing",
                   subscription_level="standard")
        with pytest.raises(RuntimeError):
            stripe_billing.cancel_for_purge(u2)
