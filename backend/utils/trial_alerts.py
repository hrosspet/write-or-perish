"""A daily count of the trials that started, with an email to the admin
when it is unusually high.

Each trial may use up to SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD of model cost
before anything is paid, and a trial is given once per account and once
per card (utils/trial_cards). The number of trials that start in a day
is otherwise not watched. The hourly task (tasks/spend_monitor) counts
the accounts whose trial started in the last 24 hours and emails the
admin once per UTC day when there are more than TRIALS_STARTED_ALERT.
Nothing is stopped or cancelled: the email is for the admin to look.

An alpha tester's free month reaches Stripe as a trial too; it is given
by hand and is not counted.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from backend.extensions import db

logger = logging.getLogger(__name__)

# HEURISTIC: trials started in 24 hours above which the admin is emailed.
# The accounts that signed up before the Beta numbered 26 over several
# weeks, so a usual day has a handful of new trials at most, and 25 in
# one day is several times that. A day on which a whole round of
# invitations is taken up at once may pass it; the email is then one
# message and nothing else happens. With the trial's ceiling at $30,
# 25 trials can cost $750 at most.
TRIALS_STARTED_ALERT = 25
TRIALS_STARTED_WINDOW = timedelta(hours=24)


def trials_started_since(since):
    """How many accounts' trials started at or after *since* (naive UTC),
    the alpha testers' free month left out."""
    from backend.models import User
    return db.session.query(User.id).filter(
        User.subscription_trial_start >= since,
        User.alpha_free_granted_at.is_(None)).count()


def check_trials_started(config, now=None, send_email=None):
    """Email the admin when more than TRIALS_STARTED_ALERT trials started
    in the last 24 hours, at most once per UTC day. Commits the day's
    record after the email went out; a failed email is tried again at the
    next run. Returns a summary dict (the Celery task's result).

    `send_email` is injectable for tests; defaults to the real sender."""
    from backend.models import TrialAlert
    if not config.get("PAYMENTS_ENABLED"):
        return {"status": "disabled"}
    now = now or datetime.utcnow()
    count = trials_started_since(now - TRIALS_STARTED_WINDOW)
    summary = {"status": "ok", "trials": count,
               "alert_above": TRIALS_STARTED_ALERT, "alerted": False}
    if count <= TRIALS_STARTED_ALERT:
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
            trials=count, alert_above=TRIALS_STARTED_ALERT,
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
