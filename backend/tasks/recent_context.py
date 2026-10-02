"""
Celery tasks for generating recent context summaries.

Recent context sits between the long-term user profile (~monthly) and raw data,
providing ~500-800 token summaries regenerated every ~10k new tokens of user writing.

Nobody waits on a recent context, so it is generated through the provider
Batch API (#380, ~50% cheaper, <=24h SLA): the 10-minute check submits one
request per due user as a batch (check_pending_recent_context_updates) and a
beat collector saves the results (collect_recent_context_batches). Until a
user's batch is collected, prompts keep reading their previous summary:
nothing is removed or replaced while a request is in flight, and an empty
result is never saved. The batch request uses the model the direct call
would (default_model_for) — the same provider, never a fallback.

The synchronous generate_recent_context task stays as the direct path; no
scheduler dispatches it.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from celery.utils.log import get_task_logger
from flask import current_app
from sqlalchemy import func, or_

from backend.celery_app import celery, flask_app
from backend.models import (
    User, UserProfile, UserRecentContext, Node, APICostLog,
    RecentContextBatchJob,
)
from backend.utils.privacy import AI_ALLOWED, account_allows_ai
from backend.extensions import db
from backend.llm_providers import (
    LLMProvider, PromptTooLongError, is_empty_truncated,
    EmptyTruncatedOutputError, DEFAULT_MAX_OUTPUT_TOKENS, model_input_cap,
    fit_by_count)
from backend.utils.tokens import reduce_export_tokens, format_date_metadata
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import llm_cost_log_fields
from backend.utils.chunk_plan import UPDATE_THRESHOLD_UNITS
from backend.utils.llm_batch import (
    BATCH_JOB_MAX_AGE, apply_batch_key_override, batch_check_and_collect,
    batch_submit,
)
from backend.utils import refusal_backoff

logger = get_task_logger(__name__)

RECENT_CONTEXT_TOKEN_THRESHOLD = 10000
MIN_INACTIVITY = timedelta(minutes=5)
MIN_GENERATION_INTERVAL = timedelta(minutes=5)


def _get_latest_chat_profile(user_id):
    """Return the latest profile with ai_usage in ('chat', 'train'), or None."""
    return (
        UserProfile.query
        .filter_by(user_id=user_id)
        .filter(UserProfile.ai_usage.in_(AI_ALLOWED))
        .order_by(UserProfile.created_at.desc())
        .first()
    )


def _get_latest_recent_context(user_id, profile_id=None):
    """Return the latest UserRecentContext for this user+profile combo."""
    q = UserRecentContext.query.filter_by(user_id=user_id)
    if profile_id is not None:
        q = q.filter_by(profile_id=profile_id)
    else:
        q = q.filter(UserRecentContext.profile_id.is_(None))
    return q.order_by(UserRecentContext.created_at.desc()).first()


def _count_new_tokens(user_id, since):
    """Count tokens of nodes owned by user created strictly after *since*.

    *since* is the `created_at` of the last node already summarized, so the
    boundary node itself must be excluded — otherwise its own `token_count`
    keeps the threshold tripped on static data and the summary regenerates
    on every beat.

    Soft-deleted nodes are also excluded — they're slated for purge and
    shouldn't keep the summary threshold tripped on content the user is
    actively trying to remove.
    """
    return db.session.query(
        func.coalesce(func.sum(Node.token_count), 0)
    ).filter(
        Node.deleted_at.is_(None),
        or_(Node.user_id == user_id,
            Node.human_owner_id == user_id),
        Node.created_at > since,
        Node.ai_usage.in_(AI_ALLOWED),
    ).scalar()


def _count_total_eligible_tokens(user_id):
    """Count all eligible tokens for a user (no cutoff). Excludes soft-deleted."""
    return db.session.query(
        func.coalesce(func.sum(Node.token_count), 0)
    ).filter(
        Node.deleted_at.is_(None),
        or_(Node.user_id == user_id,
            Node.human_owner_id == user_id),
        Node.ai_usage.in_(AI_ALLOWED),
    ).scalar()


def _load_prompt(name, user_id=None):
    """Load a prompt template, checking user overrides first."""
    import os
    if user_id:
        from backend.utils.prompts import get_user_prompt
        prompt_key = name.rsplit('.', 1)[0] if '.' in name else name
        content = get_user_prompt(user_id, prompt_key)
        if content:
            return content
    path = os.path.join(flask_app.root_path, "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _should_generate_recent_context(user):
    """Check if recent context generation should be triggered for this user.

    Returns (should_generate, profile, cutoff_for_data) or (False, None, None).
    """
    user_id = user.id

    # Only for Voice-Mode plans
    if (user.plan or "free") not in User.VOICE_MODE_PLANS:
        return False, None, None

    # User must be inactive for 5+ min
    last_node = (
        Node.query.filter_by(user_id=user_id)
        .order_by(Node.created_at.desc())
        .first()
    )
    if last_node and (datetime.utcnow() - last_node.created_at) < MIN_INACTIVITY:
        return False, None, None

    # Find current profile (if any)
    profile = _get_latest_chat_profile(user_id)
    profile_id = profile.id if profile else None
    profile_cutoff = profile.source_data_cutoff if profile else None

    # Check: profile update NOT imminent
    # Total new tokens since profile cutoff must be < 80k
    if profile_cutoff:
        total_since_profile = _count_new_tokens(user_id, profile_cutoff)
    else:
        total_since_profile = _count_total_eligible_tokens(user_id)

    if total_since_profile >= UPDATE_THRESHOLD_UNITS:
        logger.debug(
            f"User {user_id}: skipping recent context — profile update "
            f"imminent ({total_since_profile} tokens)"
        )
        return False, None, None

    # Find latest recent context for this profile
    latest_rc = _get_latest_recent_context(user_id, profile_id)

    # No recent context created in last 5 min (concurrency guard)
    if latest_rc and (datetime.utcnow() - latest_rc.created_at) < MIN_GENERATION_INTERVAL:
        return False, None, None

    # Determine cutoff: data since the profile's cutoff (or all data if no profile)
    data_cutoff = profile_cutoff  # may be None

    # Count new tokens since the latest recent context's cutoff
    # (or since profile cutoff if no recent context yet)
    if latest_rc and latest_rc.source_data_cutoff:
        tokens_since_last_rc = _count_new_tokens(
            user_id, latest_rc.source_data_cutoff
        )
    elif data_cutoff:
        tokens_since_last_rc = _count_new_tokens(user_id, data_cutoff)
    else:
        tokens_since_last_rc = _count_total_eligible_tokens(user_id)

    if tokens_since_last_rc < RECENT_CONTEXT_TOKEN_THRESHOLD:
        return False, None, None

    # After a refused (cut-off) output or a failed batch item nothing was
    # saved, so the gates above stay open and the same request would
    # repeat every 10 minutes (#368, #380): one more try after an hour,
    # then stopped until a new recent context or profile version is
    # saved. Checked last: it reads the batch jobs collected since the
    # last summary, so only users who are otherwise due pay for it.
    if refusal_backoff.recent_context_in_backoff(user_id):
        return False, None, None

    return True, profile, data_cutoff


def _build_recent_export(user, data_cutoff, max_tokens=None):
    """ALL of the user's AI-readable writing since *data_cutoff* (the
    profile's cutoff; None = everything), oldest first."""
    from backend.routes.export_data import (
        build_user_export_content as _build_export
    )
    return _build_export(
        user,
        max_tokens=max_tokens,
        filter_ai_usage=True,
        created_after=data_cutoff,
        chronological_order=True,
        return_metadata=True,
        collapse_artifacts=True,
    )


