"""Background sync of external content sources (#155 / Download).

fetch_community_archive: pull a donated archive's tweets from the public
Community Archive into ExternalItem rows (deduped per user+source+id).

sync_twitter_bookmarks: pull the user's X bookmarks via their connected
OAuth account (pay-per-use X API; env-gated by X_CLIENT_ID). Refreshes
the access token when expired.
"""
from datetime import datetime, timedelta

import requests
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import APICostLog, ExternalAccount, ExternalItem
from backend.utils.external_content import (
    ca_fetch_tweets, ca_lookup_account, x_fetch_bookmark_pages,
    x_refresh_access_token,
)

logger = get_task_logger(__name__)

# X API pay-per-use price per bookmarks request, in microdollars
# ($0.005/request as of 2026-07 — hardcoded; update if X reprices).
X_REQUEST_COST_MICRODOLLARS = 5000


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


def _mark_revoked(account, why):
    """X rejected the account's tokens — user revoked the app, or the
    rotating refresh-token family died. Park the account (nightly sync
    skips it; the import page shows a reconnect state) instead of
    retrying forever."""
    account.revoked_at = datetime.utcnow()
    db.session.commit()
    logger.warning("X account for user %s marked revoked (%s)",
                   account.user_id, why)
    return {"status": "revoked"}


@celery.task(name='backend.tasks.external_sync.sync_twitter_bookmarks',
             bind=True)
def sync_twitter_bookmarks(self, user_id, max_items=800):
    with flask_app.app_context():
        account = ExternalAccount.query.filter_by(
            user_id=user_id, provider="twitter").first()
        if account is None or not account.get_access_token():
            return {"status": "not_connected"}
        if account.revoked_at is not None:
            return {"status": "revoked"}

        client_id = flask_app.config.get("X_CLIENT_ID")
        # Refresh ahead of expiry when we can
        if (client_id and account.token_expires_at
                and account.token_expires_at
                < datetime.utcnow() + timedelta(minutes=5)
                and account.get_refresh_token()):
            try:
                tokens = x_refresh_access_token(
                    client_id, account.get_refresh_token())
            except requests.HTTPError as exc:
                code = (exc.response.status_code
                        if exc.response is not None else None)
                # 400/401 = dead grant (revoked / rotated away underneath
                # us). Anything else (429, 5xx) is transient — surface it
                # and let the next scheduled sync retry.
                if code in (400, 401):
                    return _mark_revoked(account, f"refresh HTTP {code}")
                raise
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
        created = skipped = requests_made = 0
        try:
            for page in x_fetch_bookmark_pages(
                    account.get_access_token(), account.external_user_id,
                    max_items=max_items):
                requests_made += 1  # one yielded page == one paid request
                page_created, page_skipped = _upsert_items(
                    user_id, "twitter_bookmark", page)
                created += page_created
                skipped += page_skipped
                if page_created == 0:
                    break
        except requests.HTTPError as exc:
            code = (exc.response.status_code
                    if exc.response is not None else None)
            # 401 = token invalidated without a refresh in between. 403 is
            # NOT revocation (usually an API-tier/permissions problem on
            # our side) — let it raise so it shows up as an operator error.
            if code == 401:
                return _mark_revoked(account, "bookmarks fetch HTTP 401")
            raise
        account.last_synced_at = datetime.utcnow()
        if requests_made:
            db.session.add(APICostLog(
                user_id=user_id,
                model_id="x-api/bookmarks",
                request_type="x_bookmark_sync",
                input_tokens=0,
                output_tokens=0,
                cost_microdollars=(
                    requests_made * X_REQUEST_COST_MICRODOLLARS),
            ))
        db.session.commit()
        logger.info("X bookmarks sync for user %s: %d new, %d known, "
                    "%d API requests", user_id, created, skipped,
                    requests_made)
        _post_import(user_id, created)
        return {"status": "ok", "created": created, "skipped": skipped,
                "requests": requests_made}


@celery.task(name='backend.tasks.external_sync.sync_all_twitter_bookmarks')
def sync_all_twitter_bookmarks():
    """Nightly fan-out (beat): refresh bookmarks for every connected,
    non-revoked X account. Runs BEFORE anything context-side is rebuilt —
    a change in bookmarks warrants a digest/pre-selection refresh even
    when the user hasn't touched Loore (they may have been active on X).
    Downstream chaining is per-user via _post_import. No-op until
    X_CLIENT_ID is configured."""
    with flask_app.app_context():
        if not flask_app.config.get("X_CLIENT_ID"):
            return {"status": "not_configured"}
        accounts = ExternalAccount.query.filter(
            ExternalAccount.provider == "twitter",
            ExternalAccount.access_token.isnot(None),
            ExternalAccount.revoked_at.is_(None),
        ).all()
        for i, account in enumerate(accounts):
            # Staggered so N users don't hit X (and the digest LLM) at once.
            sync_twitter_bookmarks.apply_async(
                args=[account.user_id], countdown=i * 30)
        logger.info("Nightly X bookmark sync dispatched for %d accounts",
                    len(accounts))
        return {"status": "ok", "dispatched": len(accounts)}
