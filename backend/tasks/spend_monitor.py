"""Hourly Celery beat task for API spend monitoring (issue #85).

Thin wrapper — the testable logic lives in backend/utils/spend.py.
Disabled unless ANTHROPIC_SPEND_LIMIT_USD is set to a positive value.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils.spend import (
    check_and_alert, enforce_user_spend_cap, roll_spend_carries)
from backend.utils.period_reread import reread_ended_periods
from backend.utils.trial_alerts import check_trials_started

logger = get_task_logger(__name__)


@celery.task(name='backend.tasks.spend_monitor.check_api_spend')
def check_api_spend():
    with flask_app.app_context():
        result = check_and_alert(flask_app.config)
        if result.get("fired"):
            logger.warning("Spend alert(s) fired: %s", result)
        else:
            logger.info("Spend check: %s", result)
        return result


@celery.task(name='backend.tasks.spend_monitor.enforce_user_cap')
def enforce_user_cap(user_id):
    """Per-user monthly hard-cap check, dispatched after each cost log
    (issue #85 follow-up). No-op unless PER_USER_MONTHLY_LIMIT_USD is set."""
    with flask_app.app_context():
        result = enforce_user_spend_cap(user_id, flask_app.config)
        if result.get("blocked"):
            logger.warning("Per-user cap: %s", result)
        else:
            logger.info("Per-user cap check: %s", result)
        return result


@celery.task(name='backend.tasks.spend_monitor.roll_carries')
def roll_carries():
    """Hourly: roll each user's spend-cap carry into the new month (#594)
    and cap the users whose carry alone reaches their limit. Only users not
    yet rolled this month are touched, so a run after the month's first
    finds only the accounts created since."""
    with flask_app.app_context():
        result = roll_spend_carries(flask_app.config)
        if result.get("capped_by_carry"):
            logger.warning("Spend carry: %s", result)
        else:
            logger.info("Spend carry: %s", result)
        return result


@celery.task(name='backend.tasks.spend_monitor.check_trials')
def check_trials():
    """Hourly: email the admin, at most once a day, when more accounts
    not prepared in advance started a trial in the last 24 hours than
    OUTSIDE_TRIALS_EMAIL_PER_DAY (utils/trial_alerts).
    Nothing while payments are off."""
    with flask_app.app_context():
        result = check_trials_started(flask_app.config)
        if result.get("alerted"):
            logger.warning("Trials started: %s", result)
        else:
            logger.info("Trials started: %s", result)
        return result


@celery.task(name='backend.tasks.spend_monitor.reread_periods')
def reread_periods():
    """Hourly: a subscription whose stored period has ended is read again
    from Stripe and settled as its event would be, so a renewal whose
    event is late still starts the account's next period
    (utils/period_reread). Not retried: the next hour starts again.
    Nothing while payments are off."""
    with flask_app.app_context():
        result = reread_ended_periods(flask_app.config)
        if result.get("status") not in ("ok", "off") \
                or result.get("more_waiting"):
            logger.warning("Periods read again: %s", result)
        else:
            logger.info("Periods read again: %s", result)
        return result