def _prepare_recent_context(user, profile_id, data_cutoff):
    """The checks, model and input one generation runs on, shared by the
    batch sweep and the direct path. Returns a dict (model_id, profile_id,
    export, prompt_template, profile_content), or None when this
    generation must not run."""
    user_id = user.id
    from backend.utils.spend import user_is_capped
    if user_is_capped(user):
        logger.info(
            "User %s is spend-capped; skipping recent context", user_id)
        return None
    if not account_allows_ai(user):
        # Re-checked here: the setting may change after dispatch (#346).
        logger.info(
            "User %s has opted out of AI usage; skipping recent context",
            user_id)
        return None

    # Re-check concurrency: no recent context created in last 5 min
    profile = UserProfile.query.get(profile_id) if profile_id else None
    pid = profile.id if profile else None
    latest_rc = _get_latest_recent_context(user_id, pid)
    if latest_rc and (datetime.utcnow() - latest_rc.created_at) < MIN_GENERATION_INTERVAL:
        logger.info(
            f"User {user_id}: recent context was just generated, skipping"
        )
        return None

    # Determine the model to use (same as profile generation). The
    # fallback must be a valid SUPPORTED_MODELS key — the old hardcoded
    # "claude-opus-4-6" (hyphen) is the *api_model*, not the internal id
    # ("claude-opus-4.6"), so it failed get_completion's lookup.
    from backend.utils.llm_nodes import default_model_for
    model_id = default_model_for(user)

    # Build data: ALL nodes since profile cutoff
    export_result = _build_recent_export(user, data_cutoff)
    if not export_result:
        logger.debug(f"User {user_id}: no data for recent context")
        return None

    latest_ts = export_result["latest_node_created_at"]

    # Belt-and-suspenders guard: if the export's newest node has not
    # advanced past the previous RC's cutoff, the LLM call would
    # regenerate on identical data. `_count_new_tokens` can still
    # return ≥ threshold when the user has replied in pre-profile-
    # cutoff top-level threads — those replies are counted but the
    # export filters their top-level ancestor out, so they never
    # land in `recent_data`. Skip before paying for the LLM call.
    if (latest_rc and latest_rc.source_data_cutoff
            and latest_ts
            and latest_ts <= latest_rc.source_data_cutoff):
        logger.info(
            f"User {user_id}: export latest_ts ({latest_ts}) has not "
            f"advanced past previous RC cutoff "
            f"({latest_rc.source_data_cutoff}); skipping regen"
        )
        return None

    # Load prompt template
    prompt_template = _load_prompt("recent_context.txt", user_id=user_id)

    # Inject profile content (if available)
    profile_content = ""
    if profile and profile.ai_usage in AI_ALLOWED:
        profile_content = profile.get_content()

    return {
        "model_id": model_id,
        "profile_id": pid,
        "export": export_result,
        "prompt_template": prompt_template,
        "profile_content": profile_content,
    }


