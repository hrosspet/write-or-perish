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
level (stripe_billing.create_checkout) too.
"""
from datetime import datetime, timedelta

from flask import current_app, has_app_context
from sqlalchemy import func

from backend.extensions import db

KINDS = {
    "checkout": "CHECKOUT_STARTS_PER_HOUR",
    "sync": "BILLING_SYNCS_PER_HOUR",
}
_DEFAULTS = {"checkout": 10, "sync": 20}

TOO_MANY_CHECKOUTS_MESSAGE = ("You started checkout many times in the last "
                              "hour. Please try again in an hour.")


class TooManyBillingRequests(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def limit_for(kind, config=None):
    cfg = config if config is not None else (
        current_app.config if has_app_context() else {})
    value = cfg.get(KINDS[kind])
    return _DEFAULTS[kind] if value is None else int(value)


def check_and_count(user, kind, config=None, now=None):
    """Raise TooManyBillingRequests when *user* made the hour's *kind*
    requests already; otherwise count this one. Commits the count, so a
    request that then fails at Stripe counts too."""
    from backend.models import BillingRequest
    now = now or datetime.utcnow()
    since = now - timedelta(hours=1)
    made = db.session.query(func.count(BillingRequest.id)).filter(
        BillingRequest.user_id == user.id,
        BillingRequest.kind == kind,
        BillingRequest.created_at >= since).scalar() or 0
    if made >= limit_for(kind, config):
        raise TooManyBillingRequests(kind)
    db.session.add(BillingRequest(user_id=user.id, kind=kind,
                                  created_at=now))
    db.session.commit()
