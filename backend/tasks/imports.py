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
import json
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

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


def snapshot_dir_for(config):
    """Where the Community Archive parquet snapshot lives: configured dir, or
    ``<data>/community-archive`` next to the audio/import stashes."""
    import pathlib
    configured = config.get("COMMUNITY_ARCHIVE_SNAPSHOT_DIR")
    if configured:
        return pathlib.Path(configured)
    from backend.utils.twitter_archive import STASH_ROOT
    return STASH_ROOT.parent / "community-archive"


@celery.task(name="backend.tasks.imports.refresh_community_archive_snapshot")
def refresh_community_archive_snapshot():
    """Beat sweep (every 30 min): keep the cached Community Archive export
    current, so a read started after the nightly export (~07:00 UTC)
    reads that day and does not wait on the 900 MB download itself (a
    read also refreshes on its own before rendering, in case this sweep
    is down). Maintains only a snapshot that exists — the first copy is
    fetched by the pre-fill import or the CLI, never here, so staging
    does not download a gigabyte per deploy."""
    from backend.utils.ca_feed import refresh_snapshot_for_read
    with flask_app.app_context():
        export_id = refresh_snapshot_for_read(
            snapshot_dir_for(flask_app.config), log=logger)
    return {"export_id": export_id}


def prefill_community_archive_impl(user_id, handle, options, update_state=None,
                                   seed_now=True):
    """Fetch @handle's tweets from the Community Archive (REST for small
    accounts, the nightly parquet snapshot for large ones) into the user's
    account (origin="twitter", private, AI-readable) and pin the user to
    the BATCH profile pipeline before the import's profile handoff runs,
    so a bootstrapped corpus never triggers a synchronous (full-price)
    build. Runs inside an app context; testable without Celery."""
    from backend.models import User
    from backend.utils import twitter_archive as ta
    from backend.utils import community_archive as ca

    def state(stage, done, total):
        if update_state:
            update_state(state="PROGRESS", meta={
                "user_id": user_id, "stage": stage, "done": done,
                "total": total, "handle": handle,
            })

    from flask import current_app
    user = User.query.get(user_id)
    if not user:
        raise RuntimeError(f"User {user_id} not found")
    from backend.utils.x_identity import resolve_x_id, XIdUnresolved
    try:
        # A background task, not a request: the archive's own timeout, not
        # the whitelist's request budget (a slow archive query is fine here).
        resolved = resolve_x_id(handle, x_lookup=False, timeout=ca.TIMEOUT)
    except XIdUnresolved as e:
        raise ca.CommunityArchiveError(e.message) from e
    # The one account record for this pre-fill. Its id passed the check
    # below and is the id the tweets are read for; nothing re-resolves the
    # handle after this (a stale snapshot can map it to a former holder).
    account = resolved.account
    _check_x_id_conflict(user, account["account_id"], account["username"])
    # Size by what the archive actually holds (exact live count) — NOT
    # all_account.num_tweets, the account's lifetime counter: an
    # extension-ingested account can "report 13k tweets" while the
    # archive holds 1k and the nightly snapshot none at all.
    try:
        archived = ca.count_archived(account["account_id"])
    except Exception as e:  # header lookup failed → fall back to the counter
        logger.warning("Community Archive count failed for @%s: %s", handle, e)
        archived = None
    expected = archived if archived is not None else (account.get("num_tweets") or 0)

    # Small accounts page through the REST API; large ones read the nightly
    # parquet snapshot (downloaded once per export into the data dir) —
    # unless the snapshot holds fewer rows than the live archive (account
    # ingested/updated after the export), in which case REST is complete
    # and parquet is not.
    min_parquet = current_app.config.get("COMMUNITY_ARCHIVE_PARQUET_MIN_TWEETS", 5000)
    use_parquet = bool(options.get("force_parquet")) or expected >= min_parquet
    if use_parquet:
        snapshot_dir = snapshot_dir_for(current_app.config)
        state("downloading", 0, None)
        ca.ensure_snapshot(snapshot_dir, on_progress=lambda name, done, total: state(
            f"downloading {name}", done >> 20, (total >> 20) if total else None))
        in_snapshot = ca.count_parquet(account["account_id"], snapshot_dir)
        if in_snapshot == 0 or (archived is not None and in_snapshot < archived):
            logger.info("@%s: snapshot holds %s rows vs %s live — using REST",
                        handle, in_snapshot, archived)
            use_parquet = False
    if use_parquet:
        source = ca.iter_tweets_parquet(
            account["account_id"], snapshot_dir,
            on_page=lambda n: state("fetching", n, expected))
    else:
        source = ca.iter_tweets(
            account["account_id"], on_page=lambda n: state("fetching", n, expected))
    state("fetching", 0, expected)

    rows, seen, retweets = [], set(), 0
    for raw in source:
        if raw["tweet_id"] in seen:
            continue
        seen.add(raw["tweet_id"])
        row = ta.compact_row(ca.to_export_entry(raw)["tweet"])
        if row is None:
            retweets += 1  # compact_row drops retweets, like the native import
        else:
            rows.append(row)
    result = _import_prefill_rows(user_id, account["username"], rows, options,
                                  state, seed_now, no_rows_error=ca.CommunityArchiveError,
                                  x_id=account.get("account_id"))
    result.update({
        "source": "parquet" if use_parquet else "rest",
        # What the archive actually holds vs. the account's self-reported
        # lifetime counter (uploads are often partial) — so a low node
        # count reads as "partial archive", not "import bug".
        "archived": len(seen), "retweets_skipped": retweets,
        "account_num_tweets": account.get("num_tweets") or 0,
    })
    return result


