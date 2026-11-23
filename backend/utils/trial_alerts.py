"""Trials of accounts that were not prepared in advance: a daily count,
an email to the admin above one number, and the waiting list from a
higher one.

Peter, 2026-11-23: "I'd limit number of trials for accounts outside of CA
per day in the proposed two level limit. One -> email me. A higher one ->
hard stop, put them on a waiting list as it was in Alpha."

Each trial may use up to SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD of model cost
before anything is paid, and a trial is given once per account and once
per card (utils/trial_cards). Both levels look at the last 24 hours and
only at accounts that were not prepared in advance (``prepared_ahead``).
Nothing here runs while payments are off.

* The email (``check_trials_started``, hourly from tasks/spend_monitor):
  more than OUTSIDE_TRIALS_EMAIL_PER_DAY such accounts started a trial in
  the last 24 hours. Once per UTC day, counts only, nothing is stopped.
* The waiting list (``held_at_gate``, from the request gate through
  utils/approval.let_in_at_gate): from OUTSIDE_TRIALS_HOLD_PER_DAY places
  taken in the last 24 hours, a new account that was not prepared in
  advance is not let in at its first request. It waits for the admin's
  approval exactly as every new account did before payments: both apps
  show the waiting page, the admin gets the "New signup" email that asks
  for approval, and Activate (or Activate & Welcome) in the Users tab
  lets it in to the step where the trial starts. It keeps waiting when
  the day's count falls again (``User.approval_hold_at``): the admin
  decides, as before payments.

A place (``outside_places_since``) is such an account that started a
trial in the window, or that the gate let in during the window and that
has not started one yet. The second kind is counted because the account
is decided at the gate and its trial starts later, at Stripe: counting
only the trials already started would let in any number of accounts in
the minutes before their trials begin. An account that is let in is
never put back: the count is taken once, at its first request.

Never held: an account prepared in advance; an account that is approved
already (by the admin, or let in earlier); anything while payments are
off.

An alpha tester's free month reaches Stripe as a trial too; it is given
by hand and is not counted.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, text
from sqlalchemy.exc import IntegrityError

from backend.extensions import db

logger = logging.getLogger(__name__)

# HEURISTICS (both read from the environment, backend/config.py; 0 turns
# the level off).
# The email: more than this many trials of accounts not prepared in
# advance, started in 24 hours. 10 is Peter's number (2026-11-23). The
# accounts that signed up by themselves before the Beta numbered 26 over
# several weeks, so more than 10 in one day is unusual; the email is one
# message a day and stops nothing.
OUTSIDE_TRIALS_EMAIL_PER_DAY = 10
# The waiting list: from this many places taken in 24 hours. Twice the
# email's number; with the trial's ceiling at $30, 20 trials can cost
# $600 at most before a first payment.
OUTSIDE_TRIALS_HOLD_PER_DAY = 20
TRIALS_STARTED_WINDOW = timedelta(hours=24)

# The first key of the advisory lock under which a new account's place is
# counted and taken (as utils/todo_lock: a fixed number, so it cannot
# collide with another advisory lock). One lock for all accounts: the
# count is over all of them.
_GATE_LOCK_NAMESPACE = 738001


def _config(config):
    if config is not None:
        return config
    from flask import current_app
    return current_app.config


def _number(config, name, default):
    value = _config(config).get(name)
    try:
        return int(default if value is None else value)
    except (TypeError, ValueError):
        return default


def email_above(config=None):
    return _number(config, "OUTSIDE_TRIALS_EMAIL_PER_DAY",
                   OUTSIDE_TRIALS_EMAIL_PER_DAY)


def hold_from(config=None):
    return _number(config, "OUTSIDE_TRIALS_HOLD_PER_DAY",
                   OUTSIDE_TRIALS_HOLD_PER_DAY)


def prepared_ahead(user):
    """*user* was prepared in advance: the admin's Community Archive or X
    API pre-fill, a bundle load or scripts/load_user.py made it ready
    ahead of its owner. Each of them sets ``prefilled_at`` (every time)
    and ``prefilled_handle``; accounts pre-filled before ``prefilled_at``
    existed hold only the handle, and "Delete all my content" clears only
    the handle, so either one counts. The same test as the "Prepared in
    advance" line of the admin's signup email (utils/email)."""
    return bool(user is not None
                and (user.prefilled_handle or user.prefilled_at))


def _not_prepared_clause():
    from backend.models import User
    return and_(User.prefilled_handle.is_(None), User.prefilled_at.is_(None))


