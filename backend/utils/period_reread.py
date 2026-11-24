"""A subscription whose stored period has ended is read again from Stripe
(Peter, 2026-11-23).

The account's subscription columns, and with them the period its usage
ceiling is counted over, change only when an event from Stripe arrives
(utils/stripe_billing). When the event of a renewal is late or never
arrives, the account keeps the ended period: a subscriber at the ceiling
stays there past the day the next period started.

A renewal whose payment failed has the same problem (Peter, 2026-11-24):
the account is stored as a late payment (past_due), which gives no
access, and when the person pays later and Stripe's events of that
payment are lost, the account stays without access although it paid.

Hourly (tasks/spend_monitor.reread_periods), these accounts have their
subscription read from Stripe and settled by
stripe_billing.settle_subscription, the function every subscription
event goes through:

* every account whose stored period end has passed by
  PERIOD_REREAD_MARGIN, and whose subscription still runs as far as
  Loore knows;
* every account stored as a late payment, whatever its period end. It is
  asked every hour until Stripe answers otherwise: paid (active, access
  again), or ended (canceled when every retry failed, as Peter sets
  Stripe to do), after which it is not asked again.

Nothing about the ceiling, the carry or the access is decided here.

* One read at Stripe per account and run; at most
  PERIOD_REREAD_MAX_PER_RUN accounts a run, the earliest period end
  first (an account whose period end is not stored comes last).
* Stripe cannot be asked: the run ends there, and the next hour starts
  again. Nothing is tried twice within a run. A read Stripe refuses for
  one account alone (400), or an account that cannot be settled, counts
  as failed for that account, and the run goes on.
* Beside an event of the same account: the subscription is read first,
  then the account is locked (the lock settle_subscription takes) and
  compared with what it was before the read. Changed meanwhile: an event
  was applied, and what was read here may be older, so it is dropped.
* The job only reads at Stripe. Where settling would change something
  there (the cases in which settle_subscription cancels a subscription),
  nothing is written for the account, here or at Stripe; that is left to
  an event.
* Only ids and states are logged.
* Nothing while payments are off.
"""
import logging
from datetime import datetime, timedelta

from backend.extensions import db
from backend.models import User
from backend.utils import stripe_billing

logger = logging.getLogger(__name__)

# INTRODUCED HEURISTIC: how long after the stored period end an account
# is left to Stripe's own event. Stripe starts the next period at that
# moment and sends the event within seconds, sending it again within
# minutes when the first delivery fails; ten minutes keep the job away
# from accounts whose event is on its way. With the hourly run, an
# account waits at most 70 minutes after its period ended.
PERIOD_REREAD_MARGIN = timedelta(minutes=10)

# INTRODUCED HEURISTIC: accounts read per run. Each is one request to
# Stripe, one after the other (about a minute for 200), far under
# Stripe's limit of 100 reads a second. Above the number of subscribers
# the Beta is planned for, so that even when every event of an hour was
# lost no account waits for a second run.
PERIOD_REREAD_MAX_PER_RUN = 200

# The stored states of a subscription that still runs at Stripe as far as
# Loore knows. "ended" never runs again, and "none" is a first payment
# that has not gone through (no period has been paid).
REREAD_STATUSES = ("trialing", "active", "past_due")

# The stored state of a renewal whose payment failed, read every run
# whatever its period end (Peter, 2026-11-24). Stripe's "unpaid" is
# stored as "ended" (stripe_billing.STATUS_MAP), like "canceled", so an
# account whose subscription Stripe marked unpaid is not asked; with
# Stripe set to cancel a subscription when every retry failed, a failed
# renewal reaches "canceled", not "unpaid".
LATE_PAYMENT_STATUSES = ("past_due",)