class PrefillIdConflict(RuntimeError):
    """The handle's X account is not the account being filled: the account
    already signs in with a different X id, or another account already
    signs in with this one. Raised before anything is fetched or imported;
    the message is what the admin sees in the pre-fill status."""


def _check_x_id_conflict(user, x_id, handle):
    """Stop a pre-fill whose X account contradicts the target account.

    Filling user A with @handle whose X id belongs to user B, or with a
    handle that is not the X account A signs in with, is a wrong-account
    pre-fill: it would import someone else's tweets, or build two accounts
    for one person. Neither is repaired by a warning in a log nobody reads,
    so the pre-fill fails here with a readable message instead. An account
    with no X id is fine (it may get this id, see _stamp_x_id)."""
    from backend.models import User
    x_id = str(x_id)
    if user.twitter_id is not None and user.twitter_id != x_id:
        raise PrefillIdConflict(
            f"@{handle} is X account {x_id}, but user {user.id} (@{user.username}) "
            f"signs in with X account {user.twitter_id}; pre-fill stopped before importing")
    holder = User.query.filter(User.twitter_id == x_id, User.id != user.id).first()
    if holder is not None:
        raise PrefillIdConflict(
            f"@{handle} (X id {x_id}) already signs in as user {holder.id} "
            f"(@{holder.username}); pre-fill into user {user.id} (@{user.username}) "
            "stopped before importing — that would make two accounts for one person")


def _holder_note(holder):
    """Who holds the X id this pre-fill wanted to attach, in words that
    say what happened: the owner signing in and getting a fresh account is
    one thing, a concurrent job stamping another placeholder is another,
    and an account from before sign-ins were recorded is neither for sure."""
    from backend.utils.activity import sign_in_status
    status = sign_in_status(holder)
    if status == "signed-in":
        return (f"the owner signed in with X during the pre-fill and got user {holder.id} "
                f"(@{holder.username}); X id not attached — two accounts for one person")
    if status == "never":
        return (f"another placeholder, user {holder.id} (@{holder.username}), got this X id "
                "meanwhile (a concurrent pre-fill or the backfill); X id not attached — two "
                "placeholders for one X account, keep one")
    created = f"{holder.created_at:%Y-%m-%d}" if holder.created_at else "?"
    return (f"user {holder.id} (@{holder.username}) already has this X id; it has no sign-in "
            f"on record but predates the record (created {created}), so it may be an early X "
            "signup — X id not attached; check with the person before deleting anything")


