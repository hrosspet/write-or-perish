"""Hourly Celery beat task for API spend monitoring (issue #85).

Thin wrapper — the testable logic lives in backend/utils/spend.py.
Disabled unless ANTHROPIC_SPEND_LIMIT_USD is set to a positive value.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils.spend import check_and_alert

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
