"""Poll-driven Batch API pipeline for profile generation (issue #173, Part A).

Profile chunks are sequential (chunk N's prompt embeds chunk N-1's output), so
a single user's rebuild can't be parallel-batched. Instead:

- ``seed_profile_batches`` (hourly) accumulates each batch-selected, eligible
  user's CURRENT step into one cohort batch and submits it.
- ``poll_profile_batches`` (~60s) collects finished batches, saves each result
  (advancing that user's chain via the same ``_save_profile`` the sync path
  uses), enqueues the next step (next chunk → integration → done), and submits
  the next cohort batch.

State = ProfileBatchJob (the batch envelope + per-item metadata to route
results back) + the per-chunk UserProfile rows (the resume cursor) + the
User.profile_batch_pending / profile_batch_attempts guards. A crash between
ticks loses nothing: the poller re-checks pending jobs and the last saved
UserProfile is the cursor.

Gated by use_batch_for_user (canary allowlist OR global switch); non-selected
users stay on the synchronous path. See docs/design/profile-batch-processing.md
"""
from contextlib import contextmanager
from datetime import datetime, timedelta

from celery.utils.log import get_task_logger
from flask import current_app
from sqlalchemy import func, or_

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import User, UserProfile, Node, ProfileBatchJob
from backend.llm_providers import DEFAULT_MAX_OUTPUT_TOKENS
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.llm_batch import (
    batch_submit, batch_check_and_collect, apply_batch_key_override)
# Module reference, not `from ... import names`: exports imports
# celery_app, which imports this module to register its tasks. When a
# web request touches backend.tasks.exports FIRST (e.g. POST
# /export/update_profile on a fresh gunicorn worker), exports is only
# partially initialised at this point and a names-import raises
# ImportError; attribute access at call time is fine.
from backend.tasks import exports as _exports

logger = get_task_logger(__name__)

# Mirror maybe_trigger_incremental_profile_update's gates (exports.py).
THRESHOLD_TOKENS = 80000
MIN_INACTIVITY = timedelta(minutes=30)
MIN_INTERVAL = timedelta(hours=1)

MAX_BATCH_ATTEMPTS = 3              # batch retries before sync last-resort
BATCH_STALE_AFTER = timedelta(hours=24)   # provider SLA ceiling


def use_batch_for_user(user, config):
    """A user takes the Batch path if the global switch is on, their id is
    in the canary allowlist (issue #173), or they are pinned to batch
    (User.profile_force_batch — admin pre-fills)."""
    return (bool(config.get("PROFILE_USE_BATCH"))
            or bool(getattr(user, "profile_force_batch", False))
            or user.id in config.get("PROFILE_BATCH_USER_IDS", set()))


# ── helpers ────────────────────────────────────────────────────────────

BATCH_LOCK_KEY = "loore:profile_batch:lock"
BATCH_LOCK_TTL = 30 * 60  # a poll/seed pass must finish well within this


