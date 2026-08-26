"""Background node creation for large archive imports.

``import_twitter_archive`` streams the rows the analyze step stashed on
disk (see backend/utils/twitter_archive.py), creates nodes in committed
batches, and reports progress through the Celery task state so the
frontend can poll ``GET /api/import/status/<task_id>``.

Why a task: a 60k-tweet archive means tens of thousands of nodes, each
with its own KMS-wrapped DEK. That is minutes of work — far past what a
request should hold open, and it used to run inside the confirm request
with the whole tweet list in memory twice (request body + Python).

Progress meta always carries ``user_id`` so the status endpoint can
refuse to leak another user's import.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app

logger = get_task_logger(__name__)

BATCH_SIZE = 500


@celery.task(bind=True, name="backend.tasks.imports.import_twitter_archive")
def import_twitter_archive(self, user_id, token, options):
    """options: import_type, include_replies, privacy_level, ai_usage,
    on_deleted (already resolved by the confirm request)."""
    from backend.extensions import db
    from backend.routes.import_data import create_twitter_nodes
    from backend.utils import twitter_archive as ta

    with flask_app.app_context():
        path = ta.stash_path(user_id, token)
        if path is None or not path.exists():
            raise RuntimeError("Import data expired — please upload the archive again.")

        total = ta.stash_count(path)

        def progress(done):
            self.update_state(state="PROGRESS", meta={
                "user_id": user_id, "done": done, "total": total,
            })

        progress(0)
        try:
            result = create_twitter_nodes(
                user_id=user_id,
                rows=ta.stash_iter(path),
                total=total,
                import_type=options.get("import_type", "separate_nodes"),
                include_replies=bool(options.get("include_replies", False)),
                privacy_level=options.get("privacy_level", "private"),
                ai_usage=options.get("ai_usage", "none"),
                on_deleted=options.get("on_deleted"),
                batch_size=BATCH_SIZE,
                on_progress=progress,
            )
        except Exception:
            db.session.rollback()
            logger.exception("Twitter import failed for user %s", user_id)
            raise
        finally:
            ta.stash_delete(path)

        result["user_id"] = user_id
        return result
