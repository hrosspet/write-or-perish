"""Celery side of the user data purge (#268); the work is in
backend/utils/user_purge.py.

``process_user_data_purges`` (beat, every minute) claims the jobs that
are due: a user's request whose grace period has ended, an admin purge,
or a job whose runner stopped sending heartbeats (a deploy or a crash).
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


def dispatch(job_id, token):
    """Send the runner for a claimed job; the token is the task id."""
    run_user_data_purge.apply_async(args=(job_id, token), task_id=token)


@celery.task(name="backend.tasks.user_purge.process_user_data_purges")
def process_user_data_purges():
    """Beat: start due purges and resume interrupted ones."""
    with flask_app.app_context():
        started = user_purge.dispatch_due_jobs(dispatch)
    if started:
        logger.info("user data purge: dispatched jobs %s", started)
    return {"dispatched": started}