@contextmanager
def batch_pipeline_lock():
    """Serialize seed/poll passes. On 2026-08-27 a 17-minute poll overlapped
    the next ~60s poll: both collected the same job, both built 'the next
    request' for the same users, and the cohort went out with duplicate
    custom_ids — which OpenAI rejects wholesale, and which re-seeded
    itself on every subsequent step (5 items per user, 5x the cost).

    Yields True when the lock was acquired (or Redis is unreachable — the
    pipeline must not stop because the lock store is down), False when
    another pass holds it (caller skips this cycle)."""
    client = None
    try:
        import redis
        client = redis.Redis.from_url(current_app.config.get(
            "CELERY_BROKER_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=2)
        acquired = bool(client.set(BATCH_LOCK_KEY, "1", nx=True, ex=BATCH_LOCK_TTL))
    except Exception as e:  # no redis (tests, local) → run unlocked
        logger.warning(f"profile batch lock unavailable ({e}); running unlocked")
        client, acquired = None, True
    if not acquired:
        logger.info("profile batch pass skipped: another pass holds the lock")
        yield False
        return
    try:
        yield True
    finally:
        if client is not None:
            try:
                client.delete(BATCH_LOCK_KEY)
            except Exception:
                pass

def _model_for(user):
    return (user.preferred_model
            or current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-5"))


def _provider_and_model(model_id):
    cfg = current_app.config["SUPPORTED_MODELS"].get(model_id)
    if not cfg:
        raise ValueError(f"Unsupported model: {model_id}")
    return cfg["provider"], cfg["api_model"]


def _provider_key(provider, api_model):
    return "anthropic" if provider == "anthropic" else f"openai:{api_model}"


def _latest_non_integration_profile(user_id):
    return (UserProfile.query.filter(
        UserProfile.user_id == user_id,
        UserProfile.generation_type != 'integration')
        .order_by(UserProfile.created_at.desc()).first())


def _new_token_count(user, cutoff):
    q = db.session.query(func.coalesce(func.sum(Node.token_count), 0)).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.ai_usage.in_(['chat', 'train']),
    )
    if cutoff is not None:
        q = q.filter(Node.updated_at >= cutoff)
    return q.scalar()


def _remaining_token_count(user, cutoff):
    """Stored tokens the chunk builder still has ahead of it: nodes CREATED
    after the cutoff, in the profile's anchor scope. Not _new_token_count —
    that keys on updated_at (organic "new activity"), and imported tweets
    all carry the import time there, so a pre-filled corpus reads as
    entirely unprocessed forever."""
    return db.session.query(func.coalesce(func.sum(Node.token_count), 0)).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.ai_usage.in_(['chat', 'train']),
        Node.deleted_at.is_(None),
        Node.created_at > cutoff,
    ).scalar() or 0


def _should_seed(user):
    """Whether the user has crossed the trigger gates right now. Mirrors
    maybe_trigger_incremental_profile_update (inactivity, interval, tokens)
    without dispatching."""
    # A pending full rebuild overrides the volume/interval gates: the
    # rebuild was explicitly requested (regen button, failure recovery,
    # repair script) and the gates measure "new tokens since cutoff" —
    # a cutoff the flag often exists to disavow.
    if user.profile_needs_full_regen:
        return True
    last_node = (Node.query.filter_by(user_id=user.id)
                 .order_by(Node.created_at.desc()).first())
    if last_node and (datetime.utcnow() - last_node.created_at) < MIN_INACTIVITY:
        return False
    latest = _latest_non_integration_profile(user.id)
    # Pinned (pre-filled) accounts: a chain with at least one more full
    # minimum chunk beyond its cutoff is a rebuild in progress and must
    # continue regardless of the interval / 80k-new-token gates — those
    # measure organic growth, and a pre-filled corpus never grows. Without
    # this, a chunk lost to a worker restart (2026-08-27, MarvinKeilbach)
    # stalls the account forever. A tail smaller than a minimum chunk is
    # NOT chased: it waits for more data like everyone else's (uneven
    # chunks distort the iterative update more than an unused tail).
    if (user.profile_force_batch and latest is not None
            and latest.source_data_cutoff is not None):
        _, min_chunk = _exports.chunk_budget_for(user, _model_for(user))
        if _remaining_token_count(user, latest.source_data_cutoff) >= min_chunk:
            return True
    if latest:
        if (datetime.utcnow() - latest.created_at) < MIN_INTERVAL:
            return False
        # Null cutoff (e.g. a user-written profile): nothing has been folded in
        # yet, so all eligible data counts as new — measure it instead of
        # force-seeding (mirrors maybe_trigger_incremental_profile_update).
        cutoff = latest.source_data_cutoff
        new_tokens = _new_token_count(user, cutoff)
    else:
        new_tokens = _new_token_count(user, None)
    return new_tokens >= THRESHOLD_TOKENS


def _build_next_profile_request(user):
    """Build the request for the user's CURRENT step, or None if there's
    nothing to do. Mirrors the 'what's next' decision of
    _do_initial_generation / _do_incremental_update / _chunked_profile_loop /
    _do_integration, but produces a batch request instead of calling the LLM.

    Returns {"provider", "request", "meta"} or None.
    """
    model_id = _model_for(user)
    provider, api_model = _provider_and_model(model_id)

    # A pending full rebuild starts from scratch — ignore the existing
    # chain, mirroring the sync endpoint's force_full_regen → prev_id=None.
    # The flag is cleared once the from-scratch chunk 1 commits
    # (_apply_result), so subsequent chunks chain normally.
    prev = (None if user.profile_needs_full_regen
            else _latest_non_integration_profile(user.id))
    prev_id = prev.id if prev else None
    cutoff = prev.source_data_cutoff if prev else None
    cumulative = (prev.source_tokens_used or 0) if prev else 0

    # engaged_threads: profiles read the user's full conversational
    # scope — own threads AND replies in other users' threads (#110).
    # This also routes from-scratch builds (cutoff=None) through the
    # incremental machinery, which renders budget windows correctly via
    # entry-point preambles; the legacy authored_threads path silently
    # returned None whenever no thread *root* fit the budget window.
    budget, min_chunk = _exports.chunk_budget_for(user, model_id)
    chunk = _exports.build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        created_after=cutoff, chronological_order=True, return_metadata=True,
        include_strategy="engaged_threads")

    have_chunk = bool(chunk and chunk.get("content"))
    is_first_initial = prev is None
    # Tail-aware threshold: defer only a genuine corpus tail. A chunk
    # can re-measure below MIN_CHUNK_TOKENS while being a full budget
    # window (rendered chars/4 vs stored token_count unit mismatch);
    # if data remains beyond it, process it anyway.
    big_enough = have_chunk and (
        is_first_initial
        or chunk["token_count"] >= min_chunk
        or _exports._has_more_source_after(user, chunk["latest_node_created_at"]))

    if big_enough:
        if is_first_initial:
            gen_template = _exports._load_prompt(
                "profile_generation.txt", user_id=user.id)
            prompt = gen_template.replace(
                "{user_export}", _exports.chunk_content_for_prompt(chunk))
            generation_type = "iterative"
        else:
            prompt = _exports.build_chunk_prompt(
                _exports.build_update_template(user.id), prev.get_content(),
                cumulative, chunk, prev.source_origin_stats)
            generation_type = "update"
        latest_ts = chunk["latest_node_created_at"]
        # NB: Anthropic requires custom_id to match ^[a-zA-Z0-9_-]{1,64}$ —
        # no colons. Underscore-delimited, parsed nowhere (routing is by exact
        # match against the stored items), so the format is free to change.
        cid = f"profile_{user.id}_{prev_id or 0}_chunk"
        return {
            "provider": provider,
            "request": {
                "custom_id": cid, "model_id": model_id,
                "api_model": api_model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt}]}],
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            },
            "meta": {
                "custom_id": cid, "user_id": user.id, "kind": "chunk",
                "prev_profile_id": prev_id,
                "generation_type": generation_type,
                "prev_cumulative": cumulative,
                "origin_stats": _exports.merge_origin_stats(
                    prev.source_origin_stats if prev else None,
                    chunk.get("origin_stats")),
                "source_data_cutoff": (
                    latest_ts.isoformat() if latest_ts else None),
                "model_id": model_id,
                # chars/4 of the prompt: calibrates the next chunk's budget
                # against the provider-reported input_tokens (_apply_result).
                "prompt_tokens_est": _exports.approximate_token_count(prompt),
            },
        }

    # No (full-size) new data → integrate the chain if there are ≥2 versions
    # and we haven't already integrated this tip.
    if prev is not None:
        chain = _exports._collect_iterative_chain(prev.id)
        already = UserProfile.query.filter_by(
            user_id=user.id, generation_type="integration",
            parent_profile_id=prev.id).first()
        if len(chain) >= 2 and not already:
            messages, _chain = _exports.build_integration_messages(user.id, prev.id)
            if messages is not None:
                cid = f"profile_{user.id}_{prev.id}_integration"
                return {
                    "provider": provider,
                    "request": {
                        "custom_id": cid, "model_id": model_id,
                        "api_model": api_model, "messages": messages,
                        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                    },
                    "meta": {
                        "custom_id": cid, "user_id": user.id,
                        "kind": "integration", "prev_profile_id": prev.id,
                        "prev_source_tokens": prev.source_tokens_used,
                        "prev_origin_stats": prev.source_origin_stats,
                        "source_data_cutoff": (
                            prev.source_data_cutoff.isoformat()
                            if prev.source_data_cutoff else None),
                        "model_id": model_id,
                    },
                }
    return None


