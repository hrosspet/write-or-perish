"""Background sync of external content sources (#155 / Download).

fetch_community_archive: pull a donated archive's tweets from the public
Community Archive into ExternalItem rows (deduped per user+source+id).

sync_twitter_bookmarks: pull the user's X bookmarks via their connected
OAuth account (pay-per-use X API; env-gated by X_CLIENT_ID). Refreshes
the access token when expired.
"""
from datetime import datetime, timedelta

from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import ExternalAccount, ExternalItem
from backend.utils.external_content import (
    ca_fetch_tweets, ca_lookup_account, x_fetch_bookmark_pages,
    x_refresh_access_token,
)

logger = get_task_logger(__name__)


def _post_import(user_id, created):
    """After any import/fetch/sync that created items: rebuild the digest
    artifact (topic map the agent reads before searching). The embedding
    sweep picks new items up on its own schedule."""
    if not created:
        return
    from backend.tasks.external_digest import rebuild_external_digest
    rebuild_external_digest.delay(user_id)


def _upsert_items(user_id, source, items):
    """Insert normalized items, skipping per-user duplicates. Returns
    (created, skipped)."""
    existing = {
        row[0] for row in db.session.query(ExternalItem.external_id).filter_by(
            user_id=user_id, source=source).all()
    }
    created = skipped = 0
    for item in items:
        if item["external_id"] in existing:
            skipped += 1
            continue
        row = ExternalItem(
            user_id=user_id,
            source=source,
            external_id=item["external_id"],
            author_handle=item.get("author_handle"),
            url=item.get("url"),
            posted_at=item.get("posted_at"),
        )
        row.set_content(item["content"])
        db.session.add(row)
        existing.add(item["external_id"])
        created += 1
        if created % 200 == 0:
            db.session.commit()
    db.session.commit()
    return created, skipped


@celery.task(name='backend.tasks.external_sync.fetch_community_archive',
             bind=True)
def fetch_community_archive(self, user_id, username, max_items=2000):
    with flask_app.app_context():
        username = (username or "").strip().lstrip("@")
        account_id = ca_lookup_account(username)
        if account_id is None:
            return {"status": "not_found", "username": username}
        created, skipped = _upsert_items(
            user_id, "community_archive",
            ca_fetch_tweets(account_id, username, max_items=max_items),
        )
        logger.info(
            "Community Archive fetch for user %s @%s: %d new, %d known",
            user_id, username, created, skipped)
        _post_import(user_id, created)
        return {"status": "ok", "username": username,
                "created": created, "skipped": skipped}


@celery.task(name='backend.tasks.external_sync.sync_twitter_bookmarks',
             bind=True)
def sync_twitter_bookmarks(self, user_id, max_items=800):
    with flask_app.app_context():
        account = ExternalAccount.query.filter_by(
            user_id=user_id, provider="twitter").first()
        if account is None or not account.get_access_token():
            return {"status": "not_connected"}

        client_id = flask_app.config.get("X_CLIENT_ID")
        # Refresh ahead of expiry when we can
        if (client_id and account.token_expires_at
                and account.token_expires_at
                < datetime.utcnow() + timedelta(minutes=5)
                and account.get_refresh_token()):
            tokens = x_refresh_access_token(
                client_id, account.get_refresh_token())
            account.set_tokens(
                tokens["access_token"], tokens.get("refresh_token"))
            if tokens.get("expires_in"):
                account.token_expires_at = (
                    datetime.utcnow()
                    + timedelta(seconds=int(tokens["expires_in"])))
            db.session.commit()

        # Early-stop pagination (credit saving): bookmarks arrive newest-
        # bookmarked-first, so once a whole page produced nothing new, the
        # rest is already imported — stop instead of paying for the tail.
        # Page-wise (not first-known-id) because a re-bookmarked old tweet
        # jumps to the top and would otherwise mask newer items below it.
        created = skipped = 0
        for page in x_fetch_bookmark_pages(
                account.get_access_token(), account.external_user_id,
                max_items=max_items):
            page_created, page_skipped = _upsert_items(
                user_id, "twitter_bookmark", page)
            created += page_created
            skipped += page_skipped
            if page_created == 0:
                break
        account.last_synced_at = datetime.utcnow()
        db.session.commit()
        logger.info("X bookmarks sync for user %s: %d new, %d known",
                    user_id, created, skipped)
        _post_import(user_id, created)
        return {"status": "ok", "created": created, "skipped": skipped}
