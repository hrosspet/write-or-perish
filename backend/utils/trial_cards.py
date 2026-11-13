"""One free trial per card, as well as one per account (#697).

With nobody approving new accounts, one person could open several
accounts and take a trial on each with the same card, each up to the
trial's usage ceiling. So a trial is given once per card too.

What Loore reads (no card data reaches it): Stripe-hosted Checkout, in
subscription mode, collects the card and sets it as the new
subscription's default payment method. Whenever Loore settles a
subscription that is in its trial (stripe_billing.settle_subscription:
the customer.subscription.created and checkout.session.completed events,
any later event while the trial runs, and the return page's sync), it
reads the subscription's ``default_payment_method``, then that payment
method's ``card.fingerprint``: Stripe's identifier of the card number,
the same across the customers of one Stripe account. Loore keeps only a
keyed hash of it (HMAC-SHA256 under TRIAL_CARD_HASH_KEY, or SECRET_KEY
while that is unset) and the date (models.TrialCard), never the
fingerprint itself.

A trial whose card already started one on another account is cancelled
at once, the way #649 cancels a second trial of one account
(stripe_billing.cancel_duplicate: nothing was charged during a trial, so
nothing is refunded). The account then has no trial, and may subscribe
without one: the first payment is due at checkout.

It fails open, and says so. A trial whose card cannot be read is
applied as usual: no card fingerprint (Link, other methods Managed
Payments offers), no key, or a failed read of the payment method (Stripe
down, or a restricted key without read access to payment methods). The
first is logged once per subscription at WARNING (the payment method's
type, never card data), a failed read at ERROR; the sandbox check before
payments go on, and the logs after, show whether the check works.

A deterrent, not a guarantee. Payment methods without a card fingerprint
are not checked. Apple Pay and Google Pay pay with a device card number whose
fingerprint differs from the card's own, so one card through a wallet
and typed in counts as two cards. A subscription without a trial is
never affected (the same person paying for a second account), and the
alpha testers' free month, which Checkout gives as a trial too, is
neither checked nor recorded: it is given by hand.

An account deletion keeps the row without the account
(account_deletion.delete_identity), as the anonymous mark that this card
had its trial. Changing the hash key forgets every mark.
"""
import hashlib
import hmac
import logging
from datetime import datetime

from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError

from backend.extensions import db

logger = logging.getLogger(__name__)

_HASH_CONTEXT = b"loore-trial-card:"


def card_hash(fingerprint, config=None):
    """The stored form of a card fingerprint, or None without one."""
    if not fingerprint or not isinstance(fingerprint, str):
        return None
    cfg = config if config is not None else (
        current_app.config if has_app_context() else {})
    key = cfg.get("TRIAL_CARD_HASH_KEY") or cfg.get("SECRET_KEY")
    if not key:
        return None
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, _HASH_CONTEXT + fingerprint.encode(),
                    hashlib.sha256).hexdigest()


def card_fingerprint(gateway, sub):
    """(the fingerprint of the card *sub* (a subscription read from
    Stripe) charges, or None; what was found instead, for the log: the
    payment method's type, or "no payment method"). A failed read of the
    payment method raises."""
    pm = sub.get("default_payment_method")
    if not pm:
        return None, "no payment method"
    if not isinstance(pm, dict):
        pm = gateway.retrieve_payment_method(pm) or {}
    card = pm.get("card") or {}
    fingerprint = card.get("fingerprint") or None
    return fingerprint, (pm.get("type") or "unknown type")


def trial_card_refused(gateway, user, sub, config=None, now=None):
    """For *sub*, a subscription of *user* in its trial: True when its
    card already started a trial on another account (the caller cancels
    the subscription). Otherwise records the account's trial card, once,
    and returns False. Does not commit."""
    from backend.models import TrialCard
    if sub.get("status") != "trialing" or user.is_alpha_tester:
        return False
    if TrialCard.query.filter_by(user_id=user.id).first() is not None:
        return False   # the account's trial card is known already
    # Logged once per subscription: at its first settle the account does
    # not hold it as its trial yet (apply_subscription sets that).
    first = user.subscription_trial_sub_id != sub.get("id")
    try:
        fingerprint, found = card_fingerprint(gateway, sub)
    except Exception as e:  # noqa: BLE001 - optional: fail open, loudly
        if first:
            logger.error("Trial card check of subscription %s (user %s) "
                         "failed, trial applied: %s", sub.get("id"),
                         user.id, type(e).__name__)
        return False
    digest = card_hash(fingerprint, config)
    if digest is None:
        if first:
            logger.warning("Trial card check of subscription %s (user %s) "
                           "skipped, trial applied: %s", sub.get("id"),
                           user.id, "no hash key" if fingerprint
                           else f"no card fingerprint ({found})")
        return False
    holder = TrialCard.query.filter_by(card_hash=digest).first()
    if holder is None:
        try:
            with db.session.begin_nested():
                db.session.add(TrialCard(card_hash=digest, user_id=user.id,
                                         created_at=now or datetime.utcnow()))
            return False
        except IntegrityError:
            # Another account's trial recorded this card a moment ago.
            holder = TrialCard.query.filter_by(card_hash=digest).first()
            if holder is None:
                raise
    if holder.user_id == user.id:
        return False
    logger.warning("User %s: trial %s cancelled, its card started a trial "
                   "on another account", user.id, sub.get("id"))
    return True


def detach_for_purge(user_id):
    """Account deletion: the account's trial card stays, without it.
    Returns how many rows changed."""
    from backend.models import TrialCard
    return TrialCard.query.filter(TrialCard.user_id == user_id).update(
        {TrialCard.user_id: None}, synchronize_session=False)