def _response_from_result(result):
    inp = result.get("input_tokens", 0)
    out = result.get("output_tokens", 0)
    return {"content": result["content"], "input_tokens": inp,
            "output_tokens": out, "total_tokens": inp + out}


def _apply_result(user, item, result, submitted_at):
    """Save a collected batch result (advancing the user's chain) and return
    the next request, or None if the pipeline is complete. Idempotent: a step
    that already produced its profile is not saved twice.

    The duplicate check is scoped to rows created after this job was
    submitted: it must only catch THIS item being applied twice (poll
    races), not rows from earlier runs. A repeated from-scratch rebuild
    reproduces its predecessor's exact key tuple (parent=None, same
    deterministic chunk-1 cutoff), and matching those historic rows
    discarded every result and re-submitted chunk 1 forever."""
    response = _response_from_result(result)
    cutoff = (datetime.fromisoformat(item["source_data_cutoff"])
              if item.get("source_data_cutoff") else None)

    if item["kind"] == "chunk":
        existing = UserProfile.query.filter_by(
            user_id=user.id, parent_profile_id=item["prev_profile_id"],
            source_data_cutoff=cutoff,
            generation_type=item["generation_type"]).filter(
            UserProfile.created_at >= submitted_at).first()
        if existing:
            # A duplicate of a step already applied (poll overlap / doubled
            # cohort). Advancing the chain from here would submit the SAME
            # next step twice — the duplicate-custom_id cascade. Stop.
            logger.info(f"User {user.id}: chunk already saved (idempotent) — "
                        f"duplicate item, not advancing")
            user.profile_batch_attempts = 0
            return None
        else:
            cumulative = item["prev_cumulative"] + response["input_tokens"]
            profile = _exports._save_profile(
                user, item["model_id"], response["content"], response,
                source_tokens_used=cumulative, source_data_cutoff=cutoff,
                generation_type=item["generation_type"],
                parent_profile_id=item["prev_profile_id"], batch=True,
                source_origin_stats=item.get("origin_stats"))
            # mirror PR #181: a from-scratch full regen is no longer needed
            # once its first chunk is committed. Only a from-scratch chunk
            # (prev_profile_id None) satisfies the flag — a flag set while
            # an incremental chunk was already in flight must survive that
            # chunk so the next build honors it.
            if (user.profile_needs_full_regen
                    and item["prev_profile_id"] is None):
                user.profile_needs_full_regen = False
            logger.info(
                f"User {user.id}: saved batch chunk profile {profile.id}")
        if item.get("prompt_tokens_est"):
            observed = _exports.record_token_ratio(
                user, item["model_id"], item["prompt_tokens_est"],
                response.get("input_tokens"))
            if observed is not None:
                logger.info(f"User {user.id}: batch chunk tokenizer "
                            f"calibration actual/estimated={observed}")
        user.profile_batch_attempts = 0
        return _build_next_profile_request(user)

    # integration (parent_profile_id is the chain tip, unique per run,
    # but scope by submission time anyway for consistency)
    existing = UserProfile.query.filter_by(
        user_id=user.id, generation_type="integration",
        parent_profile_id=item["prev_profile_id"]).filter(
        UserProfile.created_at >= submitted_at).first()
    if not existing:
        _exports._save_profile(
            user, item["model_id"], response["content"], response,
            source_tokens_used=item.get("prev_source_tokens"),
            source_data_cutoff=cutoff, generation_type="integration",
            parent_profile_id=item["prev_profile_id"], batch=True,
            source_origin_stats=item.get("prev_origin_stats"))
        logger.info(f"User {user.id}: saved batch integration profile")
        # Integration = the batch rebuild finished for this user (#207).
        from backend.utils.notifications import notify_profile_ready
        notify_profile_ready(user.id)
    user.profile_batch_attempts = 0
    return None