def _messages_for(prep, recent_data):
    prompt_text = prep["prompt_template"].replace(
        "{user_profile}", prep["profile_content"])
    prompt_text = prompt_text.replace("{recent_data}", recent_data)
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}]
        }
    ]


def _save_recent_context(user, model_id, summary_text, tokens_used,
                         source_tokens, latest_ts, data_cutoff, profile_id):
    """Add the new UserRecentContext row (the caller commits)."""
    rc = UserRecentContext(
        user_id=user.id,
        generated_by=model_id,
        tokens_used=tokens_used,
        source_data_cutoff=latest_ts,
        source_tokens_covered=source_tokens,
        profile_id=profile_id,
        # ai_usage mirrors the user's global default; generation is gated
        # to 'chat'/'train' users in profile_eligible_query (#191).
        ai_usage=user.default_ai_usage,
    )
    rc.set_content(
        format_date_metadata(
            covers_start=data_cutoff, covers_end=latest_ts,
            tokens=source_tokens,
        ) + summary_text
    )
    db.session.add(rc)
    return rc


def _iso(ts):
    return ts.isoformat() if ts else None


def _from_iso(value):
    return datetime.fromisoformat(value) if value else None


# ── Batch path (#380): the 10-minute check submits, a collector saves ──

def _batch_keys():
    return apply_batch_key_override(
        get_api_keys_for_usage(current_app.config, "chat"),
        current_app.config)


# One lock for the submit check and the collector's claim-to-save step.
# The check reads the users in flight (pending jobs) once, at its start,
# and the collector's claim takes a user out of flight before their
# summary is saved. Without the lock, two overlapping checks (beat
# messages queued up behind busy workers), or a check that reads during a
# collection, could submit the same user twice: billed twice, the second
# result discarded by the "already covered" guard.
RC_BATCH_LOCK_KEY = "loore:recent_context_batch:lock"
RC_BATCH_LOCK_TTL = 30 * 60   # seconds; a pass ends well within this


def _lock_redis():
    import redis
    return redis.Redis.from_url(
        current_app.config.get("CELERY_BROKER_URL",
                               "redis://localhost:6379/0"),
        socket_connect_timeout=2)


