"""Referrals (#439): the invite link, the referral kept through signup,
and a free week for both on the friend's first payment, given once.

Two harnesses: the payments test app (test_payments.py: billing, webhook,
admin and dashboard blueprints, Stripe replaced by a stub gateway) for the
link, the reward and the admin list; and an app with the sign-in routes and
the real X blueprint (as test_x_login_linking.py) for the signup paths.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

import pytest  # noqa: E402

import flask_login as _real_flask_login  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import Referral, User, UserNotification  # noqa: E402
from backend.tests import test_payments as tp  # noqa: E402
from backend.tests.test_payments import (  # noqa: E402
    _client, _event, _post_event, _user, subscription)
from backend.utils import referrals, stripe_billing  # noqa: E402


class StripeRefused(Exception):
    """A client error Stripe would repeat."""
    http_status = 400


class ReferralGateway(tp.FakeGateway):
    """The stub gateway with customer balance and invoice previews."""

    def __init__(self):
        super().__init__()
        self.balance = {}       # customer -> [transactions]
        self.previews = {}      # subscription -> preview
        self.fail_credit = 0    # this many creates raise (Stripe down)
        self.fail_after_credit = 0  # created at Stripe, then the call fails
        self.refuse_credit_for = set()  # customers whose credit is refused
        self._idem = {}

    def preview_invoice(self, customer_id, subscription_id):
        self._call("preview_invoice", customer_id, subscription_id)
        return self.previews.get(subscription_id,
                                 {"total": 3570, "currency": "usd"})

    def list_balance_transactions(self, customer_id):
        self._call("list_balance_transactions", customer_id)
        return list(self.balance.get(customer_id, []))

    def create_balance_transaction(self, customer_id, params,
                                   idempotency_key=None):
        self._call("create_balance_transaction", customer_id, params,
                   idempotency_key)
        if customer_id in self.refuse_credit_for:
            raise StripeRefused("refused")
        if self.fail_credit:
            self.fail_credit -= 1
            raise RuntimeError("stripe down")
        if idempotency_key is not None and idempotency_key in self._idem:
            return self._idem[idempotency_key]
        txn = {"id": self._id("cbtxn"), "customer": customer_id,
               "amount": params["amount"], "currency": params["currency"],
               "metadata": dict(params.get("metadata") or {})}
        self.balance.setdefault(customer_id, []).append(txn)
        if idempotency_key is not None:
            self._idem[idempotency_key] = txn
        if self.fail_after_credit:
            self.fail_after_credit -= 1
            raise RuntimeError("connection reset after the write")
        return txn

    def credits(self):
        return [t for txns in self.balance.values() for t in txns]


@pytest.fixture
def gateway(monkeypatch):
    gw = ReferralGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    stripe_billing._price_cache.clear()
    for level in ("standard", "supporter", "discounted"):
        gw.prices.append({
            "id": f"price_{level}", "active": True,
            "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]})
    return gw


def _app(payments, secret=tp.SECRET, referrals_on=True):
    app, ctx = tp._app_fixture(payments, secret=secret)
    app.config["REFERRALS_ENABLED"] = referrals_on
    from backend.routes.referrals import referrals_bp
    app.register_blueprint(referrals_bp, url_prefix="/api/referrals")
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


@pytest.fixture
def app(gateway):
    yield from _app(True)


@pytest.fixture
def app_off(gateway):
    yield from _app(False, secret=None)


@pytest.fixture
def app_referrals_off(gateway):
    yield from _app(True, referrals_on=False)


def _pair(inviter_kw=None, friend_kw=None):
    """An inviter with a live Standard subscription, and a friend it
    invited whose subscription has just been created."""
    inviter = _user("ines", **{
        "stripe_customer_id": "cus_i", "stripe_subscription_id": "sub_i",
        "subscription_status": "active", "subscription_level": "standard",
        **(inviter_kw or {})})
    friend = _user("fred", **{
        "stripe_customer_id": "cus_f", "stripe_subscription_id": "sub_f",
        "subscription_status": "trialing", "subscription_level": "standard",
        **(friend_kw or {})})
    _db.session.add(Referral(inviter_id=inviter.id, friend_id=friend.id))
    _db.session.commit()
    return inviter, friend


def _paid(gateway, sub_id="sub_f", customer="cus_f", amount=3570,
          invoice_id="in_1", status="active"):
    gateway.subscriptions[sub_id] = subscription(
        sub_id, customer=customer, status=status)
    return {"id": invoice_id, "object": "invoice", "customer": customer,
            "subscription": sub_id, "amount_paid": amount, "total": amount,
            "currency": "usd", "status": "paid"}


def _referral():
    _db.session.expire_all()
    return Referral.query.one()


# ── The link ─────────────────────────────────────────────────────────────

class TestTheLink:
    def test_the_account_page_link(self, app):
        u = _user()
        r = _client(app, u).get("/api/referrals/link")
        assert r.status_code == 200
        url = r.get_json()["url"]
        code = _db.session.get(User, u.id).referral_code
        assert url == f"https://loore.test/invite/{code}"
        # The same link every time.
        assert _client(app, u).get(
            "/api/referrals/link").get_json()["url"] == url

    def test_the_code_says_nothing_about_the_account(self, app):
        u = _user("alice")
        code = referrals.invite_code(u)
        assert len(code) == 16
        assert "alice" not in code.lower()
        # Random, not a function of the account: made again for the same
        # account, it differs.
        u.referral_code = None
        _db.session.commit()
        assert referrals.invite_code(u) != code
        assert referrals.invite_code(_user("bob")) != code

    def test_off_with_payments(self, app_off):
        r = _client(app_off, _user()).get("/api/referrals/link")
        assert r.status_code == 404

    def test_off_with_its_own_switch(self, app_referrals_off):
        r = _client(app_referrals_off, _user()).get("/api/referrals/link")
        assert r.status_code == 404


def test_the_payload_flag_on(app):
    user = _client(app, _user()).get(
        "/api/dashboard/?profile=0").get_json()["user"]
    assert user["referrals_enabled"] is True


def test_the_payload_flag_off(app_off):
    """Control: payments off, no new key on the user object (#649's
    rule)."""
    user = _client(app_off, _user()).get(
        "/api/dashboard/?profile=0").get_json()["user"]
    assert "referrals_enabled" not in user


# ── Recording who invited whom ───────────────────────────────────────────

class TestRecording:
    def test_a_new_account_through_the_link(self, app):
        inviter = _user("ines")
        friend = _user("fred")
        assert referrals.record_invite(
            friend, referrals.invite_code(inviter), new_account=True)
        ref = Referral.query.one()
        assert (ref.inviter_id, ref.friend_id) == (inviter.id, friend.id)

    def test_no_self_referral(self, app):
        u = _user("ines")
        code = referrals.invite_code(u)
        assert referrals.record_invite(u, code, new_account=True) is None
        assert Referral.query.count() == 0

    def test_the_first_inviter_is_kept(self, app):
        a, b = _user("a"), _user("b")
        friend = _user("fred")
        assert referrals.record_invite(friend, referrals.invite_code(a), True)
        assert referrals.record_invite(
            friend, referrals.invite_code(b), True) is None
        assert Referral.query.one().inviter_id == a.id

    def test_an_existing_user_is_not_brought_by_a_link(self, app):
        inviter = _user("ines")
        member = _user("old", last_seen_at=datetime.utcnow())
        assert referrals.record_invite(
            member, referrals.invite_code(inviter), new_account=False) is None

    def test_an_account_set_up_ahead_at_its_first_sign_in(self, app):
        inviter = _user("ines")
        placeholder = _user("pre")   # created now, nobody signed in
        assert referrals.record_invite(
            placeholder, referrals.invite_code(inviter), new_account=False)

    def test_unknown_or_malformed_codes_record_nothing(self, app):
        friend = _user("fred")
        for code in ("x" * 16, "", None, 5, "../../etc", "a b c d e f g h"):
            assert referrals.record_invite(friend, code, True) is None
        assert Referral.query.count() == 0

    def test_a_deleted_inviter_records_nothing(self, app):
        inviter = _user("ines", deleted_at=datetime.utcnow())
        code = referrals.invite_code(inviter)
        assert referrals.record_invite(_user("fred"), code, True) is None

    def test_nothing_while_off(self, app_referrals_off):
        inviter = _user("ines")
        code = referrals.invite_code(inviter)
        assert referrals.record_invite(_user("fred"), code, True) is None


# ── The reward ───────────────────────────────────────────────────────────

class TestReward:
    def test_the_first_payment_gives_both_a_week(self, app, gateway):
        inviter, friend = _pair()
        gateway.previews["sub_i"] = {"total": 3570, "currency": "usd"}
        gateway.previews["sub_f"] = {"total": 1428, "currency": "usd"}
        r = _post_event(app, _event("invoice.paid", _paid(gateway)))
        assert r.status_code == 200, r.get_data(as_text=True)
        credits = {t["customer"]: t for t in gateway.credits()}
        # 7/30 of the next invoice, tax included.
        assert credits["cus_f"]["amount"] == -round(1428 * 7 / 30)
        assert credits["cus_i"]["amount"] == -round(3570 * 7 / 30) == -833
        assert credits["cus_f"]["currency"] == "usd"
        ref = _referral()
        assert ref.rewarded_at is not None
        assert ref.reward_invoice_id == "in_1"
        assert (ref.friend_reward, ref.inviter_reward) == ("granted",
                                                           "granted")
        assert ref.inviter_credit_cents == 833

    def test_the_notices_name_nobody(self, app, gateway):
        inviter, friend = _pair()
        _post_event(app, _event("invoice.paid", _paid(gateway)))
        notes = {n.user_id: n for n in UserNotification.query.all()}
        assert set(notes) == {inviter.id, friend.id}
        for n in notes.values():
            assert n.type == "referral_reward"
            text = f"{n.title} {n.body}".lower()
            assert "fred" not in text and "ines" not in text
            assert "paid" not in text and "subscri" not in text

    def test_a_trial_start_gives_nothing(self, app, gateway):
        """Control: a 0.00 trial invoice is not a payment."""
        _pair()
        invoice = _paid(gateway, amount=0, status="trialing")
        r = _post_event(app, _event("invoice.paid", invoice))
        assert r.status_code == 200
        assert gateway.credits() == []
        assert _referral().rewarded_at is None

    def test_a_replayed_event_gives_nothing_more(self, app, gateway):
        _pair()
        event = _event("invoice.paid", _paid(gateway), event_id="evt_p")
        _post_event(app, event)
        r = _post_event(app, event)
        assert r.get_json()["status"] == "duplicate"
        assert len(gateway.credits()) == 2

    def test_both_payment_events_of_one_invoice_give_it_once(
            self, app, gateway):
        _pair()
        invoice = _paid(gateway)
        _post_event(app, _event("invoice.paid", invoice, event_id="evt_a"))
        _post_event(app, _event("invoice.payment_succeeded", invoice,
                                event_id="evt_b"))
        assert len(gateway.credits()) == 2
        assert UserNotification.query.count() == 2
        assert gateway.names().count("create_balance_transaction") == 2

    def test_a_retry_after_stripe_failed_part_way(self, app, gateway):
        """The friend's credit is made, then Stripe fails on the
        inviter's: the event answers 503 and records nothing; the resent
        event finds the friend's credit and makes only the inviter's."""
        _pair()
        invoice = _paid(gateway)
        gateway.fail_credit = 0
        calls = {"n": 0}
        real = gateway.create_balance_transaction

        def second_fails(customer, params, idempotency_key=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("stripe down")
            return real(customer, params, idempotency_key)
        gateway.create_balance_transaction = second_fails
        r = _post_event(app, _event("invoice.paid", invoice, event_id="e1"))
        assert r.status_code == 503
        assert _referral().rewarded_at is None
        r = _post_event(app, _event("invoice.paid", invoice, event_id="e1"))
        assert r.status_code == 200
        credits = gateway.credits()
        assert sorted(t["customer"] for t in credits) == ["cus_f", "cus_i"]
        assert _referral().rewarded_at is not None

    def test_a_retry_after_a_lost_answer(self, app, gateway):
        """Stripe made the credit but the answer was lost: the resent event
        finds it by its mark and makes no second one."""
        _pair()
        invoice = _paid(gateway)
        gateway.fail_after_credit = 1
        assert _post_event(app, _event("invoice.paid", invoice,
                                       event_id="e1")).status_code == 503
        gateway._idem.clear()   # past Stripe's idempotency window
        assert _post_event(app, _event("invoice.paid", invoice,
                                       event_id="e1")).status_code == 200
        assert len(gateway.credits()) == 2

    def test_a_later_payment_gives_nothing(self, app, gateway):
        _pair()
        _post_event(app, _event("invoice.paid", _paid(gateway),
                                event_id="e1"))
        _post_event(app, _event("invoice.paid", _paid(
            gateway, invoice_id="in_2"), event_id="e2"))
        assert len(gateway.credits()) == 2
        assert UserNotification.query.count() == 2
        assert gateway.names().count("create_balance_transaction") == 2

    def test_a_refund_after_the_reward_takes_nothing_back(
            self, app, gateway):
        inviter, friend = _pair()
        invoice = _paid(gateway)
        _post_event(app, _event("invoice.paid", invoice, event_id="e1"))
        # The friend's first payment is refunded in full.
        sub = gateway.subscriptions["sub_f"]
        sub["latest_invoice"] = "in_1"
        gateway.invoices["in_1"] = {
            "id": "in_1", "amount_paid": 3570,
            "payments": {"data": [{"payment": {"payment_intent": "pi_1"}}]}}
        charge = {"id": "ch_1", "customer": "cus_f", "payment_intent": "pi_1",
                  "amount": 3570, "amount_refunded": 3570, "refunded": True}
        r = _post_event(app, _event("charge.refunded", charge,
                                    event_id="e2"))
        assert r.status_code == 200
        assert _db.session.get(User, friend.id).subscription_revoked == \
            "refund"
        # No clawback: both credits stay, the referral stays rewarded.
        assert len(gateway.credits()) == 2
        assert "delete_balance_transaction" not in gateway.names()
        assert _referral().inviter_reward == "granted"
        # A later paid invoice gives nothing more.
        _post_event(app, _event("invoice.paid", _paid(
            gateway, invoice_id="in_9"), event_id="e3"))
        assert len(gateway.credits()) == 2
        assert UserNotification.query.count() == 2

    def test_a_dispute_after_the_reward_takes_nothing_back(
            self, app, gateway):
        _pair()
        _post_event(app, _event("invoice.paid", _paid(gateway),
                                event_id="e1"))
        gateway.charges["ch_1"] = {"id": "ch_1", "customer": "cus_f",
                                   "payment_intent": "pi_1", "amount": 3570}
        r = _post_event(app, _event("charge.dispute.created",
                                    {"id": "dp_1", "charge": "ch_1"},
                                    event_id="e2"))
        assert r.status_code == 200
        assert len(gateway.credits()) == 2
        assert _referral().friend_reward == "granted"

    def test_the_inviters_week_is_at_most_what_the_friend_paid(
            self, app, gateway):
        """A Supporter inviter, a friend on the discounted price: the
        inviter's week is capped at the friend's first payment; the
        friend's own week is 7/30 of their next invoice."""
        _pair(inviter_kw={"subscription_level": "supporter"},
              friend_kw={"subscription_level": "discounted"})
        gateway.previews["sub_i"] = {"total": 11781, "currency": "usd"}
        gateway.previews["sub_f"] = {"total": 1428, "currency": "usd"}
        r = _post_event(app, _event("invoice.paid", dict(
            _paid(gateway, amount=1428), currency="usd")))
        assert r.status_code == 200
        credits = {t["customer"]: t["amount"] for t in gateway.credits()}
        assert credits["cus_i"] == -1428          # not -2749
        assert credits["cus_f"] == -round(1428 * 7 / 30)
        assert _referral().inviter_credit_cents == 1428

    def test_a_cheaper_inviter_keeps_their_own_week(self, app, gateway):
        """Control: below the cap, the inviter's own 7/30."""
        _pair()
        gateway.previews["sub_i"] = {"total": 1428, "currency": "usd"}
        _post_event(app, _event("invoice.paid", dict(
            _paid(gateway, amount=3570), currency="usd")))
        credits = {t["customer"]: t["amount"] for t in gateway.credits()}
        assert credits["cus_i"] == -round(1428 * 7 / 30)

    def test_two_currencies_cap_at_a_standard_week(self, app, gateway):
        """The friend paid in another currency: the inviter's week is at
        most a week of the Standard price (7.00 USD)."""
        _pair(inviter_kw={"subscription_level": "supporter"})
        gateway.previews["sub_i"] = {"total": 11781, "currency": "usd"}
        _post_event(app, _event("invoice.paid", dict(
            _paid(gateway, amount=3200), currency="eur")))
        credits = {t["customer"]: t["amount"] for t in gateway.credits()}
        assert credits["cus_i"] == -700

    def test_the_credit_key_differs_between_databases(self, app):
        """Two databases sharing the sandbox have the same referral ids:
        the key carries the referral's creation time."""
        from backend.utils.referrals import _credit_key
        a = Referral(id=3, created_at=datetime(2026, 11, 1))
        b = Referral(id=3, created_at=datetime(2026, 11, 2))
        assert _credit_key(a, "inviter") != _credit_key(b, "inviter")
        assert _credit_key(a, "inviter").startswith("loore-referral-3-")

    def test_an_inviter_without_a_stripe_customer(self, app, gateway):
        """An account set by hand (alpha) never checked out: a customer is
        made with the checkout's key, and the week is the Standard price's,
        before tax."""
        inviter = _user("ines", plan="alpha")
        friend = _user("fred", stripe_customer_id="cus_f",
                       stripe_subscription_id="sub_f",
                       subscription_status="trialing",
                       subscription_level="standard")
        _db.session.add(Referral(inviter_id=inviter.id, friend_id=friend.id))
        _db.session.commit()
        _post_event(app, _event("invoice.paid", _paid(gateway)))
        made = [c for c in gateway.calls if c[0] == "create_customer"]
        assert made and made[0][2] == \
            stripe_billing.customer_idempotency_key(inviter)
        assert made[0][2].startswith(f"loore-customer-{inviter.id}-")
        cus = _db.session.get(User, inviter.id).stripe_customer_id
        credit = [t for t in gateway.credits() if t["customer"] == cus][0]
        assert credit["amount"] == -700
        assert credit["currency"] == "usd"

    def test_a_deleted_inviter_gets_nothing_the_friend_does(
            self, app, gateway):
        _pair(inviter_kw={"deleted_at": datetime.utcnow()})
        _post_event(app, _event("invoice.paid", _paid(gateway)))
        assert [t["customer"] for t in gateway.credits()] == ["cus_f"]
        assert _referral().inviter_reward == "skipped_deleted"

    def test_a_refused_credit_does_not_hold_the_event(self, app, gateway):
        """Stripe refuses the inviter's credit for good: recorded as
        failed and logged; the friend's subscription event still applies."""
        inviter, friend = _pair()
        gateway.refuse_credit_for.add("cus_i")
        r = _post_event(app, _event("invoice.paid", _paid(gateway)))
        assert r.status_code == 200
        ref = _referral()
        assert (ref.friend_reward, ref.inviter_reward) == ("granted",
                                                           "failed")
        assert _db.session.get(User, friend.id).subscription_ever_paid

    def test_a_self_referral_row_gives_nothing(self, app, gateway):
        """Control (defence): a row naming one account twice is never
        paid out; record_invite never writes one."""
        u = _user("solo", stripe_customer_id="cus_f",
                  stripe_subscription_id="sub_f",
                  subscription_status="active",
                  subscription_level="standard")
        _db.session.add(Referral(inviter_id=u.id, friend_id=u.id))
        _db.session.commit()
        _post_event(app, _event("invoice.paid", _paid(gateway)))
        assert gateway.credits() == []

    def test_no_referral_no_credit(self, app, gateway):
        """Control: a first payment of an account nobody invited."""
        _user("fred", stripe_customer_id="cus_f",
              stripe_subscription_id="sub_f", subscription_status="trialing",
              subscription_level="standard")
        r = _post_event(app, _event("invoice.paid", _paid(gateway)))
        assert r.status_code == 200
        assert gateway.credits() == []


