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


def prefill_community_archive_impl(user_id, handle, options, update_state=None):
    """Fetch @handle's tweets from the Community Archive into the user's
    account (origin="twitter", private, AI-readable) and pin the user to
    the BATCH profile pipeline before the import's profile handoff runs,
    so a bootstrapped corpus never triggers a synchronous (full-price)
    build. Runs inside an app context; testable without Celery."""
    from backend.extensions import db
    from backend.models import User
    from backend.routes.import_data import create_twitter_nodes
    from backend.utils import twitter_archive as ta
    from backend.utils import community_archive as ca

    def state(stage, done, total):
        if update_state:
            update_state(state="PROGRESS", meta={
                "user_id": user_id, "stage": stage, "done": done,
                "total": total, "handle": handle,
            })

    user = User.query.get(user_id)
    if not user:
        raise RuntimeError(f"User {user_id} not found")
    account = ca.fetch_account(handle)
    if not account:
        raise ca.CommunityArchiveError(
            f"@{handle} is not in the Community Archive")
    expected = account.get("num_tweets") or 0
    state("fetching", 0, expected)

    rows, seen = [], set()
    for raw in ca.iter_tweets(account["username"],
                              on_page=lambda n: state("fetching", n, expected)):
        if raw["tweet_id"] in seen:
            continue
        seen.add(raw["tweet_id"])
        row = ta.compact_row(ca.to_export_entry(raw)["tweet"])
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: ta._sort_key(r["created_at"]))
    total = len(rows)
    if total == 0:
        raise ca.CommunityArchiveError(f"@{handle}: no own tweets found")

    # Pin BEFORE create_twitter_nodes: its profile handoff consults
    # use_batch_for_user and must route to the seeder, not the sync task.
    user.profile_force_batch = True
    db.session.commit()

    state("importing", 0, total)
    try:
        result = create_twitter_nodes(
            user_id=user_id,
            rows=iter(rows),
            total=total,
            import_type="separate_nodes",
            include_replies=bool(options.get("include_replies", True)),
            privacy_level="private",
            ai_usage=options.get("ai_usage", "chat"),
            on_deleted=None,
            batch_size=BATCH_SIZE,
            on_progress=lambda done: state("importing", done, total),
        )
    except Exception:
        db.session.rollback()
        raise
    result.update({
        "user_id": user_id, "handle": account["username"],
        "total": total, "stage": "done",
        "profile_batch_queued": bool(
            User.query.get(user_id).profile_needs_full_regen),
    })
    return result


@celery.task(bind=True, name="backend.tasks.imports.prefill_community_archive")
def prefill_community_archive(self, user_id, handle, options):
    """Admin pre-fill (see prefill_community_archive_impl)."""
    with flask_app.app_context():
        try:
            return prefill_community_archive_impl(
                user_id, handle, options or {}, update_state=self.update_state)
        except Exception:
            logger.exception("Community Archive pre-fill failed for user %s (@%s)",
                             user_id, handle)
            raise