@contextmanager
def recent_context_batch_lock():
    """Held by the check for its whole pass and by the collector from the
    claim of an ended job until its outcomes are saved. Same pattern as
    profile_batch.batch_pipeline_lock: a non-blocking acquire with a TTL;
    yields False when another pass holds it (the caller skips this run),
    True when acquired or when Redis is unreachable (tests, local: run
    unlocked, as before). The release only deletes this pass's own lock,
    in case the TTL ran out and another pass took it."""
    lock = None
    try:
        lock = _lock_redis().lock(RC_BATCH_LOCK_KEY,
                                  timeout=RC_BATCH_LOCK_TTL)
        acquired = bool(lock.acquire(blocking=False))
    except Exception as e:  # no Redis → run unlocked
        logger.warning("Recent-context batch lock unavailable (%s); "
                       "running unlocked", e)
        lock, acquired = None, True
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception as e:
                logger.warning("Recent-context batch lock release failed "
                               "(%s); it expires after its TTL", e)


def _users_in_pending_batches():
    pending = RecentContextBatchJob.query.filter_by(status="pending").all()
    return {item["user_id"] for job in pending for item in job.items}


def _build_batch_request(user, profile, data_cutoff):
    """The batch request for one due user: the same checks, model, prompt
    and data window as the direct path. Returns {"provider", "request",
    "meta"}, or None when nothing is to be generated."""
    prep = _prepare_recent_context(
        user, profile.id if profile else None, data_cutoff)
    if prep is None:
        return None
    model_id = prep["model_id"]
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id)
    if cfg is None:
        logger.warning("Recent context for user %s skipped: model %s not "
                       "configured", user.id, model_id)
        return None
    # The output cap get_completion gives the direct call by default.
    max_tokens = min(cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                     DEFAULT_MAX_OUTPUT_TOKENS)

    def build(budget):
        export = _build_recent_export(user, data_cutoff, max_tokens=budget)
        return (_messages_for(prep, export["content"]), export) \
            if export else None

    # A batch request gets no prompt-too-long retry: an oversize prompt
    # comes back as a failed item hours later. So the window is sized
    # BEFORE submitting (counted, and shrunk if over the model's input
    # cap), as the direct path shrinks it after a rejection. strict=False:
    # if counting cannot get it under, the closest build goes out and a
    # failed item counts in the backoff.
    built, _budget, _tokens = fit_by_count(
        model_id, get_api_keys_for_usage(current_app.config, "chat"),
        model_input_cap(cfg, max_tokens), None, build,
        corpus_tokens=prep["export"]["token_count"],
        first_built=(_messages_for(prep, prep["export"]["content"]),
                     prep["export"]),
        strict=False)
    if not built:
        return None
    messages, export = built
    # Anthropic custom_id: ^[a-zA-Z0-9_-]{1,64}$; one item per user.
    custom_id = f"recent_context_{user.id}"
    return {
        "provider": cfg["provider"],
        "request": {
            "custom_id": custom_id,
            "model_id": model_id,
            "api_model": cfg["api_model"],
            "messages": messages,
            "max_tokens": max_tokens,
        },
        "meta": {
            "custom_id": custom_id,
            "user_id": user.id,
            "profile_id": prep["profile_id"],
            "model_id": model_id,
            "data_cutoff": _iso(data_cutoff),
            "source_data_cutoff": _iso(export["latest_node_created_at"]),
            "source_tokens": export["token_count"],
        },
    }


def _submit_batch(built):
    """Submit the built requests, one batch per provider (and OpenAI
    model), and persist a job per batch. Returns the number of users put
    in flight."""
    if not built:
        return 0
    requests_by_provider = {}
    for b in built:
        requests_by_provider.setdefault(b["provider"], []).append(
            b["request"])
    batch_ids = batch_submit(requests_by_provider, _batch_keys(),
                             "recent_context")
    submitted = 0
    for provider_key, batch_id in batch_ids.items():
        # batch_submit keys OpenAI jobs "openai:<api_model>"; route each
        # request's metadata to the job that carries it.
        provider, _, api_model = provider_key.partition(":")
        items = [
            b["meta"] for b in built
            if b["provider"] == provider
            and (not api_model or b["request"]["api_model"] == api_model)
        ]
        db.session.add(RecentContextBatchJob(
            provider_key=provider_key, batch_id=batch_id, items=items))
        submitted += len(items)
    db.session.commit()
    # batch_submit logs (not raises) a provider's failure; those users have
    # nothing in flight and the next check submits them again.
    return submitted


@celery.task
def check_pending_recent_context_updates():
    """Periodic task (every 10 min): submit a batch request for each user
    whose recent context is due."""
    with flask_app.app_context():
        return _check_pending_recent_context_updates()