class TestCustomerKey:
    def test_two_databases_never_share_a_customer(self, app):
        """Staging and local development share the Stripe sandbox and
        have the same account ids: the key of the customer-creating call
        tells them apart, so Stripe never hands one the other's customer
        (it did in the sandbox, 2026-11-12, before this)."""
        a = User(id=7, username="a", created_at=datetime(2026, 11, 1))
        b = User(id=7, username="b", created_at=datetime(2026, 11, 2))
        assert stripe_billing.customer_idempotency_key(a) != \
            stripe_billing.customer_idempotency_key(b)

    def test_checkout_and_credit_use_one_key(self, app, gateway):
        """A checkout and a referral's credit for one account within
        Stripe's window make one customer, not two."""
        u = _user("ines")
        _client(app, u).post("/api/billing/checkout",
                             json={"level": "standard"})
        made = [c for c in gateway.calls if c[0] == "create_customer"]
        assert made[0][2] == stripe_billing.customer_idempotency_key(u)


class TestPaidOnce:
    def test_an_invoice_paid_by_credit_counts_as_paid(self, app, gateway):
        """Credit can cover a whole invoice: it was paid, so a later failed
        payment leaves the account lapsed, not shut out."""
        u = _user("c", stripe_customer_id="cus_c",
                  stripe_subscription_id="sub_c",
                  subscription_status="active",
                  subscription_level="discounted")
        gateway.subscriptions["sub_c"] = subscription("sub_c",
                                                      customer="cus_c")
        invoice = {"id": "in_c", "subscription": "sub_c", "customer": "cus_c",
                   "amount_paid": 0, "total": 1428, "status": "paid"}
        _post_event(app, _event("invoice.paid", invoice))
        assert _db.session.get(User, u.id).subscription_ever_paid

    def test_a_trial_invoice_is_not_a_payment(self, app, gateway):
        """Control: #649's rule for a 0.00 invoice."""
        u = _user("c", stripe_customer_id="cus_c",
                  stripe_subscription_id="sub_c",
                  subscription_status="trialing",
                  subscription_level="standard")
        gateway.subscriptions["sub_c"] = subscription(
            "sub_c", customer="cus_c", status="trialing", trial=True)
        invoice = {"id": "in_c", "subscription": "sub_c", "customer": "cus_c",
                   "amount_paid": 0, "total": 0, "status": "paid"}
        _post_event(app, _event("invoice.paid", invoice))
        assert not _db.session.get(User, u.id).subscription_ever_paid


