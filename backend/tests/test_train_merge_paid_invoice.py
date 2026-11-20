"""The webhook's paid-invoice step after the payments train's merge (PR
#680, "Conflicts and how each was resolved": backend/utils/
stripe_billing.py, #664 onto #661).

#661 recorded the first payment only for ``amount_paid > 0`` and, when the
new ceiling differs from the old one, lifts a spend block it no longer
warrants (an alpha tester's ceiling is Standard's until the first paid
invoice). #664 counts an invoice paid by customer credit (``total > 0``)
as paid, and then grants the referral's credits. Merged: the payment is
recorded under #664's condition, with #661's ceiling check around it,
then #664's grant. Payments on throughout.

The referral's own trigger stays #664's: a first payment by card
(``amount_paid > 0``). A first invoice paid entirely by credit counts as
paid and lifts the block, and grants no referral credit; the first
payment by card does, once.

Same harness as test_referrals.py (the payments test app with the stub
gateway that keeps customer balance).
"""
from datetime import datetime, timedelta

from backend.extensions import db as _db
from backend.models import Referral, User, UserNotification
from backend.tests import test_payments as tp
from backend.tests.test_payments import _event, _post_event, _user
from backend.tests.test_referrals import app, gateway  # noqa: F401
from backend.utils.spend import current_month, get_user_spend_limit_usd
from backend.utils.spend import enforce_user_spend_cap, user_is_capped


def _ceilings(flask_app):
    flask_app.config["SUBSCRIPTION_SPEND_LIMIT_USD_STANDARD"] = 50.0
    flask_app.config["SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER"] = 100.0
    flask_app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 50.0


def _capped_tester(flask_app, gw, level):
    """Tess, an alpha tester in her free month, invited by Ines, who
    subscribed at *level* during the month (a trial to its end). She has
    used 56 USD this month: over Standard's 50, her ceiling until a first
    paid invoice. Both blocks are on: the free month's (counted from its
    start) and the month's at Standard's ceiling (as in
    test_alpha_free_month)."""
    _ceilings(flask_app)
    inviter = _user("ines", stripe_customer_id="cus_i",
                    stripe_subscription_id="sub_i",
                    subscription_status="active",
                    subscription_level="standard")
    tess = _user("tess", plan="free", stripe_customer_id="cus_t")
    tess.alpha_free_granted_at = datetime.utcnow() - timedelta(days=3)
    tess.alpha_free_start = datetime.utcnow() - timedelta(days=2)
    tess.alpha_free_until = tess.alpha_free_start + timedelta(days=30)
    _db.session.add(Referral(inviter_id=inviter.id, friend_id=tess.id))
    _db.session.commit()
    gw.subscriptions["sub_t"] = tp.subscription(
        "sub_t", customer="cus_t", status="trialing", trial=True,
        level=level)
    created = _event("customer.subscription.created", {"id": "sub_t"},
                     event_id="e0")
    assert _post_event(flask_app, created).status_code == 200
    tp._cost(tess.id, 56, datetime.utcnow() - timedelta(minutes=1))
    enforce_user_spend_cap(tess.id, flask_app.config,
                           send_email=lambda **_k: None)
    tess = _db.session.get(User, tess.id)
    tess.spend_blocked_month = current_month()
    _db.session.commit()
    assert tess.subscription_trial_capped and user_is_capped(tess)
    assert get_user_spend_limit_usd(tess, flask_app.config) == 50.0
    # The free month ends: Stripe makes the subscription active and
    # charges its first invoice.
    gw.subscriptions["sub_t"]["status"] = "active"
    return inviter, tess


def _invoice(invoice_id, total, amount_paid):
    return {"id": invoice_id, "object": "invoice", "customer": "cus_t",
            "subscription": "sub_t", "total": total,
            "amount_paid": amount_paid, "currency": "usd",
            "status": "paid"}


def _tess():
    _db.session.expire_all()
    return User.query.filter_by(username="tess").one()


