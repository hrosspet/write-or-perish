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
from backend.tasks.exports import (
    CHUNK_BUDGET, MIN_CHUNK_TOKENS,
    build_user_export_content, build_update_template, build_chunk_prompt,
    build_integration_messages, _save_profile, _load_prompt,
    _collect_iterative_chain,
)

logger = get_task_logger(__name__)

# Mirror maybe_trigger_incremental_profile_update's gates (exports.py).
THRESHOLD_TOKENS = 80000
MIN_INACTIVITY = timedelta(minutes=30)
MIN_INTERVAL = timedelta(hours=1)

MAX_BATCH_ATTEMPTS = 3              # batch retries before sync last-resort
BATCH_STALE_AFTER = timedelta(hours=24)   # provider SLA ceiling


def use_batch_for_user(user, config):
    """A user takes the Batch path if the global switch is on OR their id is
    in the canary allowlist (issue #173)."""
    return (bool(config.get("PROFILE_USE_BATCH"))
            or user.id in config.get("PROFILE_BATCH_USER_IDS", set()))


# ── helpers ────────────────────────────────────────────────────────────

def _model_for(user):
    return (user.preferred_model
            or current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6"))


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


def _should_seed(user):
    """Whether the user has crossed the trigger gates right now. Mirrors
    maybe_trigger_incremental_profile_update (inactivity, interval, tokens)
    without dispatching."""
    last_node = (Node.query.filter_by(user_id=user.id)
                 .order_by(Node.created_at.desc()).first())
    if last_node and (datetime.utcnow() - last_node.created_at) < MIN_INACTIVITY:
        return False
    latest = _latest_non_integration_profile(user.id)
    if latest:
        if (datetime.utcnow() - latest.created_at) < MIN_INTERVAL:
            return False
        cutoff = latest.source_data_cutoff
        new_tokens = (_new_token_count(user, cutoff) if cutoff
                      else THRESHOLD_TOKENS)
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

    prev = _latest_non_integration_profile(user.id)
    prev_id = prev.id if prev else None
    cutoff = prev.source_data_cutoff if prev else None
    cumulative = (prev.source_tokens_used or 0) if prev else 0

    chunk = build_user_export_content(
        user, max_tokens=CHUNK_BUDGET, filter_ai_usage=True,
        created_after=cutoff, chronological_order=True, return_metadata=True)

    have_chunk = bool(chunk and chunk.get("content"))
    is_first_initial = prev is None
    big_enough = have_chunk and (
        is_first_initial or chunk["token_count"] >= MIN_CHUNK_TOKENS)

    if big_enough:
        if is_first_initial:
            gen_template = _load_prompt(
                "profile_generation.txt", user_id=user.id)
            prompt = gen_template.replace("{user_export}", chunk["content"])
            generation_type = "iterative"
        else:
            prompt = build_chunk_prompt(
                build_update_template(user.id), prev.get_content(),
                cumulative, chunk)
            generation_type = "update"
        latest_ts = chunk["latest_node_created_at"]
        cid = f"profile:{user.id}:{prev_id or 0}:chunk"
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
                "source_data_cutoff": (
                    latest_ts.isoformat() if latest_ts else None),
                "model_id": model_id,
            },
        }

    # No (full-size) new data → integrate the chain if there are ≥2 versions
    # and we haven't already integrated this tip.
    if prev is not None:
        chain = _collect_iterative_chain(prev.id)
        already = UserProfile.query.filter_by(
            user_id=user.id, generation_type="integration",
            parent_profile_id=prev.id).first()
        if len(chain) >= 2 and not already:
            messages, _chain = build_integration_messages(user.id, prev.id)
            if messages is not None:
                cid = f"profile:{user.id}:{prev.id}:integration"
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


def _apply_result(user, item, result):
    """Save a collected batch result (advancing the user's chain) and return
    the next request, or None if the pipeline is complete. Idempotent: a step
    that already produced its profile is not saved twice."""
    response = _response_from_result(result)
    cutoff = (datetime.fromisoformat(item["source_data_cutoff"])
              if item.get("source_data_cutoff") else None)

    if item["kind"] == "chunk":
        existing = UserProfile.query.filter_by(
            user_id=user.id, parent_profile_id=item["prev_profile_id"],
            source_data_cutoff=cutoff,
            generation_type=item["generation_type"]).first()
        if existing:
            logger.info(f"User {user.id}: chunk already saved (idempotent)")
        else:
            cumulative = item["prev_cumulative"] + response["input_tokens"]
            profile = _save_profile(
                user, item["model_id"], response["content"], response,
                source_tokens_used=cumulative, source_data_cutoff=cutoff,
                generation_type=item["generation_type"],
                parent_profile_id=item["prev_profile_id"], batch=True)
            # mirror PR #181: a from-scratch full regen is no longer needed
            # once the first chunk is committed.
            if user.profile_needs_full_regen:
                user.profile_needs_full_regen = False
            logger.info(
                f"User {user.id}: saved batch chunk profile {profile.id}")
        user.profile_batch_attempts = 0
        return _build_next_profile_request(user)

    # integration
    existing = UserProfile.query.filter_by(
        user_id=user.id, generation_type="integration",
        parent_profile_id=item["prev_profile_id"]).first()
    if not existing:
        _save_profile(
            user, item["model_id"], response["content"], response,
            source_tokens_used=item.get("prev_source_tokens"),
            source_data_cutoff=cutoff, generation_type="integration",
            parent_profile_id=item["prev_profile_id"], batch=True)
        logger.info(f"User {user.id}: saved batch integration profile")
    user.profile_batch_attempts = 0
    return None


def _submit_requests(built, keys):
    """Group built requests by provider, submit one batch per provider/model,
    persist a ProfileBatchJob per returned batch id, and set guards.

    `built` items are not in flight until their batch id comes back; a failed
    submission clears the guard so the user is re-seeded next cycle."""
    if not built:
        return
    requests_by_provider = {}
    for b in built:
        requests_by_provider.setdefault(b["provider"], []).append(b["request"])

    batch_ids = batch_submit(requests_by_provider, keys, "profile")

    items_by_key = {}
    for b in built:
        key = _provider_key(b["provider"], b["request"]["api_model"])
        items_by_key.setdefault(key, []).append(b["meta"])

    now = datetime.utcnow()
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
        logger.info(f"Profile batch {batch_id} ({provider_key}): "
                    f"{len(items)} item(s) submitted")


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
        _seed_profile_batches()


def _seed_profile_batches():
    """Impl — runs inside an active app context (testable directly)."""
    config = current_app.config
    keys = apply_batch_key_override(
        get_api_keys_for_usage(config, 'chat'), config)
    built = []
    for user in User.profile_eligible_query().all():
        if user.profile_batch_pending:
            continue
        if not use_batch_for_user(user, config):
            continue
        if (user.profile_batch_attempts or 0) >= MAX_BATCH_ATTEMPTS:
            continue  # exhausted → synchronous last-resort handles it
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
    _submit_requests(built, keys)


@celery.task
def poll_profile_batches():
    """~Every 60s: collect finished batches, advance each user's chain, and
    submit the cohort's next step."""
    with flask_app.app_context():
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
                nxt = _apply_result(user, item, result)
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