def outside_trials_started_since(since):
    """How many accounts not prepared in advance started a trial at or
    after *since* (naive UTC), the alpha testers' free month left out."""
    from backend.models import User
    return db.session.query(User.id).filter(
        User.subscription_trial_start >= since,
        User.alpha_free_granted_at.is_(None),
        _not_prepared_clause()).count()


def outside_places_since(since):
    """The places taken at or after *since*: accounts not prepared in
    advance that started a trial, and those the gate let in that have not
    started one yet."""
    from backend.models import User
    return db.session.query(User.id).filter(
        User.alpha_free_granted_at.is_(None),
        _not_prepared_clause(),
        or_(User.subscription_trial_start >= since,
            and_(User.subscription_trial_start.is_(None),
                 User.approved.is_(True),
                 User.let_in_at >= since))).count()


def waits_for_approval(user):
    """The gate put *user* on the waiting list and the admin has not
    approved it since."""
    return bool(user is not None and not user.approved
                and getattr(user, "approval_hold_at", None) is not None)


def _lock_gate():
    """One new account at a time counts the places and takes one: a
    PostgreSQL advisory lock for the rest of the transaction, released by
    the commit or rollback (as utils/llm_nodes._lock_new_replies; its
    holder counts, writes one row and commits). Other databases (SQLite
    in the tests) take no lock; the count still runs."""
    if db.session.get_bind().dialect.name != "postgresql":
        return
    db.session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, 0)"),
        {"namespace": _GATE_LOCK_NAMESPACE})


def held_at_gate(user, now=None, config=None):
    """The request gate asks, for an account it is about to let in: does
    it wait for the admin's approval instead? True: it waits (set and
    committed here the first time). False: the caller lets it in within
    the same transaction, so its place is taken before the next account
    counts. Payments on only; the caller checked the rest (never
    approved, not blocked, not deleted)."""
    # The account itself, not flask-login's proxy for it.
    user = getattr(user, "_get_current_object", lambda: user)()
    if waits_for_approval(user):
        return True
    limit = hold_from(config)
    if limit <= 0 or prepared_ahead(user):
        return False
    now = now or datetime.utcnow()
    _lock_gate()
    # Another request of the same account may have decided meanwhile.
    db.session.refresh(user)
    if user.approved:
        return False
    if waits_for_approval(user):
        return True
    if outside_places_since(now - TRIALS_STARTED_WINDOW) < limit:
        return False
    user.approval_hold_at = now
    db.session.commit()
    logger.warning(
        "New account %s waits for approval: %s places taken in 24 hours",
        user.id, limit)
    return True


def check_trials_started(config, now=None, send_email=None):
    """Email the admin when more than OUTSIDE_TRIALS_EMAIL_PER_DAY
    accounts not prepared in advance started a trial in the last 24
    hours, at most once per UTC day. Commits the day's record after the
    email went out; a failed email is tried again at the next run.
    Returns a summary dict (the Celery task's result).

    `send_email` is injectable for tests; defaults to the real sender."""
    from backend.models import TrialAlert
    if not config.get("PAYMENTS_ENABLED"):
        return {"status": "disabled"}
    above = email_above(config)
    if above <= 0:
        return {"status": "off"}
    now = now or datetime.utcnow()
    since = now - TRIALS_STARTED_WINDOW
    count = outside_trials_started_since(since)
    summary = {"status": "ok", "trials": count,
               "alert_above": above, "alerted": False}
    if count <= above:
        return summary
    day = now.strftime("%Y-%m-%d")
    if TrialAlert.query.filter_by(day=day).first() is not None:
        summary["status"] = "already_alerted"
        return summary
    if send_email is None:
        from backend.utils.email import send_trials_started_email
        send_email = send_trials_started_email
    try:
        send_email(
            to_email=config.get("SPEND_ALERT_EMAIL") or "signup@loore.org",
            trials=count, alert_above=above,
            places=outside_places_since(since), hold_from=hold_from(config),
            trial_limit_usd=float(
                config.get("SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD") or 0))
    except Exception:
        # Not recorded as sent: the next run tries again.
        logger.exception("Failed to send the trials-started email")
        summary["status"] = "email_failed"
        return summary
    db.session.add(TrialAlert(day=day, trials=count))
    try:
        db.session.commit()
    except IntegrityError:
        # Another run recorded the day a moment ago.
        db.session.rollback()
    summary["alerted"] = True
    return summary