def _stamp_x_id(user, x_id):
    """Give a pre-filled account that has no login yet the X id of the
    handle it was filled from, so its owner's X login finds it by id (X
    logins never match on handle). Only an account with no login of its
    own gets it: attaching an X id to an account that signs in by email
    would hand that account to the handle's owner without the account's
    owner ever proving control of the X account, and an account that
    already has an X id is never re-keyed — the id that passed
    _check_x_id_conflict is the only one that may be attached, and only
    where there is none.

    The write is conditional (``WHERE twitter_id IS NULL``) and committed
    here, so a concurrent pre-fill or backfill that stamped the account
    after this task read it is not overwritten; anything set on ``user``
    before must already be committed. Returns the result fields:
    ``x_id``, ``x_id_stamped`` and, when not stamped, ``x_id_note``."""
    from sqlalchemy import update
    from backend.extensions import db
    from backend.models import User
    x_id = str(x_id)
    if user.twitter_id == x_id:
        return {"x_id": x_id, "x_id_stamped": False,
                "x_id_note": "account already signs in with this X account"}
    if user.twitter_id is not None:
        return {"x_id": x_id, "x_id_stamped": False,
                "x_id_note": (f"account signs in with X account {user.twitter_id}; "
                              "X id not changed")}
    if user.email is not None:
        return {"x_id": x_id, "x_id_stamped": False,
                "x_id_note": ("account signs in by email; X id not attached — the "
                              "owner can only add X by signing in with it (Connect X)")}
    # Re-checked here, not only up front: the owner may have signed in
    # with X while the tweets were being fetched.
    holder = User.query.filter(User.twitter_id == x_id, User.id != user.id).first()
    if holder is not None:
        return {"x_id": x_id, "x_id_stamped": False, "x_id_note": _holder_note(holder)}
    try:
        rows = db.session.execute(
            update(User).where(User.id == user.id, User.twitter_id.is_(None))
            .values(twitter_id=x_id)).rowcount
        db.session.commit()
    except IntegrityError:
        # Another row took the id between the check above and this write.
        db.session.rollback()
        holder = User.query.filter(User.twitter_id == x_id, User.id != user.id).first()
        note = (_holder_note(holder) if holder is not None
                else "another account took this X id meanwhile; X id not attached")
        return {"x_id": x_id, "x_id_stamped": False, "x_id_note": note}
    db.session.refresh(user)
    if rows == 0:
        # Stamped after this task read the account: not ours to change.
        if user.twitter_id == x_id:
            note = "X id attached meanwhile by another job (a concurrent pre-fill or the backfill)"
        else:
            note = f"another job attached X account {user.twitter_id} meanwhile; X id not changed"
        return {"x_id": x_id, "x_id_stamped": False, "x_id_note": note}
    return {"x_id": x_id, "x_id_stamped": True, "x_id_note": None}


