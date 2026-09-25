"""Background sync of external content sources (#155 / Download).

fetch_community_archive: pull a donated archive's tweets from the public
Community Archive into ExternalItem rows (deduped per user+source+id).

sync_twitter_bookmarks: pull the user's X bookmarks via their connected
OAuth account (pay-per-use X API; env-gated by X_CLIENT_ID). Refreshes
the access token when expired.
"""
from datetime import datetime, timedelta
import time

import requests
from celery.utils.log import get_task_logger
from sqlalchemy import func

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import APICostLog, ExternalAccount, ExternalItem
from backend.utils.cost import X_POST_READ_COST_MICRODOLLARS
from backend.utils.notifications import notify_user
from backend.utils.timefmt import user_local_hour
from backend.utils.external_content import (
    ca_fetch_tweets, ca_lookup_account, x_fetch_bookmark_pages,
    x_refresh_access_token, x_tweet_public_status,
)

logger = get_task_logger(__name__)


def _upsert_items(user_id, source, items):
    """Insert normalized items, skipping per-user duplicates. Returns
    (created, skipped).

    A tweet a Read picked and the user had not saved already has a row
    (READ_PICK_SOURCE): saving it turns that row into this source's
    reference in place, so its read mark and verdict carry over (#352).
    That counts as created — it is new to the references, and the
    bookmark sync's early stop must not read it as a known bookmark."""
    from backend.models import TWEET_SOURCES
    from backend.utils.reference_rows import pick_rows_by_tweet, save_pick_row
    existing = {
        row[0] for row in db.session.query(ExternalItem.external_id).filter_by(
            user_id=user_id, source=source).all()
    }
    picked = pick_rows_by_tweet(user_id) if source in TWEET_SOURCES else {}
    created = skipped = 0
    for item in items:
        if item["external_id"] in existing:
            skipped += 1
            continue
        pick_row = picked.pop(item["external_id"], None)
        if pick_row is not None:
            save_pick_row(pick_row, source)
            if item.get("public_source") is False:
                # A protected author seen by the sync: private wins.
                pick_row.public_source = False
            existing.add(item["external_id"])
            created += 1
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
            user_id, SOURCE_COMMUNITY_ARCHIVE,
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
        #
        # Except after a sync that did not finish (#310). The early stop
        # assumes everything below a known page was imported by a sync
        # that ran to its end. A sync cut off by a 429, a 5xx or a deploy
        # killing the worker, while it was still storing new bookmarks,
        # leaves its head imported and the rest missing; stopping at that
        # head would skip the rest for good. That holds for a first
        # import (no last_synced_at yet) and for a catch-up sync on an
        # account that has synced before (after a reconnect or an
        # unpark, hundreds of bookmarks can be waiting), which the
        # sync_incomplete marker records. In either case a known page
        # does not end the sync and does not freeze the page size: it
        # reads on to the end or to max_items (X's own cap), re-reading
        # the imported head once — at most max_items posts, the cost the
        # interrupted attempt would have had if it had finished. A page
        # on which X returned nothing still ends it.
        read_to_end = (account.last_synced_at is None
                       or bool(account.sync_incomplete))
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
                if page:
                    # Rides _upsert_items' commit, so it is stored with
                    # the page's rows: an exception or a killed worker
                    # after this point leaves it set for the next sync.
                    # Cleared below only when the sync reaches its end.
                    account.sync_incomplete = True
                page_created, page_skipped = _upsert_items(
                    user_id, "twitter_bookmark", page)
                created += page_created
                skipped += page_skipped
                if page_created == 0 and (not read_to_end or returned == 0):
                    break
                grow = page_skipped == 0 or read_to_end
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
        # Only a sync that ran to its end gets here, so this is what
        # ends the read-to-the-end mode above.
        account.last_synced_at = datetime.utcnow()
        account.sync_incomplete = None
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


# Nightly publicness check for saved tweets nobody has vouched for yet
# (#295 step 0): rows the JSON import created, tweets clipped without a
# visible lock, bookmarks synced before the sync asked X about the
# author. Each distinct tweet id is asked ONCE against X's free oEmbed
# endpoint (no API credits; the request carries the tweet id and nothing
# about the user) and the verdict lands on every user's copy.
# INTRODUCED CONSTANTS, not tuned: at most 300 lookups a night, half a
# second apart — the endpoint is meant for websites rendering embeds and
# publishes no quota. A throttle (429), a server error or no answer ends
# the night's run; those rows stay unknown and queue behind the ones
# never asked.
PUBLIC_SOURCE_SWEEP_LIMIT = 300
PUBLIC_SOURCE_SWEEP_PAUSE = 0.5
# The sweep is global (one endpoint, one budget), so its night is a UTC
# hour rather than each user's: the HOURLY beat gate runs it when the
# UTC clock is in this hour. Hourly with a gate, not a daily interval:
# every deploy deletes the beat schedule file (deploy.sh), which starts
# a daily interval's 24 h countdown over, and prod deploys most days —
# a plain 86400 s entry would fire rarely or never. INTRODUCED CONSTANT:
# 05:00 UTC is a quiet hour on both sides of the Atlantic, and nothing
# waits on the sweep.
PUBLIC_SOURCE_SWEEP_UTC_HOUR = 5
# Re-run guard, as for the bookmark sync: a beat restart shifts the
# gate's phase within the hour, and a second run would double the
# night's budget.
PUBLIC_SOURCE_SWEEP_MIN_GAP = timedelta(hours=20)
# Sources whose external_id is a tweet id.
X_TWEET_SOURCES = ("twitter_bookmark", "twitter_like")
SOURCE_COMMUNITY_ARCHIVE = "community_archive"


