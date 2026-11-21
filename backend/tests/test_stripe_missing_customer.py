"""A Stripe customer that Stripe no longer has (#718).

The customer id stored on an account stops working when the customer is
deleted in the Dashboard (or by a test cleanup): Stripe answers "No such
customer" (code resource_missing). Checkout, the billing portal and a
referral's credit then give the person a plain message, one ERROR names
the account, and nothing else changes; no customer is made in its place.
`flask stripe-forget-customer --user ID` clears the stored id (a dry run
unless --apply), and the next checkout makes a new customer.

The payments test app (test_payments.py), with a stub gateway that raises
the stripe library's own InvalidRequestError, made here: no call reaches
Stripe.
"""
import logging
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

import stripe  # noqa: E402

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    CheckoutSession, Referral, User, UserNotification)
from backend.tests import test_payments as tp  # noqa: E402
from backend.tests.test_payments import (  # noqa: E402
    _client, _event, _post_event, _user, subscription)
from backend.utils import stripe_billing  # noqa: E402

MESSAGE = ("Something is wrong with your billing record. Write to "
           "info@loore.org and we'll fix it.")


def no_such_customer(param="customer", status=400):
    """What the stripe library raises for a customer Stripe doesn't have:
    about the ``customer`` parameter (Checkout, the portal), or a customer
    in the request's path (its balance)."""
    return stripe.InvalidRequestError(
        "No such customer: 'cus_gone'", param, code="resource_missing",
        http_status=status)


class Gateway(tp.FakeGateway):
    """The payments stub, with customers Stripe no longer has (*gone*; of
    those, *deleted* ones read back as deleted), customer balance, reading
    a customer, subscriptions Stripe does not know, and idempotency keys
    answered again the way Stripe does (its first answer, for 24 hours)."""

    def __init__(self):
        super().__init__()
        self.gone = set()
        self.deleted = set()
        self.balance = {}
        self.retrieve_error = None
        self.subscription_error = None
        self.by_key = {}

    def create_customer(self, params, idempotency_key=None):
        if idempotency_key is not None and idempotency_key in self.by_key:
            self._call("create_customer", params, idempotency_key)
            return dict(self.by_key[idempotency_key])
        customer = super().create_customer(params, idempotency_key)
        if idempotency_key is not None:
            self.by_key[idempotency_key] = dict(customer)
        return customer

    def retrieve_subscription(self, sub_id):
        if self.subscription_error is not None:
            self._call("retrieve_subscription", sub_id)
            raise self.subscription_error
        if sub_id not in self.subscriptions:
            self._call("retrieve_subscription", sub_id)
            raise stripe.InvalidRequestError(
                f"No such subscription: '{sub_id}'", "id",
                code="resource_missing", http_status=404)
        return super().retrieve_subscription(sub_id)

    def create_checkout_session(self, params):
        if params.get("customer") in self.gone:
            self._call("create_checkout_session", params)
            raise no_such_customer()
        return super().create_checkout_session(params)

    def create_portal_session(self, params):
        if params.get("customer") in self.gone:
            self._call("create_portal_session", params)
            raise no_such_customer()
        return super().create_portal_session(params)

    def list_balance_transactions(self, customer_id):
        self._call("list_balance_transactions", customer_id)
        if customer_id in self.gone:
            raise no_such_customer(param=None, status=404)
        return list(self.balance.get(customer_id, []))

    def create_balance_transaction(self, customer_id, params,
                                   idempotency_key=None):
        self._call("create_balance_transaction", customer_id, params,
                   idempotency_key)
        if customer_id in self.gone:
            raise no_such_customer(param=None, status=404)
        txn = {"id": self._id("cbtxn"), "customer": customer_id,
               "amount": params["amount"], "currency": params["currency"],
               "metadata": dict(params.get("metadata") or {})}
        self.balance.setdefault(customer_id, []).append(txn)
        return txn

    def retrieve_customer(self, customer_id):
        self._call("retrieve_customer", customer_id)
        if self.retrieve_error is not None:
            raise self.retrieve_error
        if customer_id in self.deleted:
            return {"id": customer_id, "object": "customer", "deleted": True}
        if customer_id in self.gone:
            return None
        return {"id": customer_id, "object": "customer"}