def _submit_requests(built, keys):
    """Group built requests by provider, submit one batch per provider/model,
    persist a ProfileBatchJob per returned batch id, and set guards.

    `built` items are not in flight until their batch id comes back; a failed
    submission clears the guard so the user is re-seeded next cycle."""
    # Invariant: ONE in-flight step per user. Drop duplicate custom_ids
    # (OpenAI rejects the whole batch) and extra steps for the same user
    # (they'd race each other on the chain); keep the first built.
    seen_users, seen_ids, unique = set(), set(), []
    for b in built:
        uid, cid = b["meta"]["user_id"], b["request"]["custom_id"]
        if uid in seen_users or cid in seen_ids:
            logger.warning(f"Dropping duplicate batch request {cid} for user {uid}")
            continue
        seen_users.add(uid); seen_ids.add(cid); unique.append(b)
    built = unique
    if not built:
        return 0
    requests_by_provider = {}
    for b in built:
        requests_by_provider.setdefault(b["provider"], []).append(b["request"])

    batch_ids = batch_submit(requests_by_provider, keys, "profile")

    items_by_key = {}
    for b in built:
        key = _provider_key(b["provider"], b["request"]["api_model"])
        items_by_key.setdefault(key, []).append(b["meta"])

    now = datetime.utcnow()
    submitted = 0
    for provider_key, items in items_by_key.items():
        batch_id = batch_ids.get(provider_key)
        if not batch_id:
            logger.warning(
                f"Batch submit failed for {provider_key}; "
                f"{len(items)} item(s) not in flight")
            for item in items:
                u = User.query.get(item["user_id"])
                if u:
                    u.profile_batch_pending = False
                    u.profile_batch_attempts = (u.profile_batch_attempts or 0) + 1
            db.session.commit()
            continue
        db.session.add(ProfileBatchJob(
            provider_key=provider_key, batch_id=batch_id, status="pending",
            items=items, submitted_at=now))
        for item in items:
            u = User.query.get(item["user_id"])
            if u:
                u.profile_batch_pending = True
        db.session.commit()
        submitted += len(items)
        logger.info(f"Profile batch {batch_id} ({provider_key}): "
                    f"{len(items)} item(s) submitted")
    return submitted


