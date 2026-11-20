"""Celery side of the deletion of pre-filled accounts nobody claimed
(#685); the work is in backend/utils/unclaimed_accounts.py.

``delete_unclaimed_accounts`` (beat, nightly) is a dry run unless
UNCLAIMED_ACCOUNT_DELETION_ENABLED: it logs what it would delete. On, it
schedules the account deletion (#269) of at most MAX_DELETIONS_PER_RUN due
accounts, due at once; the purge beat (tasks/user_purge.py) runs each
within a minute.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils import unclaimed_accounts

logger = get_task_logger(__name__)


@celery.task(name="backend.tasks.unclaimed_accounts.delete_unclaimed_accounts")
def delete_unclaimed_accounts():
    """Beat: one pass over the pre-filled accounts. Returns counts and
    ids (no handles)."""
    with flask_app.app_context():
        return unclaimed_accounts.run()