def _vouch_archive_rows():
    """Community Archive rows are public by construction (an opt-in
    public corpus) and every write path stamps them so now; rows from
    before the column carry NULL. Settle those in one statement.
    Idempotent — once settled it matches nothing — so it costs the sweep
    one cheap query a night rather than anyone a one-off script."""
    vouched = (ExternalItem.query
               .filter(ExternalItem.source == SOURCE_COMMUNITY_ARCHIVE,
                       ExternalItem.public_source.is_(None))
               .update({"public_source": True}, synchronize_session=False))
    db.session.commit()
    return vouched


def _apply_public_verdict(external_id, verdict, now):
    """Stamp one tweet's verdict on every unknown copy of it, whoever
    saved it, and COMMIT: verdicts are kept one by one, so a run cut off
    mid-way (the soft time limit, a deploy's SIGTERM to the worker)
    keeps everything X already answered, and no row lock is held across
    the pause between lookups, where a clip of the same tweet can land.
    Answered or not, the attempt is recorded."""
    values = {"public_source_checked_at": now}
    if verdict is not None:
        values["public_source"] = verdict
    stamped = (ExternalItem.query
               .filter(ExternalItem.source.in_(X_TWEET_SOURCES),
                       ExternalItem.external_id == external_id,
                       ExternalItem.public_source.is_(None))
               .update(values, synchronize_session=False))
    db.session.commit()
    return stamped


def _unknown_tweet_ids(limit):
    """The next ``limit`` DISTINCT unknown tweet ids: never-asked tweets
    first (newest save first, so tonight's clip is resolved tonight),
    then the least recently asked. Grouped per tweet, not per row, so
    the nightly cap counts lookups — a tweet ten people saved is one
    lookup, not ten of the budget. Every copy is stamped together, so
    max(checked_at) is when the tweet was last asked and NULL means it
    never was."""
    last_asked = func.max(ExternalItem.public_source_checked_at)
    rows = (
        db.session.query(ExternalItem.external_id)
        .filter(ExternalItem.source.in_(X_TWEET_SOURCES),
                ExternalItem.public_source.is_(None))
        .group_by(ExternalItem.external_id)
        .order_by(last_asked.asc().nullsfirst(),
                  func.max(ExternalItem.id).desc())
        .limit(limit).all()
    )
    return [external_id for (external_id,) in rows]


def run_public_source_sweep(limit=PUBLIC_SOURCE_SWEEP_LIMIT,
                            pause=PUBLIC_SOURCE_SWEEP_PAUSE):
    """One night's sweep (a plain function: the beat gate below decides
    when a night is). Needs an app context."""
    archive_vouched = _vouch_archive_rows()
    public = refused = unanswered = lookups = 0
    stopped_on = None
    for external_id in _unknown_tweet_ids(limit):
        if lookups and pause:
            time.sleep(pause)
        verdict, status = x_tweet_public_status(external_id)
        lookups += 1
        _apply_public_verdict(external_id, verdict, datetime.utcnow())
        if verdict is True:
            public += 1
        elif verdict is False:
            refused += 1
        else:
            unanswered += 1
            if status is None or status == 429 or status >= 500:
                # X is throttling, failing or unreachable: tomorrow,
                # rather than hammer it tonight.
                stopped_on = status or "network"
                break
    logger.info(
        "Public-source sweep: %d archive rows vouched, %d lookups, "
        "%d public, %d refused, %d unanswered%s", archive_vouched,
        lookups, public, refused, unanswered,
        f", stopped on {stopped_on}" if stopped_on else "")
    return {"status": "ok", "archive_vouched": archive_vouched,
            "lookups": lookups, "public": public, "refused": refused,
            "unanswered": unanswered, "stopped_on": stopped_on}


@celery.task(name='backend.tasks.external_sync.verify_public_source_sweep')
def verify_public_source_sweep():
    """Hourly beat gate: run the night's sweep when the UTC clock is in
    PUBLIC_SOURCE_SWEEP_UTC_HOUR and it has not run within
    PUBLIC_SOURCE_SWEEP_MIN_GAP — the newest checked_at says when it
    last did. Hourly with an internal gate like the bookmark sync and
    the digest sweep; see PUBLIC_SOURCE_SWEEP_UTC_HOUR for why a daily
    interval would not fire."""
    with flask_app.app_context():
        now = datetime.utcnow()
        if now.hour != PUBLIC_SOURCE_SWEEP_UTC_HOUR:
            return {"status": "not_due"}
        last_run = db.session.query(
            func.max(ExternalItem.public_source_checked_at)).scalar()
        if last_run and now - last_run < PUBLIC_SOURCE_SWEEP_MIN_GAP:
            return {"status": "ran_recently"}
        return run_public_source_sweep()
