"""One free trial per card, as well as per account (#697).

With nobody approving new accounts, one person could take a trial on each
of several accounts with the same card. A trial whose card already
started one on another account is cancelled the way #649 cancels a
second trial of one account; the account may then subscribe without a
trial. Loore keeps only a keyed hash of the card's Stripe fingerprint and
the date.

The payments test app (test_payments.py: Stripe replaced by a stub
gateway, webhook events signed here). Stripe is never called: what this
relies on (the subscription's default_payment_method and that payment
method's card.fingerprint) is checked against the real sandbox before
payments go on.
"""
import hashlib
from datetime import datetime, timedelta

import pytest

from backend.extensions import db as _db
from backend.models import TrialCard, User
from backend.tests import test_payments as tp
from backend.tests.test_payments import (
    _client, _event, _post_event, _user, subscription)
from backend.utils import stripe_billing, trial_cards


class CardGateway(tp.FakeGateway):
    """The stub gateway with payment methods."""

    def __init__(self):
        super().__init__()
        self.payment_methods = {}

    def retrieve_payment_method(self, pm_id):
        self._call("retrieve_payment_method", pm_id)
        return self.payment_methods[pm_id]


@pytest.fixture
def gateway(monkeypatch):
    gw = CardGateway()
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


def _card(gw, pm_id, fingerprint, wallet=None):
    card = {"fingerprint": fingerprint, "last4": "4242", "brand": "visa"}
    if wallet:
        card["wallet"] = {"type": wallet}
    gw.payment_methods[pm_id] = {"id": pm_id, "type": "card", "card": card}


def _trial(gw, sub_id, customer, pm_id, status="trialing", trial=True):
    sub = subscription(sub_id, customer=customer, status=status, trial=trial)
    sub["default_payment_method"] = pm_id
    gw.subscriptions[sub_id] = sub
    return sub


def _created(app, sub_id, event_id):
    r = _post_event(app, _event("customer.subscription.created",
                                {"id": sub_id}, event_id))
    assert r.status_code == 200, r.get_json()
    return r


def _fresh(user):
    _db.session.expire_all()
    return _db.session.get(User, user.id)


def test_a_first_trial_goes_ahead_and_keeps_only_a_keyed_hash(app, gateway):
    ann = _user("ann", stripe_customer_id="cus_a")
    _card(gateway, "pm_a", "fp_card_1")
    _trial(gateway, "sub_a", "cus_a", "pm_a")
    _created(app, "sub_a", "evt_1")
    assert _fresh(ann).subscription_status == "trialing"
    assert "cancel_subscription" not in gateway.names()
    mark = TrialCard.query.one()
    assert mark.user_id == ann.id and mark.created_at is not None
    # Neither the fingerprint nor a plain hash of it is stored.
    assert mark.card_hash == trial_cards.card_hash("fp_card_1")
    assert "fp_card_1" not in mark.card_hash
    assert mark.card_hash != hashlib.sha256(b"fp_card_1").hexdigest()
    # Later events of the same trial read the card no more.
    _post_event(app, _event("customer.subscription.updated",
                            {"id": "sub_a"}, "evt_2"))
    assert gateway.names().count("retrieve_payment_method") == 1
    assert TrialCard.query.count() == 1


def test_the_same_card_on_another_account_gets_no_trial(app, gateway):
    _user("ann", stripe_customer_id="cus_a")
    ben = _user("ben", stripe_customer_id="cus_b")
    _card(gateway, "pm_a", "fp_card_1")
    _card(gateway, "pm_b", "fp_card_1")   # the same card, another customer
    _trial(gateway, "sub_a", "cus_a", "pm_a")
    _created(app, "sub_a", "evt_1")
    _trial(gateway, "sub_b", "cus_b", "pm_b")
    _created(app, "sub_b", "evt_2")

    # Cancelled as #649 cancels a second trial: marked, nothing to refund.
    sub_b = gateway.subscriptions["sub_b"]
    assert sub_b["status"] == "canceled"
    assert sub_b["metadata"][stripe_billing.DUPLICATE_METADATA_KEY] == "1"
    assert "create_refund" not in gateway.names()
    user = _fresh(ben)
    assert user.subscription_status == "ended"
    assert not user.on_paid_plan
    assert user.subscription_trial_used is True
    assert user.subscription_trial_card_refused_at is not None
    assert not user.subscription_cancelled_by_owner
    assert TrialCard.query.count() == 1   # ben's card is not recorded again
    # The page says why, and a subscription without a trial is possible.
    state = _client(app, user).get("/api/billing").get_json()["subscription"]
    assert state["state"] == "none"
    assert state["trial_card_refused"] is True
    assert state["trial_available"] is False
    params = stripe_billing.checkout_session_params(user, "standard", "p")
    assert "trial_period_days" not in params["subscription_data"]


def test_paying_on_a_second_account_is_unaffected(app, gateway):
    """A subscription without a trial, with a card that had a trial on
    another account: nothing happens to it."""
    _user("ann", stripe_customer_id="cus_a")
    ben = _user("ben", stripe_customer_id="cus_b")
    _card(gateway, "pm_a", "fp_card_1")
    _card(gateway, "pm_b", "fp_card_1")
    _trial(gateway, "sub_a", "cus_a", "pm_a")
    _created(app, "sub_a", "evt_1")
    _trial(gateway, "sub_b", "cus_b", "pm_b", status="active", trial=False)
    _created(app, "sub_b", "evt_2")
    assert gateway.subscriptions["sub_b"]["status"] == "active"
    user = _fresh(ben)
    assert user.subscription_status == "active"
    assert user.subscription_trial_card_refused_at is None