@pytest.fixture
def gateway(monkeypatch):
    gw = Gateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    stripe_billing._price_cache.clear()
    for level in ("standard", "supporter", "discounted"):
        gw.prices.append({
            "id": f"price_{level}", "active": True,
            "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]})
    return gw


@pytest.fixture
def app(gateway):
    app, ctx = tp._app_fixture(True)
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


def _columns(user_id):
    """Every column of the account, read from the database."""
    _db.session.expire_all()
    user = _db.session.get(User, user_id)
    return {c.name: getattr(user, c.name) for c in User.__table__.columns}


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _the_one_error(caplog, user_id):
    """Exactly one ERROR, naming the account and nothing about the person
    or the customer."""
    errors = _errors(caplog)
    assert len(errors) == 1, [r.getMessage() for r in errors]
    text = errors[0].getMessage()
    assert f"user {user_id} " in text
    assert "cus_" not in text and "@" not in text
    return text


def _gone_user(gateway, username="alice", **kw):
    gateway.gone.add("cus_gone")
    gateway.deleted.add("cus_gone")
    return _user(username, stripe_customer_id="cus_gone", **kw)


# ── Recognising Stripe's answer ──────────────────────────────────────────

class TestRecognised:
    def test_no_such_customer(self):
        assert stripe_billing.customer_missing(no_such_customer())
        assert stripe_billing.customer_missing(
            no_such_customer(param=None, status=404))

    def test_other_errors_are_not_it(self):
        price = stripe.InvalidRequestError(
            "No such price: 'price_x'", "line_items[0][price]",
            code="resource_missing", http_status=400)
        card = stripe.InvalidRequestError(
            "No such customer: 'cus_x'", "customer", code="parameter_invalid",
            http_status=400)
        for exc in (price, card, RuntimeError("No such customer"),
                    stripe.APIConnectionError("down")):
            assert not stripe_billing.customer_missing(exc), exc

    def test_the_gateway_reads_a_customer(self):
        """StripeGateway.retrieve_customer: None for a customer Stripe
        doesn't have, the deleted marker as Stripe gives it."""
        from unittest.mock import MagicMock
        client = MagicMock()
        gw = stripe_billing.StripeGateway(client)
        client.v1.customers.retrieve.side_effect = no_such_customer(
            param="id", status=404)
        assert gw.retrieve_customer("cus_x") is None
        client.v1.customers.retrieve.side_effect = None
        client.v1.customers.retrieve.return_value = {"id": "cus_x",
                                                     "deleted": True}
        assert gw.retrieve_customer("cus_x")["deleted"] is True
        client.v1.customers.retrieve.side_effect = \
            stripe.APIConnectionError("down")
        with pytest.raises(stripe.APIConnectionError):
            gw.retrieve_customer("cus_x")


# ── Checkout, portal, referral credit ────────────────────────────────────

class TestTheCallsWithTheStoredId:
    def test_checkout(self, app, gateway, caplog):
        u = _gone_user(gateway)
        before = _columns(u.id)
        caplog.set_level(logging.INFO)
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "standard"})
        assert r.status_code == 409, r.get_data(as_text=True)
        assert r.get_json() == {"error": "billing_record", "message": MESSAGE}
        text = _the_one_error(caplog, u.id)
        assert "checkout" in text
        assert "flask stripe-forget-customer --user" in text
        assert _columns(u.id) == before
        # Nothing made in its place.
        assert "create_customer" not in gateway.names()
        assert CheckoutSession.query.count() == 0

    def test_checkout_with_a_code(self, app, gateway, caplog):
        from backend.utils.discount_codes import create_code
        create_code(code="LOWER-1")
        u = _gone_user(gateway)
        before = _columns(u.id)
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"code": "LOWER-1"})
        assert r.status_code == 409, r.get_data(as_text=True)
        assert r.get_json()["message"] == MESSAGE
        _the_one_error(caplog, u.id)
        assert _columns(u.id) == before

    def test_the_alpha_free_month_checkout(self, app, gateway, caplog):
        """An alpha tester subscribing during the free month (the
        discounted level without a code, the month as the trial)."""
        u = _gone_user(gateway, plan="free")
        u.alpha_free_granted_at = datetime.utcnow() - timedelta(days=3)
        u.alpha_free_start = datetime.utcnow() - timedelta(days=2)
        u.alpha_free_until = u.alpha_free_start + timedelta(days=30)
        _db.session.commit()
        before = _columns(u.id)
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "discounted"})
        assert r.status_code == 409, r.get_data(as_text=True)
        params = [c for c in gateway.calls
                  if c[0] == "create_checkout_session"][0][1]
        assert "trial_end" in params["subscription_data"]
        assert r.get_json()["message"] == MESSAGE
        _the_one_error(caplog, u.id)
        assert _columns(u.id) == before

    def test_portal(self, app, gateway, caplog):
        u = _gone_user(gateway, stripe_subscription_id="sub_old",
                       subscription_status="ended",
                       subscription_level="standard")
        before = _columns(u.id)
        r = _client(app, u).post("/api/billing/portal")
        assert r.status_code == 409, r.get_data(as_text=True)
        assert r.get_json() == {"error": "billing_record", "message": MESSAGE}
        assert "billing portal" in _the_one_error(caplog, u.id)
        assert _columns(u.id) == before
        assert "create_customer" not in gateway.names()

    def test_the_contact_address_is_the_setting(self, app, gateway):
        app.config["SUBSCRIPTION_CONTACT_EMAIL"] = "help@example.test"
        u = _gone_user(gateway)
        r = _client(app, u).post("/api/billing/portal")
        assert r.get_json()["message"] == (
            "Something is wrong with your billing record. Write to "
            "help@example.test and we'll fix it.")

    def test_referral_credit(self, app, gateway, caplog):
        """The inviter's customer is gone when the friend first pays: the
        friend's credit is made, the inviter's side is "failed" (given by
        hand later), the inviter is told plainly, one ERROR, and the
        event applies."""
        gateway.gone.add("cus_gone")
        inviter = _user("ines", stripe_customer_id="cus_gone",
                        stripe_subscription_id="sub_i",
                        subscription_status="ended",
                        subscription_level="standard")
        friend = _user("fred", stripe_customer_id="cus_f",
                       stripe_subscription_id="sub_f",
                       subscription_status="trialing",
                       subscription_level="standard")
        _db.session.add(Referral(inviter_id=inviter.id, friend_id=friend.id))
        _db.session.commit()
        before = _columns(inviter.id)
        gateway.subscriptions["sub_f"] = subscription("sub_f", "cus_f")
        invoice = {"id": "in_1", "object": "invoice", "customer": "cus_f",
                   "subscription": "sub_f", "amount_paid": 3570,
                   "total": 3570, "currency": "usd", "status": "paid"}
        r = _post_event(app, _event("invoice.paid", invoice))
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["status"] == "applied"
        _db.session.expire_all()
        ref = Referral.query.one()
        assert (ref.friend_reward, ref.inviter_reward) == ("granted",
                                                           "failed")
        assert "referral credit" in _the_one_error(caplog, inviter.id)
        assert _columns(inviter.id) == before
        assert "create_customer" not in gateway.names()
        assert [t["customer"] for txns in gateway.balance.values()
                for t in txns] == ["cus_f"]
        note = UserNotification.query.filter_by(user_id=inviter.id).one()
        assert note.title == "A free week, with thanks"
        assert note.body.endswith(MESSAGE)
        assert "could not be added" in note.body
        assert "fred" not in note.body
        # The friend's own notice is the usual one.
        mine = UserNotification.query.filter_by(user_id=friend.id).one()
        assert "taken off your next payment" in mine.body

    def test_a_known_customer_is_unchanged(self, app, gateway, caplog):
        """Control: a customer Stripe has: checkout and portal give their
        address, no ERROR, nothing is read about the customer."""
        u = _user(stripe_customer_id="cus_ok")
        c = _client(app, u)
        r = c.post("/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 200
        assert r.get_json()["url"].startswith("https://checkout.example/")
        r = c.post("/api/billing/portal")
        assert r.status_code == 200
        assert r.get_json()["url"] == "https://billing.example/session"
        assert _errors(caplog) == []
        assert "create_customer" not in gateway.names()
        assert "retrieve_customer" not in gateway.names()

    def test_stripe_down_is_still_a_503(self, app, gateway, caplog):
        """Control: any other failure keeps its own answer."""
        u = _user(stripe_customer_id="cus_ok")
        gateway.fail = True
        r = _client(app, u).post("/api/billing/portal")
        assert r.status_code == 503
        assert r.get_json()["error"] == "billing_unavailable"


# ── The admin command ────────────────────────────────────────────────────

def _run(app, *args):
    from backend.billing_cli import stripe_forget_customer_command
    return app.test_cli_runner().invoke(stripe_forget_customer_command,
                                        list(args))


class TestForgetCommand:
    def test_the_dry_run_names_the_account_and_the_id(self, app, gateway):
        u = _gone_user(gateway)
        before = _columns(u.id)
        r = _run(app, "--user", str(u.id))
        assert r.exit_code == 0, r.output
        assert f"Account {u.id} (alice)" in r.output
        assert "stored Stripe customer: cus_gone" in r.output
        assert "at Stripe (test key): deleted" in r.output
        assert "subscription recorded: none" in r.output
        assert "Nothing changed. Run again with --apply." in r.output
        assert _columns(u.id) == before

    def test_apply_clears_it_and_the_next_checkout_makes_a_customer(
            self, app, gateway):
        u = _gone_user(gateway, stripe_subscription_id="sub_old",
                       subscription_status="ended",
                       subscription_level="standard")
        before = _columns(u.id)
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code == 0, r.output
        assert "Done" in r.output
        after = _columns(u.id)
        assert after["stripe_customer_id"] is None
        assert after["stripe_customer_cleared_count"] == 1
        # Only the customer id and the count of clears changed.
        assert {k for k in after if after[k] != before[k]} == {
            "stripe_customer_id", "stripe_customer_cleared_count"}
        # Nothing was asked of Stripe but the customer itself.
        assert "delete_customer" not in gateway.names()
        assert "create_customer" not in gateway.names()
        assert "retrieve_subscription" not in gateway.names()
        r = _client(app, _db.session.get(User, u.id)).post(
            "/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert gateway.names().count("create_customer") == 1
        new = _db.session.get(User, u.id).stripe_customer_id
        assert new and new != "cus_gone"
        params = [c for c in gateway.calls
                  if c[0] == "create_checkout_session"][-1][1]
        assert params["customer"] == new

    def test_within_a_day_the_next_checkout_gets_a_new_customer(
            self, app, gateway, caplog):
        """The customer was made by a checkout and deleted the same day:
        Stripe would answer the same create-customer key with the deleted
        customer for 24 hours (the stub does too). The key carries the
        count of clears, so the checkout after a clear makes a new one,
        and so does the one after a second clear."""
        u = _user("bob")
        c = _client(app, u)
        assert c.post("/api/billing/checkout",
                      json={"level": "standard"}).status_code == 200
        first = _columns(u.id)["stripe_customer_id"]
        gateway.gone.add(first)
        gateway.deleted.add(first)
        assert c.post("/api/billing/checkout",
                      json={"level": "standard"}).status_code == 409
        assert _run(app, "--user", str(u.id), "--apply").exit_code == 0
        caplog.clear()
        c = _client(app, _db.session.get(User, u.id))
        r = c.post("/api/billing/checkout", json={"level": "standard"})
        assert r.status_code == 200, r.get_data(as_text=True)
        second = _columns(u.id)["stripe_customer_id"]
        assert second and second != first
        keys = [x[2] for x in gateway.calls if x[0] == "create_customer"]
        assert len(set(keys)) == 2
        assert keys[-1] == stripe_billing.customer_idempotency_key(
            _db.session.get(User, u.id))
        assert keys[-1].endswith("-r1")
        assert _errors(caplog) == []
        # A second clear, the same day: a third key, a third customer.
        gateway.gone.add(second)
        gateway.deleted.add(second)
        assert _run(app, "--user", str(u.id), "--apply").exit_code == 0
        c = _client(app, _db.session.get(User, u.id))
        assert c.post("/api/billing/checkout",
                      json={"level": "standard"}).status_code == 200
        third = _columns(u.id)["stripe_customer_id"]
        assert third not in (first, second)
        keys = [x[2] for x in gateway.calls if x[0] == "create_customer"]
        assert keys[-1].endswith("-r2")

    def test_a_referral_credit_after_a_clear_makes_a_new_customer(
            self, app, gateway):
        """The referral's credit makes a customer with the same key as a
        checkout: after a clear it is a new one too."""
        from backend.utils.referrals import _customer_for
        u = _user("ines")
        first = _customer_for(gateway, u)
        _db.session.commit()
        gateway.gone.add(first)
        gateway.deleted.add(first)
        assert _run(app, "--user", str(u.id), "--apply").exit_code == 0
        u = _db.session.get(User, u.id)
        assert _customer_for(gateway, u) != first

    def test_a_customer_stripe_never_had(self, app, gateway):
        """An id Stripe has no customer for at all (not even deleted) is
        cleared the same way."""
        gateway.gone.add("cus_other")
        u = _user(stripe_customer_id="cus_other")
        r = _run(app, "--user", str(u.id))
        assert "no customer with this id" in r.output
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code == 0, r.output
        assert _columns(u.id)["stripe_customer_id"] is None

    @pytest.mark.parametrize("status", ["trialing", "active", "past_due"])
    def test_a_cancellation_that_never_arrived(self, app, gateway, status):
        """The customer was deleted, Stripe cancelled its subscription, and
        the event never reached Loore: the account still records it live.
        The command reads it from Stripe, records it as ended through the
        cancellation event's own path, then clears the id."""
        u = _gone_user(gateway, stripe_subscription_id="sub_1",
                       subscription_status=status,
                       subscription_level="supporter")
        gateway.subscriptions["sub_1"] = subscription(
            "sub_1", customer="cus_gone", status="canceled",
            level="supporter", user_id=u.id)
        before = _columns(u.id)
        r = _run(app, "--user", str(u.id))
        assert r.exit_code == 0, r.output
        assert f"subscription recorded: {status} (sub_1)" in r.output
        assert "at Stripe the subscription is canceled" in r.output
        assert "--apply would refuse" not in r.output
        assert _columns(u.id) == before
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code == 0, r.output
        assert "recorded as ended" in r.output
        after = _columns(u.id)
        assert after["subscription_status"] == "ended"
        assert after["stripe_subscription_id"] == "sub_1"
        assert after["subscription_level"] == "supporter"
        assert after["stripe_customer_id"] is None
        # Nothing was changed at Stripe.
        assert "cancel_subscription" not in gateway.names()
        assert "update_subscription" not in gateway.names()

    def test_a_subscription_stripe_does_not_know(self, app, gateway):
        """An id Stripe never had (another Stripe account or mode): no
        event will ever come, and Stripe has no such subscription. It is
        taken as ended, as an account deletion takes it."""
        gateway.gone.add("cus_other")
        period_end = datetime(2026, 12, 20, 10, 0)
        u = _user(stripe_customer_id="cus_other",
                  stripe_subscription_id="sub_other",
                  subscription_status="active", subscription_level="standard",
                  subscription_period_end=period_end)
        r = _run(app, "--user", str(u.id))
        assert "Stripe has no such subscription" in r.output
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code == 0, r.output
        after = _columns(u.id)
        assert after["subscription_status"] == "ended"
        assert after["subscription_level"] == "standard"
        assert after["subscription_period_end"] == period_end
        assert after["subscription_cancel_at_period_end"] is False
        assert after["stripe_customer_id"] is None

    @pytest.mark.parametrize("status", ["trialing", "active", "past_due"])
    def test_refuses_while_stripe_runs_the_subscription(
            self, app, gateway, status):
        u = _gone_user(gateway, stripe_subscription_id="sub_live",
                       subscription_status=status,
                       subscription_level="standard")
        gateway.subscriptions["sub_live"] = subscription(
            "sub_live", customer="cus_gone", status=status, user_id=u.id)
        before = _columns(u.id)
        r = _run(app, "--user", str(u.id))
        assert r.exit_code == 0
        assert "--apply would refuse" in r.output
        assert f"sub_live as {status}" in r.output
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code != 0
        assert "still runs" in r.output
        assert "Nothing changed." in r.output
        assert _columns(u.id) == before

    def test_refuses_when_the_subscription_cannot_be_read(
            self, app, gateway):
        u = _gone_user(gateway, stripe_subscription_id="sub_1",
                       subscription_status="active",
                       subscription_level="standard")
        gateway.subscription_error = stripe.APIConnectionError("down")
        before = _columns(u.id)
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code != 0
        assert "could not be asked about the subscription" in r.output
        assert _columns(u.id) == before

    def test_refuses_while_stripe_has_the_customer(self, app, gateway):
        u = _user(stripe_customer_id="cus_ok")
        r = _run(app, "--user", str(u.id))
        assert "the customer exists" in r.output
        assert "--apply would refuse" in r.output
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code != 0
        assert "Stripe still has this customer" in r.output
        assert _columns(u.id)["stripe_customer_id"] == "cus_ok"

    def test_refuses_when_stripe_cannot_be_asked(self, app, gateway):
        u = _gone_user(gateway)
        gateway.retrieve_error = stripe.APIConnectionError("down")
        r = _run(app, "--user", str(u.id))
        assert "could not be asked (APIConnectionError)" in r.output
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code != 0
        assert _columns(u.id)["stripe_customer_id"] == "cus_gone"

    def test_refuses_when_something_changed_meanwhile(self, app, gateway):
        """Read again under the lock: a checkout that stored another id,
        or a subscription that started, between the check and the clear
        keeps the account as it is."""
        u = _gone_user(gateway)
        check = stripe_billing.check_forget(gateway, u)
        with pytest.raises(stripe_billing.ForgetRefused):
            stripe_billing.forget_customer(gateway, u, "cus_earlier", check)
        u.subscription_status = "active"
        u.stripe_subscription_id = "sub_new"
        _db.session.commit()
        with pytest.raises(stripe_billing.ForgetRefused):
            stripe_billing.forget_customer(gateway, u, "cus_gone", check)
        after = _columns(u.id)
        assert after["stripe_customer_id"] == "cus_gone"
        assert after["subscription_status"] == "active"

    def test_nothing_to_clear(self, app, gateway):
        u = _user()
        r = _run(app, "--user", str(u.id), "--apply")
        assert r.exit_code == 0
        assert "stored Stripe customer: none" in r.output
        assert "Nothing to clear." in r.output
        assert "retrieve_customer" not in gateway.names()

    def test_an_unknown_account(self, app, gateway):
        r = _run(app, "--user", "999", "--apply")
        assert r.exit_code != 0
        assert "No account 999." in r.output

    def test_registered(self):
        from backend.billing_cli import stripe_forget_customer_command
        assert stripe_forget_customer_command.name == "stripe-forget-customer"


# ── A late event does not put the cleared id back ────────────────────────

class TestLateEvents:
    def test_an_ended_subscription_brings_no_customer_back(
            self, app, gateway):
        """After the admin cleared the id, a late (or resent) event about
        the deleted customer's cancelled subscription is applied, but its
        customer is not stored again."""
        u = _user(stripe_subscription_id="sub_old",
                  subscription_status="ended", subscription_level="standard")
        gateway.subscriptions["sub_old"] = subscription(
            "sub_old", customer="cus_gone", status="canceled", user_id=u.id)
        r = _post_event(app, _event("customer.subscription.deleted",
                                    {"id": "sub_old"}))
        assert r.status_code == 200, r.get_data(as_text=True)
        after = _columns(u.id)
        assert after["stripe_customer_id"] is None
        assert after["subscription_status"] == "ended"

    def test_a_live_subscription_still_brings_its_customer(
            self, app, gateway):
        """Control: a subscription that gives access, for an account
        without a customer, stores its customer as before."""
        u = _user()
        gateway.subscriptions["sub_new"] = subscription(
            "sub_new", customer="cus_new", status="active", user_id=u.id)
        r = _post_event(app, _event("customer.subscription.created",
                                    {"id": "sub_new"}))
        assert r.status_code == 200, r.get_data(as_text=True)
        after = _columns(u.id)
        assert after["stripe_customer_id"] == "cus_new"
        assert after["subscription_status"] == "active"

    def test_a_resent_checkout_brings_no_customer_back(self, app, gateway):
        """After the admin cleared the id, the deleted customer's
        checkout.session.completed is sent again (a resend from the
        Dashboard, or Stripe's retries after an outage): it is applied,
        and the cleared id stays cleared."""
        u = _gone_user(gateway, stripe_subscription_id="sub_old",
                       subscription_status="ended",
                       subscription_level="standard")
        gateway.subscriptions["sub_old"] = subscription(
            "sub_old", customer="cus_gone", status="canceled", user_id=u.id)
        assert _run(app, "--user", str(u.id), "--apply").exit_code == 0
        r = _post_event(app, _event("checkout.session.completed", {
            "id": "cs_old", "object": "checkout.session",
            "mode": "subscription", "customer": "cus_gone",
            "client_reference_id": str(u.id),
            "metadata": {"loore_user_id": str(u.id)},
            "subscription": "sub_old", "status": "complete"},
            event_id="evt_resent"))
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["status"] == "applied"
        after = _columns(u.id)
        assert after["stripe_customer_id"] is None
        assert after["subscription_status"] == "ended"

    def test_a_completed_checkout_for_an_account_without_a_customer(
            self, app, gateway):
        """Control: a completed checkout whose subscription gives access
        stores its customer (through the subscription)."""
        u = _user()
        gateway.subscriptions["sub_new"] = subscription(
            "sub_new", customer="cus_new", status="trialing", trial=True,
            user_id=u.id)
        r = _post_event(app, _event("checkout.session.completed", {
            "id": "cs_new", "object": "checkout.session",
            "mode": "subscription", "customer": "cus_new",
            "client_reference_id": str(u.id),
            "subscription": "sub_new", "status": "complete"}))
        assert r.status_code == 200, r.get_data(as_text=True)
        after = _columns(u.id)
        assert after["stripe_customer_id"] == "cus_new"
        assert after["subscription_status"] == "trialing"