def _fail_job(job, reason):
    job.status = "failed"
    job.collected_at = datetime.utcnow()
    for item in job.items:
        u = User.query.get(item["user_id"])
        if u:
            u.profile_batch_pending = False
            u.profile_batch_attempts = (u.profile_batch_attempts or 0) + 1
    db.session.commit()
    logger.warning(f"Profile batch {job.batch_id} failed ({reason})")


# ── scheduled tasks ───────────────────────────────────────────────────

@celery.task
def seed_profile_batches():
    """Hourly: submit one cohort batch of current-step requests for
    batch-selected, eligible users not already in flight."""
    with flask_app.app_context():
        with batch_pipeline_lock() as ok:
            if ok:
                _seed_profile_batches()


def _seed_profile_batches(users=None):
    """Impl — runs inside an active app context (testable directly).
    ``users`` restricts the cohort (immediate seed for one user); the
    default is every profile-eligible user."""
    config = current_app.config
    if config.get("PROFILE_UPDATES_PAUSED"):
        logger.info("PROFILE_UPDATES_PAUSED — skipping batch seeder")
        return 0
    keys = apply_batch_key_override(
        get_api_keys_for_usage(config, 'chat'), config)
    built = []
    if users is None:
        users = User.profile_eligible_query().all()
    for user in users:
        if user.profile_batch_pending:
            continue
        if not use_batch_for_user(user, config):
            continue
        if ((user.profile_batch_attempts or 0) >= MAX_BATCH_ATTEMPTS
                and not user.profile_force_batch):
            continue  # exhausted → synchronous last-resort handles it
        # (force-batch users keep retrying here every cycle instead:
        # they must never fall back to the full-price sync path)
        if not _should_seed(user):
            continue
        try:
            req = _build_next_profile_request(user)
        except Exception as e:
            logger.warning(
                f"Build batch request failed for user {user.id}: {e}")
            continue
        if req:
            built.append(req)
    return _submit_requests(built, keys)


