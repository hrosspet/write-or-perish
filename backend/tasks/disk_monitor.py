"""Hourly Celery beat task: email the owner when the server disk is nearly
full. Thin wrapper; the logic lives in backend/utils/disk_alert.py.
Disabled unless DISK_ALERT_ENABLED is true.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils.disk_alert import check_disk_and_alert

logger = get_task_logger(__name__)


@celery.task(name='backend.tasks.disk_monitor.check_disk_usage')
def check_disk_usage():
    with flask_app.app_context():
        result = check_disk_and_alert(flask_app.config)
        if result.get("sent"):
            logger.warning("Disk alert sent: %s", result)
        else:
            logger.info("Disk check: %s", result)
        return result
