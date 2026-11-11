"""Celery side of the user data purge (#268); the work is in
backend/utils/user_purge.py.

``process_user_data_purges`` (beat, every minute) claims the jobs that
are due: a user's request whose grace period has ended, an admin purge,
or a job whose runner stopped sending heartbeats (a deploy or a crash).
Account deletions (#269) are the same jobs with ``delete_account``.
It dispatches ``run_user_data_purge`` with a claim token; a runner whose
token no longer matches the job's stops at its next heartbeat.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.utils import user_purge

logger = get_task_logger(__name__)


@celery.task(name="backend.tasks.user_purge.run_user_data_purge",
             bind=True, max_retries=None)
def run_user_data_purge(self, job_id, token):
    """Run one claimed purge job. While the user's tasks are still
    running, look again later under the same claim (the runner bounds
    the wait by PURGE_MAX_WAIT)."""
    with flask_app.app_context():
        outcome = user_purge.run_purge_job(job_id, token)
    if outcome == "wait":
        raise self.retry(countdown=user_purge.PURGE_WAIT_RETRY_SECONDS)
    return {"job_id": job_id, "outcome": outcome}


@celery.task(name="backend.tasks.user_purge.cancel_batches_read_before",
             bind=True, max_retries=3, default_retry_delay=60)
def cancel_batches_read_before(self, user_id, job_id, cutoff_iso):
    """After a "Delete all my content" (#590): ask the providers to cancel
    the pending batches that carry only the user's work read before the
    request. Off the request, so a slow provider never holds the user's
    click; the collectors drop and cost whatever still comes back."""
    from datetime import datetime
    with flask_app.app_context():
        n = user_purge.cancel_batches_read_before(
            user_id, job_id, datetime.fromisoformat(cutoff_iso))
    return {"user_id": user_id, "cancelled": n}


def dispatch(job_id, token):
    """Send the runner for a claimed job; the token is the task id."""
    run_user_data_purge.apply_async(args=(job_id, token), task_id=token)


@celery.task(name="backend.tasks.user_purge.process_user_data_purges")
def process_user_data_purges():
    """Beat: start due purges and account deletions (#269), resume
    interrupted ones, and forget deleted accounts' handles whose
    reservation is over."""
    from backend.utils.account_deletion import release_expired_usernames
    with flask_app.app_context():
        started = user_purge.dispatch_due_jobs(dispatch)
        released = release_expired_usernames()
    if started:
        logger.info("user data purge: dispatched jobs %s", started)
    return {"dispatched": started, "usernames_released": released}