# What settle_subscription writes from the subscription: an account whose
# values changed between the read at Stripe and the lock had an event
# applied meanwhile.
_COMPARED = (
    "stripe_subscription_id", "stripe_customer_id", "subscription_status",
    "subscription_level", "subscription_period_start",
    "subscription_period_end", "subscription_trial_start",
    "subscription_trial_end", "subscription_cancel_at_period_end",
    "subscription_revoked",
)


def _stored(user):
    return tuple(getattr(user, name) for name in _COMPARED)


def _due(cutoff):
    return db.and_(
        User.stripe_subscription_id.isnot(None),
        db.or_(
            User.subscription_status.in_(LATE_PAYMENT_STATUSES),
            db.and_(
                User.subscription_status.in_(REREAD_STATUSES),
                User.subscription_period_end.isnot(None),
                User.subscription_period_end < cutoff)))


def _is_due(user, now):
    """The same test as _due, on an account already loaded."""
    if not user.stripe_subscription_id:
        return False
    if user.subscription_status in LATE_PAYMENT_STATUSES:
        return True
    return (user.subscription_status in REREAD_STATUSES
            and user.subscription_period_end is not None
            and user.subscription_period_end < now - PERIOD_REREAD_MARGIN)


def due_account_ids(now=None, limit=None):
    """Ids of the accounts to read: those whose stored period end passed
    PERIOD_REREAD_MARGIN ago or earlier and whose subscription still runs
    as far as Loore knows, and those stored as a late payment; the
    earliest period end first, an account without one last."""
    now = now or datetime.utcnow()
    q = (db.session.query(User.id)
         .filter(_due(now - PERIOD_REREAD_MARGIN))
         .order_by(User.subscription_period_end.asc().nullslast(),
                   User.id.asc()))
    if limit is not None:
        q = q.limit(limit)
    return [uid for (uid,) in q.all()]


class StripeChangeRefused(Exception):
    """Settling asked for a call that changes something at Stripe."""


class ReadOnlyGateway:
    """The gateway's reads (its ``retrieve_*`` and ``list_*`` calls) and
    nothing else: any other call raises StripeChangeRefused before it
    reaches the gateway."""

    _READS = ("retrieve_", "list_")

    def __init__(self, gateway):
        self._gateway = gateway

    def __getattr__(self, name):
        if name.startswith(self._READS):
            return getattr(self._gateway, name)

        def refused(*_args, **_kwargs):
            raise StripeChangeRefused(name)
        return refused


class StripeNotReached(Exception):
    """The read at Stripe failed for a reason other than a subscription
    Stripe does not have, or a request Stripe refused for this account
    alone: the run ends."""


def _refused_for_this_account(exc):
    """Stripe answered and refused this one request (400, an invalid
    request): the next account is asked, so one account's error cannot
    stop every later account's read, hour after hour. Any other failure
    (no connection, too many requests, Stripe's own error, a key Stripe
    does not accept) would be the same for every account."""
    return getattr(exc, "http_status", None) == 400


def _read(gateway, sub_id, user_id):
    """(the subscription as Stripe has it, None), or (None, "unknown")
    when Stripe has no such subscription, or (None, "failed") when Stripe
    refused this account's read. Raises StripeNotReached when Stripe
    cannot be asked."""
    try:
        return gateway.retrieve_subscription(sub_id), None
    except Exception as e:  # noqa: BLE001 - one Stripe does not know
        db.session.rollback()
        if stripe_billing._missing(e):
            logger.warning("Period read again: Stripe has no subscription "
                           "%s (user %s); nothing changed", sub_id, user_id)
            return None, "unknown"
        if _refused_for_this_account(e):
            logger.error("Period read again: Stripe refused the read of "
                         "subscription %s (user %s): %s; nothing changed",
                         sub_id, user_id, type(e).__name__)
            return None, "failed"
        raise StripeNotReached(type(e).__name__)


