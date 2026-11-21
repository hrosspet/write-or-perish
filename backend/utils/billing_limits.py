"""Limits on starting a checkout and on the return page's sync (#697).

With nobody approving accounts, any new account reaches the billing
routes, and each checkout started and each sync makes calls to Stripe
(and a checkout stores a row). So an account may start
CHECKOUT_STARTS_PER_HOUR checkouts and make BILLING_SYNCS_PER_HOUR syncs
an hour; past that the route answers 429 before calling Stripe. The same
idiom as the discount-code field (utils/discount_codes): one row per
request, who and when only (models.BillingRequest), counted over the last
hour. Every request that reaches Stripe counts, also one that then fails
there, and a checkout that reuses the account's open session for the same
level (stripe_billing.create_checkout) too. Opening the billing page
(Stripe's portal) is held to BILLING_PORTALS_PER_HOUR the same way.

Calls to the paid X API that an account starts are counted the same way,
over a day (Peter's rule for money: paid per use as it comes, never
without a bound): X_BOOKMARK_SYNCS_PER_DAY "Sync now" presses for X
bookmarks and X_INVITE_LOOKUPS_PER_DAY X handles looked up for invites.
"""
from datetime import datetime, timedelta

from flask import current_app, has_app_context
from sqlalchemy import func

from backend.extensions import db

# "Sync now" for X bookmarks (routes/external.twitter_sync): each sync
# reads at least X's first page of bookmarks (10 posts, about 0.05 USD on
# Loore's X bill). Twenty-four a day is one an hour on average, far above
# a person who saves bookmarks on X and presses "Sync now" in Loore after;
# the nightly sync is not counted.
X_BOOKMARK_SYNCS_PER_DAY = 24
# An invite for an X handle (utils/referrals.create_invite) looks the
# handle up: the Community Archive, then one X user read (0.01 USD). An
# account makes one invite a month; ten lookups a day leave room for
# typos and for an invite withdrawn and made again.
X_INVITE_LOOKUPS_PER_DAY = 10

# Each kind: the config setting that overrides its limit (None: none), and
# the limit without one.
KINDS = {
    "checkout": "CHECKOUT_STARTS_PER_HOUR",
    "sync": "BILLING_SYNCS_PER_HOUR",
    "portal": "BILLING_PORTALS_PER_HOUR",
    "x_bookmark_sync": None,
    "x_invite_lookup": None,
}
# HEURISTIC (portal): Stripe's billing page opened 20 times an hour. Its
# sessions last minutes, so each press makes a new one; a person changing
# a card or cancelling opens it a few times.
_DEFAULTS = {"checkout": 10, "sync": 20, "portal": 20,
             "x_bookmark_sync": X_BOOKMARK_SYNCS_PER_DAY,
             "x_invite_lookup": X_INVITE_LOOKUPS_PER_DAY}
# The time a kind's requests are counted over; an hour when not named.
_WINDOWS = {"x_bookmark_sync": timedelta(days=1),
            "x_invite_lookup": timedelta(days=1)}

TOO_MANY_CHECKOUTS_MESSAGE = ("You started checkout many times in the last "
                              "hour. Please try again in an hour.")
TOO_MANY_PORTALS_MESSAGE = ("You opened the billing page many times in the "
                            "last hour. Please try again in an hour.")
TOO_MANY_X_SYNCS_MESSAGE = ("You synced X bookmarks by hand many times in "
                            "the last day. They still sync every night; "
                            "please try again later.")
TOO_MANY_X_LOOKUPS_MESSAGE = ("You looked up many X handles for invites in "
                              "the last day. Please try again later.")


class TooManyBillingRequests(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def limit_for(kind, config=None):
    cfg = config if config is not None else (
        current_app.config if has_app_context() else {})
    value = cfg.get(KINDS[kind]) if KINDS[kind] else None
    return _DEFAULTS[kind] if value is None else int(value)


def _lock_account(user):
    """Lock the account's row until this transaction ends (the idiom of
    stripe_billing.create_checkout and settle_subscription), so two
    requests of one account count and add one after the other. sqlite
    ignores it; Postgres waits."""
    from backend.models import User
    db.session.query(User).filter(User.id == user.id).with_for_update().one()


def check_and_count(user, kind, config=None, now=None):
    """Raise TooManyBillingRequests when *user* made the *kind* requests
    its window allows already (an hour, or the kind's _WINDOWS);
    otherwise count this one. One step under the account's row lock:
    requests of one account that arrive together are counted one after
    the other, never all against the same count. Commits the count (which
    ends the lock), so a request that then fails at Stripe or X counts
    too."""
    from backend.models import BillingRequest
    now = now or datetime.utcnow()
    since = now - _WINDOWS.get(kind, timedelta(hours=1))
    _lock_account(user)
    made = db.session.query(func.count(BillingRequest.id)).filter(
        BillingRequest.user_id == user.id,
        BillingRequest.kind == kind,
        BillingRequest.created_at >= since).scalar() or 0
    if made >= limit_for(kind, config):
        db.session.rollback()   # ends the lock at once
        raise TooManyBillingRequests(kind)
    db.session.add(BillingRequest(user_id=user.id, kind=kind,
                                  created_at=now))
    db.session.commit()