def _check_pending_recent_context_updates():
    """Core of the periodic check (plain function so tests can call it
    without the Celery machinery)."""
    if current_app.config.get("PROFILE_UPDATES_PAUSED"):
        logger.info(
            "PROFILE_UPDATES_PAUSED — skipping recent-context check")
        return {"status": "paused", "submitted": 0}
    # Under the lock the collector takes from claim to save, so the users
    # in flight read below stay in flight until this pass has committed
    # its own jobs (see RC_BATCH_LOCK_KEY).
    with recent_context_batch_lock() as acquired:
        if not acquired:
            logger.info("Recent context check skipped: a check or a "
                        "collection holds the lock; the next check runs "
                        "in 10 minutes")
            return {"status": "locked", "submitted": 0}
        return _submit_due_users()


def _submit_due_users():
    """The check's pass, run under the lock: submit one request per due
    user who has none in flight."""
    # Voice-Mode users minus the llm-<model> placeholder accounts.
    # Shared helper keeps NULL-twitter_id (email signup) users in —
    # a bare NOT LIKE drops them (NULL NOT LIKE = NULL). See
    # User.profile_eligible_query.
    users = User.profile_eligible_query().all()
    # One request per user at a time; their previous summary stays in use
    # until it is collected.
    in_flight = _users_in_pending_batches()
    built = []
    for user in users:
        if user.id in in_flight:
            continue
        try:
            should, profile, data_cutoff = _should_generate_recent_context(user)
            req = (_build_batch_request(user, profile, data_cutoff)
                   if should else None)
        except Exception as e:
            db.session.rollback()
            logger.warning(
                f"Recent context check failed for user {user.id}: {e}"
            )
            continue
        if req:
            built.append(req)
    submitted = _submit_batch(built)
    logger.info(
        f"Recent context check: {len(users)} users, "
        f"{submitted} submitted to batch"
    )
    return {"status": "ok", "submitted": submitted}


def _apply_item(item, result, batch_id):
    """Save one collected item's summary. Returns the item's outcome:
    saved, refused (cut off before any text), failed or skipped."""
    user_id = item["user_id"]
    if result is None:
        # Errored, expired or missing at the provider: not billed.
        logger.warning(
            "Recent-context item %s of batch %s has no result; nothing "
            "saved, the previous summary stays", item["custom_id"],
            batch_id)
        return "failed"
    user = User.query.get(user_id)
    if user is None:
        logger.info("Recent-context item %s: user %s no longer exists",
                    item["custom_id"], user_id)
        return "skipped"
    model_id = item["model_id"]
    try:
        # The cost row first and on its own: the call was billed whatever
        # happens to the summary. Batch price, on the human owner.
        cost_log = APICostLog(
            user_id=user_id,
            model_id=model_id,
            request_type="recent_context",
            **llm_cost_log_fields(model_id, result, batch=True),
        )
        summary_text = result.get("content") or ""
        truncated = bool(result.get("truncated"))
        if not summary_text.strip():
            # Billed but empty: save nothing, keep the previous summary.
            # Cut off before any text = a refusal for the backoff (#368).
            if truncated:
                cost_log.request_ref = refusal_backoff.REFUSED_REF
            db.session.add(cost_log)
            db.session.commit()
            logger.warning(
                "Empty recent-context output for user %s (model %s, "
                "truncated=%s, output_tokens=%s): nothing saved, the "
                "previous one stays", user_id, model_id, truncated,
                result.get("output_tokens"))
            return "refused" if truncated else "failed"
        db.session.add(cost_log)
        db.session.commit()

        if not account_allows_ai(user):
            # Opted out while the batch ran (#346): don't store it.
            logger.info("User %s opted out of AI usage while their recent "
                        "context ran; not saved", user_id)
            return "skipped"
        profile_id = item.get("profile_id")
        latest_ts = _from_iso(item.get("source_data_cutoff"))
        # The direct path's guard, re-checked at save time: a summary that
        # already covers this window (an overlapping run) wins.
        latest_rc = _get_latest_recent_context(user_id, profile_id)
        if (latest_rc and latest_rc.source_data_cutoff and latest_ts
                and latest_ts <= latest_rc.source_data_cutoff):
            logger.info("User %s: a recent context already covers up to %s; "
                        "batch result not saved", user_id,
                        latest_rc.source_data_cutoff)
            return "skipped"
        _save_recent_context(
            user, model_id, summary_text,
            tokens_used=(cost_log.input_tokens or 0)
            + (cost_log.output_tokens or 0),
            source_tokens=item.get("source_tokens"),
            latest_ts=latest_ts,
            data_cutoff=_from_iso(item.get("data_cutoff")),
            profile_id=profile_id)
        db.session.commit()
        logger.info(
            "Recent context for user %s saved from batch %s: %d chars, "
            "covers %s source tokens", user_id, batch_id, len(summary_text),
            item.get("source_tokens"))
        return "saved"
    except Exception:
        db.session.rollback()
        logger.exception("Saving recent-context item %s of batch %s failed",
                         item["custom_id"], batch_id)
        return "failed"