# ── Admin ────────────────────────────────────────────────────────────────

class TestAdmin:
    def test_who_invited_whom(self, app, gateway):
        inviter, friend = _pair()
        _post_event(app, _event("invoice.paid", _paid(gateway)))
        admin = _client(app, _user("root", is_admin=True))
        rows = admin.get("/api/admin/referrals").get_json()["referrals"]
        assert len(rows) == 1
        row = rows[0]
        assert (row["inviter"], row["friend"]) == ("ines", "fred")
        assert row["inviter_reward"] == "granted"
        assert row["inviter_credit_cents"] == 833
        assert row["rewarded_at"]

    def test_only_admins(self, app):
        r = _client(app, _user()).get("/api/admin/referrals")
        assert r.status_code == 403

    def test_off_with_payments(self, app_off):
        """Control: 404 while payments are off, as #649's admin lists."""
        admin = _client(app_off, _user("root", is_admin=True))
        assert admin.get("/api/admin/referrals").status_code == 404


# ── Signup: the X round trip and the email link ─────────────────────────

FRONTEND_URL = "http://frontend.test"


def _signup_app():
    from flask import Flask
    from flask_login import LoginManager
    from backend.config import Config

    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    app.config["TWITTER_API_KEY"] = "fake"
    app.config["TWITTER_API_SECRET"] = "fake"
    app.config["PAYMENTS_ENABLED"] = True
    app.config["REFERRALS_ENABLED"] = True
    app.config["DEPLOY_ENV"] = "testing"
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.auth import auth_bp
    from backend.oauth import init_twitter_blueprint
    app.register_blueprint(auth_bp, url_prefix="/auth")
    init_twitter_blueprint(app)
    return app