def test_another_card_gets_its_trial(app, gateway):
    _user("ann", stripe_customer_id="cus_a")
    ben = _user("ben", stripe_customer_id="cus_b")
    _card(gateway, "pm_a", "fp_card_1")
    _card(gateway, "pm_b", "fp_card_2")
    _trial(gateway, "sub_a", "cus_a", "pm_a")
    _created(app, "sub_a", "evt_1")
    _trial(gateway, "sub_b", "cus_b", "pm_b")
    _created(app, "sub_b", "evt_2")
    assert _fresh(ben).subscription_status == "trialing"
    assert TrialCard.query.count() == 2


def test_a_deleted_accounts_mark_still_counts(app, gateway):
    """An account deletion keeps the mark without the account: the card
    has had its trial."""
    ben = _user("ben", stripe_customer_id="cus_b")
    _card(gateway, "pm_b", "fp_card_1")
    _db.session.add(TrialCard(card_hash=trial_cards.card_hash("fp_card_1"),
                              user_id=None,
                              created_at=datetime.utcnow() - timedelta(days=40)))
    _db.session.commit()
    _trial(gateway, "sub_b", "cus_b", "pm_b")
    _created(app, "sub_b", "evt_1")
    assert gateway.subscriptions["sub_b"]["status"] == "canceled"
    assert _fresh(ben).subscription_trial_card_refused_at is not None


def test_detach_for_purge_keeps_the_mark_without_the_account(app):
    ann = _user("ann")
    _db.session.add(TrialCard(card_hash="x" * 64, user_id=ann.id))
    _db.session.commit()
    assert trial_cards.detach_for_purge(ann.id) == 1
    _db.session.commit()
    mark = TrialCard.query.one()
    assert mark.user_id is None and mark.card_hash == "x" * 64


@pytest.mark.parametrize("pm", [
    {"id": "pm_l", "type": "link", "link": {"email": "a@example.com"}},
    {"id": "pm_l", "type": "card", "card": {"last4": "4242"}},
])
def test_without_a_card_fingerprint_nothing_is_checked(app, gateway, pm):
    """Link and other methods carry no card fingerprint: a deterrent,
    not a guarantee."""
    ann = _user("ann", stripe_customer_id="cus_a")
    gateway.payment_methods["pm_l"] = pm
    _trial(gateway, "sub_a", "cus_a", "pm_l")
    _created(app, "sub_a", "evt_1")
    assert _fresh(ann).subscription_status == "trialing"
    assert TrialCard.query.count() == 0


def test_a_wallet_card_counts_as_its_own_card(app, gateway):
    """Apple Pay and Google Pay give a device card number: its fingerprint
    is not the card's own, so it is a different mark (as documented)."""
    _user("ann", stripe_customer_id="cus_a")
    ben = _user("ben", stripe_customer_id="cus_b")
    _card(gateway, "pm_a", "fp_card_1")
    _card(gateway, "pm_b", "fp_device_1", wallet="apple_pay")
    _trial(gateway, "sub_a", "cus_a", "pm_a")
    _created(app, "sub_a", "evt_1")
    _trial(gateway, "sub_b", "cus_b", "pm_b")
    _created(app, "sub_b", "evt_2")
    assert _fresh(ben).subscription_status == "trialing"


def test_no_default_payment_method_is_not_checked(app, gateway):
    ann = _user("ann", stripe_customer_id="cus_a")
    gateway.subscriptions["sub_a"] = subscription(
        "sub_a", customer="cus_a", status="trialing", trial=True)
    _created(app, "sub_a", "evt_1")
    assert _fresh(ann).subscription_status == "trialing"
    assert "retrieve_payment_method" not in gateway.names()


def test_the_alpha_free_month_is_neither_checked_nor_recorded(app, gateway):
    """An alpha tester's Checkout carries the free month as a trial: it is
    given by hand, not the trial a card is limited to."""
    _user("ann", stripe_customer_id="cus_a")
    tess = _user("tess", stripe_customer_id="cus_t",
                 alpha_free_granted_at=datetime.utcnow(),
                 alpha_free_start=datetime.utcnow() - timedelta(days=1),
                 alpha_free_until=datetime.utcnow() + timedelta(days=29))
    _card(gateway, "pm_a", "fp_card_1")
    _card(gateway, "pm_t", "fp_card_1")
    _trial(gateway, "sub_a", "cus_a", "pm_a")
    _created(app, "sub_a", "evt_1")
    _trial(gateway, "sub_t", "cus_t", "pm_t")
    _created(app, "sub_t", "evt_2")
    assert gateway.subscriptions["sub_t"]["status"] == "trialing"
    assert _fresh(tess).subscription_trial_card_refused_at is None
    assert TrialCard.query.filter_by(user_id=tess.id).count() == 0


def test_the_hash_is_keyed():
    a = trial_cards.card_hash("fp_card_1", {"SECRET_KEY": "one"})
    assert a == trial_cards.card_hash("fp_card_1", {"SECRET_KEY": "one"})
    assert a != trial_cards.card_hash("fp_card_1", {"SECRET_KEY": "two"})
    assert a != trial_cards.card_hash("fp_card_2", {"SECRET_KEY": "one"})
    assert trial_cards.card_hash(None, {"SECRET_KEY": "one"}) is None
    assert trial_cards.card_hash("fp", {}) is None