def _import_prefill_rows(user_id, handle, rows, options, state, seed_now,
                         no_rows_error=RuntimeError, x_id=None):
    """Shared tail of every admin pre-fill: sort the compact rows, pin the
    user to the BATCH profile pipeline, create private twitter-origin nodes,
    and kick the batch seeder. ``handle`` is the canonical username the
    tweets came from (stored as ``prefilled_handle``); ``x_id`` its numeric
    X id, stamped on a login-less account so its owner's X login finds it
    (the result says whether it was, and why not)."""
    from backend.extensions import db
    from backend.models import User
    from backend.routes.import_data import create_twitter_nodes
    from backend.utils import twitter_archive as ta

    rows.sort(key=lambda r: ta._sort_key(r["created_at"]))
    total = len(rows)
    if total == 0:
        raise no_rows_error(f"@{handle}: no own tweets found")

    # Pin BEFORE create_twitter_nodes: its profile handoff consults
    # use_batch_for_user and must route to the seeder, not the sync task.
    user = User.query.get(user_id)
    user.profile_force_batch = True
    user.prefilled_handle = handle
    db.session.commit()
    x_id_result = _stamp_x_id(user, x_id) if x_id else {}

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
    # Whether the seeder will build for this account is its own gate,
    # evaluated here for the admin's result line (the regen flag it sets
    # when due is the one the seed sets anyway). Reading the flag instead
    # raced the seed task the handoff dispatches and reported "below
    # threshold" for every pre-fill since the chunk planner (2026-09-04..15).
    from backend.tasks.profile_batch import (
        _should_seed, _latest_non_integration_profile,
        seed_profile_batch_for_user)
    from backend.utils.chunk_plan import next_build_threshold
    user = User.query.get(user_id)
    queued = bool(user.profile_batch_pending or _should_seed(user))
    if queued and seed_now and not user.profile_batch_pending:
        # Don't wait for the hourly seeder (which skips unapproved
        # accounts): submit this user's first chunk now. The handoff in
        # create_twitter_nodes seeds too, but only for Voice-Mode plans;
        # a second seed is harmless (pipeline lock + pending guard).
        seed_profile_batch_for_user.delay(user_id)
    latest = _latest_non_integration_profile(user_id)
    covered = (latest.source_tokens_used or 0) if latest else 0
    result.update({
        "user_id": user_id, "handle": handle, "total": total, "stage": "done",
        "profile_batch_queued": queued,
        **x_id_result,
        # The ladder step the account's total must reach for the next
        # from-scratch build (None once the account is past the ladder).
        "profile_threshold_tokens": next_build_threshold(covered),
        # Importer's approximate count over the rows offered (incl. any
        # deduped as already imported).
        "imported_tokens": sum(r.get("token_count") or 0 for r in rows),
    })
    return result


