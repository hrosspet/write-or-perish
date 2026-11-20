"""Invites (#439): an unused invite expires INVITE_EXPIRY_DAYS after it is
made, and the X handle or email address it names is deleted then
(utils/referrals.expire_due). Hourly; one indexed query when nothing is
due."""
import logging

from backend.celery_app import celery, flask_app

logger = logging.getLogger(__name__)


@celery.task(name="backend.tasks.invites.expire_invites")
def expire_invites():
    with flask_app.app_context():
        from backend.utils.referrals import expire_due
        expired = expire_due()
        if expired:
            logger.info("Invites expired: %d", expired)
        return {"expired": expired}