@pytest.fixture
def signup_app():
    affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]
    app = _signup_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _x(x_id, screen_name, authorized=True):
    x = MagicMock()
    x.authorized = authorized
    x.get.return_value = MagicMock(
        ok=True, json=lambda: {"id": x_id, "screen_name": screen_name})
    return x


def _inviter_code():
    inviter = User(username="ines", approved=True,
                   last_seen_at=datetime.utcnow())
    _db.session.add(inviter)
    _db.session.commit()
    return inviter, referrals.invite_code(inviter)


class TestSignup:
    def test_the_x_round_trip_keeps_the_invite(self, signup_app):
        inviter, code = _inviter_code()
        client = signup_app.test_client()
        # Off to X: the invite waits in the session.
        with patch("backend.routes.auth.twitter",
                   _x(1, "fred", authorized=False)):
            r = client.get(f"/auth/login?invite={code}")
        assert r.status_code == 302
        # Back from X: a new account, invited by Ines, not approved.
        with patch("backend.routes.auth.twitter", _x(4242, "fred")):
            r = client.get("/auth/login")
        assert r.status_code == 302
        friend = User.query.filter_by(twitter_id="4242").one()
        ref = Referral.query.one()
        assert (ref.inviter_id, ref.friend_id) == (inviter.id, friend.id)
        assert not friend.approved
        with client.session_transaction() as sess:
            assert referrals.SESSION_KEY not in sess

    def test_an_existing_x_account_is_not_brought_by_a_link(
            self, signup_app):
        """Control: a code changes nothing for an existing account."""
        _inviter, code = _inviter_code()
        member = User(username="old", twitter_id="77", approved=True,
                      last_seen_at=datetime.utcnow())
        _db.session.add(member)
        _db.session.commit()
        client = signup_app.test_client()
        with patch("backend.routes.auth.twitter", _x(77, "old")):
            client.get(f"/auth/login?invite={code}")
        assert Referral.query.count() == 0

    def test_the_email_link_keeps_the_invite(self, signup_app):
        inviter, code = _inviter_code()
        client = signup_app.test_client()
        sent = {}

        def capture(email, url, **_kwargs):   # #671 passes user_id
            sent["url"] = url
        with patch("backend.routes.auth.send_magic_link_email", capture):
            r = client.post("/auth/magic-link/send", json={
                "email": "fred@example.com", "invite": code})
        assert r.status_code == 200
        # Opened later in the same browser: the link records the invite.
        path = sent["url"].split("://", 1)[1].split("/", 1)[1]
        r = client.get("/" + path)
        assert r.status_code == 302
        friend = User.query.filter_by(email="fred@example.com").one()
        assert not friend.approved
        assert Referral.query.one().inviter_id == inviter.id

    def _mail(self, client, email, invite):
        sent = {}
        with patch("backend.routes.auth.send_magic_link_email",
                   lambda e, url, **_kw: sent.update(url=url)):
            client.post("/auth/magic-link/send",
                        json={"email": email, "invite": invite})
        return "/" + sent["url"].split("://", 1)[1].split("/", 1)[1]

    def test_a_code_put_on_someone_elses_sign_in_records_nothing(
            self, signup_app):
        """Someone asks for a sign-in link for another person's address
        with their own code: that person signs in through it, and no
        referral is recorded (nor would its reward be sent)."""
        _inviter, code = _inviter_code()
        link = self._mail(signup_app.test_client(), "vic@example.com", code)
        victim = signup_app.test_client()
        r = victim.get(link)
        assert r.status_code == 302
        assert User.query.filter_by(email="vic@example.com").one()
        assert Referral.query.count() == 0

    def test_the_link_opened_in_another_browser_records_nothing(
            self, signup_app):
        """The price of the rule above: the invite is followed on one
        device and the mail opened on another."""
        _inviter, code = _inviter_code()
        link = self._mail(signup_app.test_client(), "fred@example.com", code)
        signup_app.test_client().get(link)
        assert Referral.query.count() == 0

    def test_a_code_changes_no_answer(self, signup_app):
        """Control (privacy): the send answer is the same for a real, a
        made-up and no code: nothing tells whether a code belongs to
        anyone."""
        _inviter, code = _inviter_code()
        answers = []
        with patch("backend.routes.auth.send_magic_link_email",
                   lambda *a, **k: None):
            for invite in (code, "Zz" * 8, None):
                r = signup_app.test_client().post(
                    "/auth/magic-link/send",
                    json={"email": "x@example.com", "invite": invite})
                answers.append((r.status_code, r.get_json()))
        assert answers[0] == answers[1] == answers[2]
