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
from backend.utils.cost import X_POST_READ_COST_MICRODOLLARS
from backend.utils.notifications import notify_user
from backend.utils.timefmt import user_local_hour
from backend.utils.external_content import (
    ca_fetch_tweets, ca_lookup_account, x_fetch_bookmark_pages,
    x_refresh_access_token,
)

logger = get_task_logger(__name__)


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
            title=item.get("title"),
            url=item.get("url"),
            posted_at=item.get("posted_at"),
            # Absent (the JSON bookmark import) = unknown, never a guess.
            public_source=item.get("public_source"),
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
        return {"status": "ok", "username": username,
                "created": created, "skipped": skipped}


# Import page anchor of the X Bookmarks card (ExternalImport.js).
X_RECONNECT_LINK = "/import#x-bookmarks"


def _oauth_error(exc):
    """``(error, error_description)`` from a failed grant, ('', '') when
    the response carries no parseable JSON body."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return "", ""
    try:
        body = resp.json()
    except Exception:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    return body.get("error") or "", body.get("error_description") or ""


# X's wording when the refresh token itself is rejected, as opposed to
# the request around it: "Value passed for the token was invalid."
DEAD_TOKEN_DESCRIPTIONS = ("token was invalid",)


def _grant_is_dead(code, error, description):
    """Did X reject the USER's refresh token, or Loore's own client?

    Only the first warrants parking the account and telling the user to
    reconnect. X's token endpoint does not use the RFC 6749 error names,
    so this reads the codes it actually sends (probed live 2026-09-18):

      401 unauthorized_client "Missing valid authorization header"
          — Loore's client credentials are missing or wrong. Identical
            response whether the Basic header is absent or carries the
            wrong secret. This was #313: every user disconnected for a
            bug none of them could fix by reconnecting.
      400 invalid_request     "Value passed for the token was invalid."
          — the user's refresh token is dead: they revoked Loore on X,
            or the rotating refresh-token family died.

    ``invalid_request`` is also the generic "your request was malformed"
    code — X sends it for a missing parameter too — so it means a dead
    grant only when the description is X's specific one about the token.
    A request Loore built wrong is ours, not the user's.

    Anything else — an unparseable body, an HTML error page from an
    edge, a 5xx — is not a signal about this user's grant, so it fails
    the sync loudly instead of disconnecting somebody. Matching X's
    wording is brittle by nature: if they reword it, this stops parking
    and the sync starts erroring nightly, which Sentry surfaces. That is
    the direction to be brittle in — the other one disconnects users
    who did nothing wrong, silently, which is how #313 ran for months.
    """
    if code not in (400, 401) or not error:
        return False
    if error == "invalid_grant":  # the RFC name, should X ever adopt it
        return True
    if error == "invalid_request":
        return any(phrase in description.lower()
                   for phrase in DEAD_TOKEN_DESCRIPTIONS)
    return False


def _mark_revoked(account, why):
    """X rejected the account's tokens — user revoked the app, or the
    rotating refresh-token family died. Park the account (nightly sync
    skips it; the import page shows a reconnect state) instead of
    retrying forever."""
    account.revoked_at = datetime.utcnow()
    db.session.commit()
    logger.warning("X account for user %s marked revoked (%s)",
                   account.user_id, why)
    # The nightly sync fails silently otherwise — the user only learns
    # bookmarks stopped arriving if they happen to open the import page.
    # notify_user commits (and rolls back on failure), hence AFTER the
    # revoked_at commit above. The reconnect callback marks it read.
    notify_user(
        account.user_id,
        type="x_disconnected",
        title="X disconnected — bookmark sync paused",
        body=("X stopped accepting Loore's access"
              + (f" to @{account.handle}" if account.handle else "")
              + ". Your nightly bookmark sync is paused until you "
              "reconnect on the Import page."),
        link=X_RECONNECT_LINK,
    )
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
                    client_id, account.get_refresh_token(),
                    flask_app.config.get("X_CLIENT_SECRET"))
            except requests.HTTPError as exc:
                code = (exc.response.status_code
                        if exc.response is not None else None)
                error, description = _oauth_error(exc)
                if _grant_is_dead(code, error, description):
                    return _mark_revoked(
                        account, f"refresh HTTP {code} {error}")
                # Not this user's grant — ours, or X's. Fail loudly
                # rather than disconnecting a user who did nothing and
                # can fix nothing by reconnecting (#313).
                logger.error(
                    "X token refresh for user %s failed with HTTP %s "
                    "%s: %s — NOT parking the account, this is not the "
                    "user's grant", account.user_id, code,
                    error or "(no error body)", description[:200])
                raise
            account.set_tokens(
                tokens["access_token"], tokens.get("refresh_token"))
            if tokens.get("expires_in"):
                account.token_expires_at = (
                    datetime.utcnow()
                    + timedelta(seconds=int(tokens["expires_in"])))
            db.session.commit()

        # Early-stop pagination: bookmarks arrive newest-bookmarked-first,
        # so once a whole page produced nothing new, the rest is already
        # imported — stop instead of paying for the tail. Page-wise (not
        # first-known-id) because a re-bookmarked old tweet jumps to the
        # top and would otherwise mask newer items below it. Pages start
        # small and grow while all-new (x_fetch_bookmark_pages); the page
        # that reaches known bookmarks freezes the size (send(False)), so
        # the page that ends a night is a cheap one.
        # X bills pay-per-use per RETURNED POST (#271), not per request:
        # a page of N bookmarks costs N post reads, an empty page nothing.
        created = skipped = requests_made = posts_read = 0

        def _log_cost():
            """One APICostLog row for the pages X returned — also when the
            sync then fails (429, 5xx, 401): X billed every page that
            arrived, and a dropped row under-counts by up to a page of
            posts. The failing request itself returned nothing, so it
            costs nothing. Auditable against the developer-portal bill:
            N posts read over M pages."""
            if requests_made:
                db.session.add(APICostLog(
                    user_id=user_id,
                    model_id="x-api/bookmarks",
                    request_type="x_bookmark_sync",
                    request_ref=f"posts:{posts_read}/pages:{requests_made}",
                    input_tokens=0,
                    output_tokens=0,
                    cost_microdollars=(
                        posts_read * X_POST_READ_COST_MICRODOLLARS),
                ))

        pages = x_fetch_bookmark_pages(
            account.get_access_token(), account.external_user_id,
            max_items=max_items)
        grow = None
        try:
            while True:
                try:
                    page, returned = pages.send(grow)
                except StopIteration:
                    break
                requests_made += 1
                posts_read += returned
                page_created, page_skipped = _upsert_items(
                    user_id, "twitter_bookmark", page)
                created += page_created
                skipped += page_skipped
                if page_created == 0:
                    break
                grow = page_skipped == 0
        except Exception as exc:
            # Pages already upserted are committed (_upsert_items commits
            # per page); this only discards a half-applied page from a DB
            # failure, so the cost row can be written on a clean session.
            # If the database itself is what failed, the cost row cannot
            # be written either: log that and let the ORIGINAL error
            # propagate rather than replacing it with the commit's.
            try:
                db.session.rollback()
                _log_cost()
                db.session.commit()
            except Exception:
                logger.exception(
                    "X bookmarks sync for user %s: could not log the cost "
                    "of %d posts over %d pages after the sync failed",
                    user_id, posts_read, requests_made)
                db.session.rollback()
            code = (exc.response.status_code
                    if isinstance(exc, requests.HTTPError)
                    and exc.response is not None else None)
            # 401 = token invalidated without a refresh in between. 403 is
            # NOT revocation (usually an API-tier/permissions problem on
            # our side) — let it raise so it shows up as an operator error.
            if code == 401:
                return _mark_revoked(account, "bookmarks fetch HTTP 401")
            raise
        account.last_synced_at = datetime.utcnow()
        account.last_sync_created = created
        _log_cost()
        db.session.commit()
        logger.info("X bookmarks sync for user %s: %d new, %d known, "
                    "%d posts read over %d API requests", user_id, created,
                    skipped, posts_read, requests_made)
        return {"status": "ok", "created": created, "skipped": skipped,
                "requests": requests_made, "posts_read": posts_read}


# The nightly sync fires in each user's OWN night: the hourly beat gate
# dispatches an account when its user's local clock (User.timezone,
# browser-captured IANA name, UTC fallback) is in this hour.
NIGHTLY_SYNC_LOCAL_HOUR = 3
# Re-dispatch guard: beat restarts shift the gate's phase within the
# hour, so without this a user could sync twice in one night.
NIGHTLY_SYNC_MIN_GAP = timedelta(hours=20)


@celery.task(name='backend.tasks.external_sync.sync_all_twitter_bookmarks')
def sync_all_twitter_bookmarks():
    """Hourly beat gate: dispatch bookmark syncs for connected, non-revoked
    X accounts whose user's LOCAL time is ~3am, so every user syncs in
    their own night. Runs BEFORE anything context-side is rebuilt: the
    external-digest sweep gates on local 4am, an hour later, so a night's
    new bookmarks are already stored when the digest is rebuilt — X
    activity alone warrants a refresh even when the user hasn't touched
    Loore. No-op until X_CLIENT_ID is configured."""
    with flask_app.app_context():
        if not flask_app.config.get("X_CLIENT_ID"):
            return {"status": "not_configured"}
        now = datetime.utcnow()
        accounts = ExternalAccount.query.filter(
            ExternalAccount.provider == "twitter",
            ExternalAccount.access_token.isnot(None),
            ExternalAccount.revoked_at.is_(None),
        ).all()
        dispatched = 0
        for account in accounts:
            if user_local_hour(account.user) != NIGHTLY_SYNC_LOCAL_HOUR:
                continue
            if (account.last_synced_at
                    and now - account.last_synced_at < NIGHTLY_SYNC_MIN_GAP):
                continue
            # Staggered so N users don't hit X (and the digest LLM) at once.
            sync_twitter_bookmarks.apply_async(
                args=[account.user_id], countdown=dispatched * 30)
            dispatched += 1
        if dispatched:
            logger.info("Nightly X bookmark sync dispatched for %d accounts",
                        dispatched)
        return {"status": "ok", "dispatched": dispatched}
