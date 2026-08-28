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
    account = ca.fetch_account(handle)
    if not account:
        raise ca.CommunityArchiveError(
            f"@{handle} is not in the Community Archive")
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
        parquet_account = ca.fetch_account_parquet(account["username"], snapshot_dir)
        if parquet_account:
            account = parquet_account
        source = ca.iter_tweets_parquet(
            account["account_id"], snapshot_dir,
            on_page=lambda n: state("fetching", n, expected))
    else:
        source = ca.iter_tweets(
            account["username"], on_page=lambda n: state("fetching", n, expected))
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
                                  state, seed_now, no_rows_error=ca.CommunityArchiveError)
    result.update({
        "source": "parquet" if use_parquet else "rest",
        # What the archive actually holds vs. the account's self-reported
        # lifetime counter (uploads are often partial) — so a low node
        # count reads as "partial archive", not "import bug".
        "archived": len(seen), "retweets_skipped": retweets,
        "account_num_tweets": account.get("num_tweets") or 0,
    })
    return result


def _import_prefill_rows(user_id, handle, rows, options, state, seed_now,
                         no_rows_error=RuntimeError):
    """Shared tail of every admin pre-fill: sort the compact rows, pin the
    user to the BATCH profile pipeline, create private twitter-origin nodes,
    and kick the batch seeder. ``handle`` is the canonical username the
    tweets came from (stored as ``prefilled_handle``)."""
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
    queued = bool(User.query.get(user_id).profile_needs_full_regen)
    if queued and seed_now:
        # Don't wait for the hourly seeder: submit this user's first chunk
        # now (the ~60s poller then drives the rest of the chain).
        from backend.tasks.profile_batch import seed_profile_batch_for_user
        seed_profile_batch_for_user.delay(user_id)
    result.update({
        "user_id": user_id, "handle": handle, "total": total, "stage": "done",
        "profile_batch_queued": queued,
        # The immediate seed runs regardless, but every LATER chunk comes
        # from the hourly seeder, which only walks approved accounts.
        "awaiting_activation": queued and not User.query.get(user_id).approved,
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

    if not User.query.get(user_id):
        raise RuntimeError(f"User {user_id} not found")
    creds = (current_app.config.get("TWITTER_API_KEY"),
             current_app.config.get("TWITTER_API_SECRET"))
    account = x_api.lookup_user(handle, creds)
    if not account:
        raise x_api.XApiError(f"@{handle}: no such X account")
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
    # Spent columns and cost_report.py read): posts read + the lookup.
    from backend.extensions import db
    from backend.models import APICostLog
    db.session.add(APICostLog(
        user_id=user_id, model_id="x-api/timeline", request_type="x_prefill",
        request_ref=f"@{account['username']}"[:64], input_tokens=0, output_tokens=0,
        cost_microdollars=x_api.cost_microdollars(len(seen), user_reads=1)))
    db.session.commit()
    if fetch_error and not seen:
        raise x_api.XApiError(fetch_error)
    result = _import_prefill_rows(user_id, account["username"], rows, options,
                                  state, seed_now, no_rows_error=x_api.XApiError)
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