def _reread_one(gateway, user_id, config, now):
    """Read one account's subscription and settle it. Returns what
    happened: "changed", "unchanged", "skipped" (the account is not due
    any more; Stripe was not asked), "event_first" (an event was applied
    between the read and the lock), "unknown" (Stripe has no such
    subscription, or it is not this account's), "failed" (it could not
    be written, or settling it would change something at Stripe; nothing
    of it is kept). Raises StripeNotReached when Stripe cannot be asked.
    Commits."""
    user = db.session.get(User, user_id)
    if user is None:
        return "skipped"
    before = _stored(user)
    status_before = user.subscription_status
    sub_id = user.stripe_subscription_id
    if not _is_due(user, now):
        return "skipped"
    sub, outcome = _read(gateway, sub_id, user_id)
    if outcome is not None:
        return outcome
    try:
        owner = stripe_billing._user_for_subscription(sub or {})
        if owner is None or owner.id != user_id:
            db.session.rollback()
            logger.warning("Period read again: subscription %s is not "
                           "user %s's at Stripe; nothing changed", sub_id,
                           user_id)
            return "unknown"
        # The account's lock, as settle_subscription takes it; then the
        # same account as before the read, or nothing is written.
        db.session.query(User).filter(
            User.id == user_id).with_for_update().one()
        db.session.refresh(user)
        if _stored(user) != before:
            db.session.rollback()
            return "event_first"
        stripe_billing.settle_subscription(
            ReadOnlyGateway(gateway), sub, config, lock=False)
        after = _stored(user)
        status_after = user.subscription_status
        db.session.commit()
    except StripeChangeRefused as e:
        db.session.rollback()
        logger.warning("Period read again: subscription %s of user %s "
                       "needs a change at Stripe (%s); nothing written, "
                       "left to its event", sub_id, user_id, e)
        return "failed"
    except Exception as e:  # noqa: BLE001 - this account waits an hour
        db.session.rollback()
        logger.error("Period read again: subscription %s of user %s not "
                     "applied: %s", sub_id, user_id, type(e).__name__)
        return "failed"
    if after == before:
        return "unchanged"
    logger.info("Period read again: user %s, subscription %s: %s -> %s",
                user_id, sub_id, status_before, status_after)
    return "changed"


def reread_ended_periods(config, now=None):
    """Hourly: read from Stripe the subscription of every account whose
    stored period has ended, or whose renewal payment failed, and settle
    it as its event would. Returns counts. Nothing while payments are
    off."""
    if not config.get("PAYMENTS_ENABLED"):
        return {"status": "off"}
    now = now or datetime.utcnow()
    ids = due_account_ids(now, limit=PERIOD_REREAD_MAX_PER_RUN + 1)
    more = len(ids) > PERIOD_REREAD_MAX_PER_RUN
    ids = ids[:PERIOD_REREAD_MAX_PER_RUN]
    result = {"status": "ok", "due": len(ids), "more_waiting": more,
              "asked": 0, "changed": 0, "unchanged": 0, "skipped": 0,
              "event_first": 0, "unknown": 0, "failed": 0}
    db.session.rollback()
    if not ids:
        return result
    try:
        gateway = stripe_billing.get_gateway(config)
    except stripe_billing.BillingNotConfigured:
        result["status"] = "not_configured"
        return result
    if more:
        logger.warning("Period read again: more than %s accounts are due; "
                       "the rest wait for the next run",
                       PERIOD_REREAD_MAX_PER_RUN)
    for user_id in ids:
        try:
            outcome = _reread_one(gateway, user_id, config, now)
        except StripeNotReached as e:   # the next hour starts again
            result["asked"] += 1
            result["status"] = "stripe_unreachable"
            logger.warning("Period read again: Stripe could not be asked "
                           "(%s) at user %s; the run ends", e, user_id)
            break
        except Exception as e:  # noqa: BLE001 - this account waits an hour
            db.session.rollback()
            logger.error("Period read again: user %s not read: %s",
                         user_id, type(e).__name__)
            outcome = "failed"
        if outcome != "skipped":
            result["asked"] += 1
        result[outcome] += 1
    return result
