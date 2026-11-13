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
keyed hash of it (HMAC-SHA256 under the app's SECRET_KEY) and the date
(models.TrialCard), never the fingerprint itself.

A trial whose card already started one on another account is cancelled
at once, the way #649 cancels a second trial of one account
(stripe_billing.cancel_duplicate: nothing was charged during a trial, so
nothing is refunded). The account then has no trial, and may subscribe
without one: the first payment is due at checkout.

A deterrent, not a guarantee. A payment method without a card
fingerprint (Link, other methods Managed Payments offers) is not
checked. Apple Pay and Google Pay pay with a device card number whose
fingerprint differs from the card's own, so one card through a wallet
and typed in counts as two cards. A subscription without a trial is
never affected (the same person paying for a second account), and the
alpha testers' free month, which Checkout gives as a trial too, is
neither checked nor recorded: it is given by hand.

An account deletion keeps the row without the account
(account_deletion.delete_identity), as the anonymous mark that this card
had its trial. Changing SECRET_KEY forgets every mark.
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
    key = cfg.get("SECRET_KEY")
    if not key:
        return None
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, _HASH_CONTEXT + fingerprint.encode(),
                    hashlib.sha256).hexdigest()


def card_fingerprint(gateway, sub):
    """The fingerprint of the card *sub* (a subscription read from
    Stripe) charges, or None: no default payment method, or one that is
    not a card."""
    pm = sub.get("default_payment_method")
    if not pm:
        return None
    if not isinstance(pm, dict):
        pm = gateway.retrieve_payment_method(pm) or {}
    card = pm.get("card") or {}
    return card.get("fingerprint") or None


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
    digest = card_hash(card_fingerprint(gateway, sub), config)
    if digest is None:
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
