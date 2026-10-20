"""Background node creation for ChatGPT and Claude imports.

The analyze step stashes the analyzed conversations on the server,
encrypted (backend/utils/conversation_stash.py); the confirm step starts
``import_conversations`` with the stash token and the import settings.
The task creates the nodes in committed chunks and reports "done of
total" messages through the Celery task state, which the client polls
via ``GET /api/import/status/<task_id>`` (as for a Twitter import).

Why a task: a 43k-message export took the confirm request an hour (one
KMS call and one flush per node, one transaction for all of it), far
past the 60 s the proxy and the client wait, so the user saw the dialog
come back and pressed Confirm again.

Resuming: the job saves its place after every commit. When the worker
dies mid-run (a deploy, a crash) the task is delivered again
(``acks_late`` + ``reject_on_worker_lost``) and the soft time limit
retries it; both runs keep the task id, so the client's polling follows
them, and each continues from the saved place. Dedup keys make any
overlap a skip, never a duplicate.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app

logger = get_task_logger(__name__)

# HEURISTIC: how many times one import may hit the soft time limit
# (55 min per run) and continue in a new run.
MAX_TIME_LIMIT_RESUMES = 5


@celery.task(bind=True, name="backend.tasks.chat_imports.import_conversations",
             acks_late=True, reject_on_worker_lost=True,
             max_retries=MAX_TIME_LIMIT_RESUMES)
def import_conversations(self, user_id, token, options):
    """options: privacy_level, ai_usage, on_deleted (already resolved by
    the confirm request)."""
    from celery.exceptions import SoftTimeLimitExceeded
    from backend.routes.import_data import run_conversation_import
    from backend.utils import conversation_stash as cs
    from backend.utils.task_owner import UserFacingTaskError

    with flask_app.app_context():
        def progress(done, total):
            self.update_state(state="PROGRESS", meta={
                "user_id": user_id, "done": done, "total": total,
            })

        try:
            result = run_conversation_import(
                user_id, token, self.request.id, options,
                on_progress=progress, resumable=(SoftTimeLimitExceeded,))
        except SoftTimeLimitExceeded:
            if self.request.retries >= self.max_retries:
                cs.release_lock(user_id, self.request.id)
                raise UserFacingTaskError(
                    "The import took too long and stopped. Press Confirm "
                    "Import again to continue where it stopped.")
            logger.warning(
                "Conversation import for user %s hit the time limit; "
                "continuing in a new run", user_id)
            raise self.retry(countdown=5)
        except UserFacingTaskError:
            raise
        except Exception as e:
            # Type only: a database error's text can carry the content.
            logger.error("Conversation import failed for user %s: %s",
                         user_id, type(e).__name__)
            raise

        result["user_id"] = user_id
        return result


@celery.task(name="backend.tasks.chat_imports.sweep_import_stashes")
def sweep_import_stashes():
    """Beat: remove import stashes nobody confirmed (or resumed) within
    their TTL: ChatGPT/Claude conversation stashes and Twitter tweet
    stashes (which were otherwise swept only by the next analyze)."""
    from backend.utils import conversation_stash as cs
    from backend.utils import twitter_archive as ta
    removed = cs.sweep_expired()
    ta.sweep_expired()
    return {"removed": removed}