def _report_unsaved(items):
    """After a job closes: for each refused or failed item, log when the
    user's next try is allowed, or report the stop (the second strike in
    a row, utils/refusal_backoff.py)."""
    for item in items:
        if item.get("outcome") not in ("refused", "failed"):
            continue
        n, until, stopped = refusal_backoff.recent_context_backoff_state(
            item["user_id"])
        if stopped:
            refusal_backoff.report_stop(
                item["user_id"], "recent context", n, item["model_id"],
                "recent_context", cause="batch item failed or cut off")
        else:
            logger.warning(
                "Recent context for user %s: batch item %s; next try after "
                "%s", item["user_id"], item["outcome"], until)


def _collect_recent_context_batches():
    """Core of the collector (plain function so tests can call it without
    the Celery machinery)."""
    jobs = RecentContextBatchJob.query.filter_by(status="pending").all()
    if not jobs:
        return {"collected": 0, "abandoned": 0}

    keys = _batch_keys()
    now = datetime.utcnow()
    collected = abandoned = 0
    ended = []
    for job in jobs:
        try:
            results, still_pending, _ = batch_check_and_collect(
                {job.provider_key: job.batch_id}, keys)
        except Exception as e:
            # A warning, not an error: this runs every minute, and a batch
            # that stays unreadable is reported once, when it is abandoned.
            logger.warning("Collect failed for recent-context batch %s: %s",
                           job.batch_id, e)
            # Unreadable, not merely unfinished — the case the age
            # backstop exists for, so it must apply here too.
            still_pending = {job.provider_key: job.batch_id}
            results = {}
        if still_pending:
            if now - job.submitted_at > BATCH_JOB_MAX_AGE:
                logger.error(
                    "Recent-context batch %s not ended after %s; "
                    "abandoning (%d users keep their previous summary)",
                    job.batch_id, BATCH_JOB_MAX_AGE, len(job.items))
                # Status and outcomes in one commit: a check sees these
                # users either in flight or failed, so the abandon needs
                # no lock.
                job.items = [dict(item, outcome="failed")
                             for item in job.items]
                job.status = "abandoned"
                job.collected_at = now
                db.session.commit()
                _report_unsaved(job.items)
                abandoned += 1
            continue
        ended.append((job, results))

    if not ended:
        return {"collected": 0, "abandoned": abandoned}
    # From the claim until the outcomes are saved, a claimed job's users
    # are neither in flight nor covered by a saved summary. The lock keeps
    # the check from reading them in that window (RC_BATCH_LOCK_KEY).
    with recent_context_batch_lock() as acquired:
        if not acquired:
            logger.info("Recent-context collection deferred: a check or "
                        "another collection holds the lock; %d ended "
                        "batch(es) are collected next run", len(ended))
            return {"collected": 0, "abandoned": abandoned}
        for job, results in ended:
            collected += _claim_and_apply(job, results, now)
    return {"collected": collected, "abandoned": abandoned}


def _claim_and_apply(job, results, now):
    """Claim an ended job and save its items. Returns 1 if this run
    claimed it, 0 if another collector run already had."""
    # Claim the job before saving anything, so a collector run
    # overlapping this one (the beat fires every minute) cannot save the
    # same summaries twice.
    batch_id, items = job.batch_id, [dict(i) for i in job.items]
    claimed = RecentContextBatchJob.query.filter_by(
        id=job.id, status="pending").update(
        {"status": "collected", "collected_at": now},
        synchronize_session=False)
    db.session.commit()
    if not claimed:
        return 0
    for item in items:
        item["outcome"] = _apply_item(
            item, results.get(item["custom_id"]), batch_id)
    job.items = items
    db.session.commit()
    _report_unsaved(items)
    return 1