def test_a_first_invoice_paid_by_credit_counts_and_lifts_the_block(
        app, gateway):  # noqa: F811
    """Supporter (99.00 + 18.81 VAT), paid entirely by customer credit:
    paid once, and the new ceiling (100) no longer warrants the block, so
    it is lifted at once. Guards both merged lines: #664's ``or
    (obj.get("total") or 0) > 0`` (merged #661's way, a credit-paid
    invoice is no payment: not paid once, still capped) and #661's
    ``reconcile_user_spend_block(user, cfg)`` (merged #664's way, the
    month's block stays)."""
    _capped_tester(app, gateway, "supporter")
    r = _post_event(app, _event("invoice.paid", _invoice(
        "in_1", total=11781, amount_paid=0), event_id="e1"))
    assert r.status_code == 200, r.get_data(as_text=True)
    tess = _tess()
    assert tess.subscription_ever_paid
    assert not tess.subscription_trial_capped
    assert get_user_spend_limit_usd(tess, app.config) == 100.0
    assert tess.spend_blocked_month is None
    assert not user_is_capped(tess)


def test_control_the_ceiling_does_not_change_the_block_stays(
        app, gateway):  # noqa: F811
    """Control: at Standard the ceiling is 50 before and after, and 56 are
    used this month: paid once, and still capped for the month (both
    versions keep it)."""
    _capped_tester(app, gateway, "standard")
    _post_event(app, _event("invoice.paid", _invoice(
        "in_1", total=3570, amount_paid=0), event_id="e1"))
    tess = _tess()
    assert tess.subscription_ever_paid
    assert get_user_spend_limit_usd(tess, app.config) == 50.0
    assert tess.spend_blocked_month == current_month()
    assert user_is_capped(tess)


def test_the_referral_credits_come_with_the_first_payment_by_card_once(
        app, gateway):  # noqa: F811
    """The credit-paid first invoice grants no referral credit (#664's
    trigger is a payment by card); the next invoice, paid by card, gives
    both sides their 7.50 once: its second event and a replay give nothing
    more. Guards the merged grant after the ceiling step
    (``grant_on_first_payment(gateway, user, obj, config)``; merged #661's
    way, no credit is ever made)."""
    inviter, tess = _capped_tester(app, gateway, "supporter")
    _post_event(app, _event("invoice.paid", _invoice(
        "in_1", total=11781, amount_paid=0), event_id="e1"))
    assert gateway.credits() == []
    assert Referral.query.one().rewarded_at is None

    card = _invoice("in_2", total=11781, amount_paid=11781)
    for event_id, etype in (("e2", "invoice.paid"),
                            ("e3", "invoice.payment_succeeded"),
                            ("e2", "invoice.paid")):
        assert _post_event(app, _event(etype, card, event_id=event_id)
                           ).status_code == 200
    credits = {t["customer"]: t["amount"] for t in gateway.credits()}
    assert credits == {"cus_t": -750, "cus_i": -750}
    assert gateway.names().count("create_balance_transaction") == 2
    assert UserNotification.query.filter_by(
        type="referral_reward").count() == 2
    ref = Referral.query.one()
    assert ref.reward_invoice_id == "in_2"
    assert not user_is_capped(_tess())


def test_a_first_payment_by_card_does_all_three_once(
        app, gateway):  # noqa: F811
    """The usual path: the first invoice paid by card is a payment, lifts
    the block the new ceiling no longer warrants, and gives the referral's
    credits, once."""
    _capped_tester(app, gateway, "supporter")
    card = _invoice("in_1", total=11781, amount_paid=11781)
    _post_event(app, _event("invoice.paid", card, event_id="e1"))
    _post_event(app, _event("invoice.payment_succeeded", card,
                            event_id="e2"))
    tess = _tess()
    assert tess.subscription_ever_paid and not user_is_capped(tess)
    assert sorted(t["customer"] for t in gateway.credits()) == [
        "cus_i", "cus_t"]
