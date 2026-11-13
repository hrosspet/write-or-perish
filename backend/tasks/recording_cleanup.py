"""Daily clean-up of recordings that never received audio
(utils/recording_limits.remove_empty_recordings)."""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils import recording_limits

logger = get_task_logger(__name__)


@celery.task
def remove_abandoned_empty_recordings():
    """Beat (daily): recordings without audio older than
    EMPTY_SESSION_REMOVED_AFTER, at most EMPTY_SESSIONS_REMOVED_PER_RUN."""
    with flask_app.app_context():
        removed = recording_limits.remove_empty_recordings()
        if removed:
            logger.info("Removed %d recordings that never received audio",
                        removed)
        return {"removed": removed}
