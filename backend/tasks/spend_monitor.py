"""Hourly Celery beat task for API spend monitoring (issue #85).

Thin wrapper — the testable logic lives in backend/utils/spend.py.
Disabled unless ANTHROPIC_SPEND_LIMIT_USD is set to a positive value.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils.spend import (
    check_and_alert, enforce_user_spend_cap, roll_spend_carries)

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