@celery.task
def seed_profile_batch_for_user(user_id):
    """Immediate seed for one user (admin pre-fill): same gates as the
    hourly seeder, without waiting for it. Returns the number actually
    put in flight (0 when the provider rejected the submit)."""
    with flask_app.app_context():
        user = User.query.get(user_id)
        if not user:
            return 0
        with batch_pipeline_lock() as ok:
            if not ok:
                logger.info(f"User {user_id}: immediate seed skipped (pass in "
                            f"progress); the hourly seeder will pick it up")
                return 0
            n = _seed_profile_batches(users=[user])
        logger.info(f"User {user_id}: immediate batch seed → {n} request(s)")
        return n


@celery.task
def poll_profile_batches():
    """~Every 60s: collect finished batches, advance each user's chain, and
    submit the cohort's next step."""
    with flask_app.app_context():
        with batch_pipeline_lock() as ok:
            if ok:
                _poll_profile_batches()


def _poll_profile_batches():
    """Impl — runs inside an active app context (testable directly)."""
    config = current_app.config
    keys = apply_batch_key_override(
        get_api_keys_for_usage(config, 'chat'), config)
    next_built = []
    for job in ProfileBatchJob.query.filter_by(status="pending").all():
        if datetime.utcnow() - job.submitted_at > BATCH_STALE_AFTER:
            _fail_job(job, "stale")
            continue
        try:
            results, still_pending, _ = batch_check_and_collect(
                {job.provider_key: job.batch_id}, keys)
        except Exception as e:
            logger.warning(f"Poll failed for batch {job.batch_id}: {e}")
            continue
        if job.provider_key in still_pending:
            continue  # not ended yet

        for item in job.items:
            user = User.query.get(item["user_id"])
            if not user:
                continue
            result = results.get(item["custom_id"])
            if result is None:
                user.profile_batch_attempts = (
                    user.profile_batch_attempts or 0) + 1
                user.profile_batch_pending = False
                db.session.commit()
                logger.warning(
                    f"Batch item failed for user {user.id} "
                    f"({item['custom_id']}); attempt "
                    f"{user.profile_batch_attempts}")
                continue
            try:
                nxt = _apply_result(user, item, result, job.submitted_at)
            except Exception as e:
                logger.error(
                    f"Apply batch result failed for user {user.id}: {e}",
                    exc_info=True)
                user.profile_batch_pending = False
                db.session.commit()
                continue
            if nxt:
                next_built.append(nxt)   # stays pending; re-submitted below
            else:
                user.profile_batch_pending = False
            db.session.commit()

        job.status = "collected"
        job.collected_at = datetime.utcnow()
        db.session.commit()

    _submit_requests(next_built, keys)