def prefill_x_api_impl(user_id, handle, options, update_state=None, seed_now=True):
    """Pull @handle's most recent own posts straight from the X API (paid,
    ~$0.005/post; capped by the timeline endpoint at ~3,200) into the
    user's account, then hand off to the same batch-profile tail as the
    Community Archive pre-fill. ``options["max_tweets"]`` bounds the pull
    (clamped to min(cap, account's tweet_count))."""
    from flask import current_app
    from backend.models import User
    from backend.utils import twitter_archive as ta
    from backend.utils import x_api

    def state(stage, done, total):
        if update_state:
            update_state(state="PROGRESS", meta={
                "user_id": user_id, "stage": stage, "done": done,
                "total": total, "handle": handle,
            })

    user = User.query.get(user_id)
    if not user:
        raise RuntimeError(f"User {user_id} not found")
    creds = (current_app.config.get("TWITTER_API_KEY"),
             current_app.config.get("TWITTER_API_SECRET"))
    from backend.utils.x_identity import resolve_x_id, XIdUnresolved
    try:
        # archive=False: the paid pull needs X's own record (tweet_count,
        # protected). The user read is billed to this account by the
        # resolver whether or not the pre-fill goes ahead.
        resolved = resolve_x_id(handle, x_lookup=True, archive=False,
                                cost_user_id=user_id, creds=creds,
                                timeout=x_api.TIMEOUT)  # a task, not a request
    except XIdUnresolved as e:
        raise x_api.XApiError(e.message) from e
    account = resolved.account
    # Before the paid pull: a wrong-account pre-fill must not cost more
    # than the lookup already did.
    _check_x_id_conflict(user, account["id"], account["username"])
    if account["protected"]:
        raise x_api.XApiError(f"@{account['username']} is protected — app-only auth can't read the timeline")
    expected = x_api.fetchable(account["tweet_count"], options.get("max_tweets"))
    if expected == 0:
        raise x_api.XApiError(f"@{account['username']}: nothing to fetch")
    state("fetching", 0, expected)

    # Keep an independent copy of every paid-for post as raw v2 JSON under
    # <data>/x-api/ (one file per pull), separate from the user's account
    # and its nodes — the fetch is billable and the account may be deleted.
    dump_path = x_api_dump_path(account["username"])
    rows, seen, retweets, fetch_error = [], set(), 0, None
    with open(dump_path, "w", encoding="utf-8") as dump:
        dump.write(json.dumps({
            "_meta": "loore x-api pre-fill", "fetched_at": datetime.now(timezone.utc).isoformat(),
            "account": account, "max_tweets": expected, "for_user_id": user_id,
        }) + "\n")
        try:
            for entry in x_api.iter_user_tweets(
                    account["id"], creds, max_tweets=expected,
                    on_page=lambda n: state("fetching", n, expected),
                    on_raw=lambda t, users: dump.write(json.dumps(
                        {**t, "_reply_to_username": users.get(t.get("in_reply_to_user_id"))}) + "\n")):
                tweet = entry["tweet"]
                if tweet["id_str"] in seen:
                    continue
                seen.add(tweet["id_str"])
                row = ta.compact_row(tweet)
                if row is None:
                    retweets += 1
                else:
                    rows.append(row)
        except x_api.XApiError as e:
            # Credits depleted (402), rate limit (429), transient 5xx…:
            # every post already returned has been billed, so keep it —
            # log the cost, import what we have, report the pull as
            # partial. Only a pull that got nothing is a hard failure.
            fetch_error = str(e)
            logger.warning("X API pre-fill for @%s stopped after %d posts: %s",
                           account["username"], len(seen), e)
    logger.info("X API pre-fill: %d posts for @%s saved to %s", len(seen), account["username"], dump_path)
    # Bill the pull to the target user's ledger (same table the admin
    # Spent columns and cost_report.py read): posts read. The user read
    # was logged by resolve_x_id (request_type x_id_lookup).
    from backend.extensions import db
    from backend.models import APICostLog
    db.session.add(APICostLog(
        user_id=user_id, model_id="x-api/timeline", request_type="x_prefill",
        request_ref=f"@{account['username']}"[:64], input_tokens=0, output_tokens=0,
        cost_microdollars=x_api.cost_microdollars(len(seen), user_reads=0)))
    db.session.commit()
    if fetch_error and not seen:
        raise x_api.XApiError(fetch_error)
    result = _import_prefill_rows(user_id, account["username"], rows, options,
                                  state, seed_now, no_rows_error=x_api.XApiError,
                                  x_id=account.get("id"))
    result.update({
        "source": "x-api",
        "fetched": len(seen), "retweets_skipped": retweets,
        "account_num_tweets": account.get("tweet_count") or 0,
        "est_cost_usd": x_api.estimate_cost(len(seen)),
        "dump_path": str(dump_path),
        "partial": bool(fetch_error), "fetch_error": fetch_error,
    })
    return result


def x_api_dump_path(handle):
    """``<data>/x-api/<handle>-<UTC stamp>.jsonl`` next to the audio/import
    stashes (AUDIO_STORAGE_PATH's parent — /home/.../write-or-perish/data
    on prod). Created on demand."""
    from backend.utils.twitter_archive import STASH_ROOT
    d = STASH_ROOT.parent / "x-api"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return d / f"{handle}-{stamp}.jsonl"


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


@celery.task(bind=True, name="backend.tasks.imports.prefill_x_api")
def prefill_x_api(self, user_id, handle, options):
    """Admin pre-fill via the X API (see prefill_x_api_impl)."""
    with flask_app.app_context():
        try:
            return prefill_x_api_impl(
                user_id, handle, options or {}, update_state=self.update_state)
        except Exception:
            logger.exception("X API pre-fill failed for user %s (@%s)", user_id, handle)
            raise