@celery.task
def collect_recent_context_batches():
    """Beat task: retrieve finished recent-context batches and save the
    summaries. No-op when nothing is pending."""
    with flask_app.app_context():
        return _collect_recent_context_batches()


# ── Direct path: one synchronous call; no scheduler dispatches it ──────

@celery.task
def generate_recent_context(user_id, profile_id=None, data_cutoff_iso=None):
    """Generate a recent context summary for a user with a direct
    (non-batch) call. The scheduled path is the batch check above.

    Each generation includes ALL data since the last profile update (not just
    the delta since the last recent context). This means the summary gets
    progressively more comprehensive.
    """
    with flask_app.app_context():
        _generate_recent_context_impl(user_id, profile_id, data_cutoff_iso)


def _generate_recent_context_impl(user_id, profile_id=None,
                                  data_cutoff_iso=None):
    """Body of generate_recent_context; runs inside an app context (plain
    function so tests can call it without the Celery machinery)."""
    user = User.query.get(user_id)
    if not user:
        logger.warning(f"User {user_id} not found")
        return

    data_cutoff = (
        datetime.fromisoformat(data_cutoff_iso)
        if data_cutoff_iso else None
    )

    prep = _prepare_recent_context(user, profile_id, data_cutoff)
    if prep is None:
        return
    model_id = prep["model_id"]
    pid = prep["profile_id"]
    export_result = prep["export"]
    recent_data = export_result["content"]
    source_tokens = export_result["token_count"]
    latest_ts = export_result["latest_node_created_at"]
    messages = _messages_for(prep, recent_data)

    api_keys = get_api_keys_for_usage(flask_app.config, 'chat')

    MAX_RETRIES = 2
    max_data_tokens = None
    for attempt in range(MAX_RETRIES + 1):
        if max_data_tokens is not None:
            # Retry with truncated data
            export_result = _build_recent_export(
                user, data_cutoff, max_tokens=max_data_tokens)
            if not export_result:
                logger.warning(f"User {user_id}: no data after truncation")
                return
            recent_data = export_result["content"]
            source_tokens = export_result["token_count"]
            latest_ts = export_result["latest_node_created_at"]
            messages = _messages_for(prep, recent_data)

        try:
            response = LLMProvider.get_completion(
                model_id, messages, api_keys
            )
            break
        except PromptTooLongError as e:
            if attempt == MAX_RETRIES:
                raise
            max_data_tokens = reduce_export_tokens(
                max_data_tokens, e.actual_tokens, e.max_tokens,
                export_content=recent_data,
            )
            logger.warning(
                f"Prompt too long for recent context "
                f"({e.actual_tokens} > {e.max_tokens}), "
                f"retrying with max_data_tokens={max_data_tokens}"
            )

    summary_text = response["content"]
    total_tokens = response["total_tokens"]

    # Log API cost (cache-aware, #286)
    cost_log = APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type="recent_context",
        **llm_cost_log_fields(model_id, response),
    )
    db.session.add(cost_log)

    # Cut off before any text (#368): keep the previous recent context
    # rather than replace it with an empty one, and fail the task.
    if is_empty_truncated(response):
        # The cost row (the call was billed), marked as a refusal for the
        # backoff in _should_generate_recent_context.
        cost_log.request_ref = refusal_backoff.REFUSED_REF
        db.session.commit()
        n, until, stopped = refusal_backoff.recent_context_backoff_state(
            user_id)
        if stopped:
            refusal_backoff.report_stop(
                user_id, "recent context", n, model_id, "recent_context")
        else:
            logger.warning(
                "Empty truncated recent-context output for user %s (model "
                "%s, output_tokens=%s): nothing saved, the previous one "
                "stays; next try after %s", user_id, model_id,
                response.get("output_tokens"), until)
        raise EmptyTruncatedOutputError(
            f"recent context for user {user_id}", model_id,
            response.get("output_tokens"))

    # Save the recent context
    _save_recent_context(
        user, model_id, summary_text, total_tokens, source_tokens,
        latest_ts, data_cutoff, pid)
    db.session.commit()

    logger.info(
        f"Generated recent context for user {user_id}: "
        f"{len(summary_text)} chars, {total_tokens} tokens used, "
        f"covers {source_tokens} source tokens"
    )
