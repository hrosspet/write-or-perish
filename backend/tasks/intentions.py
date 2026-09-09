"""Admin "Infer intentions": generate the intentions artifact for a
pre-filled account from its PUBLIC tweets, via the Batch API (~50%
cheaper), using the public fork of the tested intentions_detection prompt.

Mirrors the --batch mode of backend/scripts/backfill_intentions.py. A batch
can't retry-shrink mid-flight, so sizing happens BEFORE submit, against the
provider's EXACT count: Anthropic's count_tokens endpoint is free and
unbilled, so the built prompt is counted first and — when it exceeds the
context window — the export budget is shrunk by the real ratio (newest
kept, oldest cut) and rebuilt until it fits. No billed sync probe, no
"probe unexpectedly fit and got saved at full price" branch, no batch
round-trip burned on an overflow rejection. If the batch item still
overflows anyway (count drift), it is resubmitted once, calibrated from
the reported count — kept as a backstop.

Two ways to run (admin picks per click):
  * batch (default, ~50% cheaper, <=24h SLA) — sized, submitted, persisted,
    collected by the poller, as described below.
  * sync ("run now") — the same count-based sizing, then one synchronous
    completion at full price, saved before the task returns. For the case
    where the admin is mid-conversation with a fresh signup and wants the
    account ready in minutes, not hours. A pending batch for the same
    user can be CANCELLED (provider-side + job row marked "cancelled") and
    replaced by a sync run in one click.

Model is PINNED to claude-opus-4-8 (flat pricing across the 1M window; the
prompt was tested on it) — deliberately no preferred_model override, so
long-context-premium models (gpt-5.6-sol) can never be picked.

The submitted batch is PERSISTED as a ProfileBatchJob row whose item is
tagged kind="intentions", and collected by the same beat-driven
poll_profile_batches pass that drives profile chunks — so a worker restart
or deploy mid-flight loses nothing (the poller picks the batch up on the
next tick). Saving writes a NEW version of the "intentions" UserArtifact
plus an APICostLog row (request_type="intentions_infer").
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app

logger = get_task_logger(__name__)

MODEL_ID = "claude-opus-4.8"  # pinned (config key; api_model is claude-opus-4-8) — see module docstring
PROMPT_FILE = "intentions_detection_public.txt"
KIND = "intentions"
BATCH_OUTPUT_TOKENS = 8192     # ample for a ~14-item intentions list
# count -> shrink -> rebuild rounds before giving up. The shrink scales by
# the provider-reported ratio, so round 2 already lands under the limit;
# more rounds only run if the re-render measures unexpectedly hot.
MAX_SIZING_ROUNDS = 4


def _template_and_params():
    import os
    from backend.utils.placeholders import (
        USER_EXPORT_PATTERN, parse_placeholder_params, parse_max_export_tokens)
    # Relative to the backend package, not current_app.root_path — test
    # apps have a different root.
    prompts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
    with open(os.path.join(prompts_dir, PROMPT_FILE), encoding="utf-8") as f:
        template = f.read()
    m = USER_EXPORT_PATTERN.search(template)
    if not m:
        raise RuntimeError(f"{PROMPT_FILE} has no {{user_export}} placeholder")
    params = parse_placeholder_params(m.group(1) or "")
    return (template, parse_max_export_tokens(params.get("max_export_tokens")),
            params.get("keep") == "oldest")


def _build_messages(user, template, budget, chronological):
    from backend.routes.export_data import build_user_export_content
    from backend.utils.placeholders import USER_EXPORT_PATTERN
    from backend.utils.tokens import approximate_token_count
    export = build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        chronological_order=chronological, include_strategy="engaged_threads")
    if not export:
        return None
    prompt = USER_EXPORT_PATTERN.sub(lambda _m: export, template, count=1)
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    return messages, export, approximate_token_count(export)


def _save(user, content, input_tokens, output_tokens, total_tokens, batch):
    from backend.extensions import db
    from backend.models import UserArtifact, APICostLog
    from backend.utils.cost import calculate_llm_cost_microdollars
    cost = calculate_llm_cost_microdollars(
        MODEL_ID, input_tokens, output_tokens, batch=batch)
    db.session.add(APICostLog(
        user_id=user.id, model_id=MODEL_ID, request_type="intentions_infer",
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_microdollars=cost))
    artifact = UserArtifact(
        user_id=user.id, kind=KIND,
        title=UserArtifact.DEFAULT_KINDS.get(KIND, "Intentions"),
        generated_by=MODEL_ID, tokens_used=total_tokens,
        ai_usage=user.default_ai_usage)
    artifact.set_content(content)
    db.session.add(artifact)
    db.session.commit()
    version = UserArtifact.query.filter_by(user_id=user.id, kind=KIND).count()
    return {"version": version, "cost_usd": round(cost / 1e6, 4),
            "llm_tokens": total_tokens, "batch": batch}


def _submit(user, template, budget, chronological, keys, label, built=None):
    """Build at `budget` (or reuse `built` from the sizing loop), submit a
    one-user batch, and PERSIST it as a ProfileBatchJob whose item is
    tagged kind="intentions" — the beat poller (poll_profile_batches)
    collects and saves it, so the flight survives worker restarts and
    deploys."""
    from datetime import datetime
    from flask import current_app
    from backend.extensions import db
    from backend.models import ProfileBatchJob
    from backend.utils.llm_batch import batch_submit
    if built is None:
        built = _build_messages(user, template, budget, chronological)
    if built is None:
        raise RuntimeError(f"user {user.id}: no AI-readable archive")
    messages, _, est = built
    cfg = current_app.config["SUPPORTED_MODELS"][MODEL_ID]
    req = {"custom_id": f"int-u{user.id}", "model_id": MODEL_ID,
           "api_model": cfg["api_model"], "messages": messages,
           "max_tokens": BATCH_OUTPUT_TOKENS}
    batch_ids = batch_submit({cfg["provider"]: [req]}, keys, "intentions")
    if not batch_ids:
        raise RuntimeError(f"user {user.id}: batch submit failed (see logs)")
    provider_key, batch_id = next(iter(batch_ids.items()))
    item = {"custom_id": req["custom_id"], "user_id": user.id,
            "kind": "intentions", "budget": budget,
            "resubmitted": label == "overflow-recalibrated"}
    db.session.add(ProfileBatchJob(
        provider_key=provider_key, batch_id=batch_id, status="pending",
        items=[item], submitted_at=datetime.utcnow()))
    db.session.commit()
    logger.info("intentions user %s: submitted batch %s (%s export ~%s est tokens, budget=%s)",
                user.id, batch_id, label, est, budget)
    return {"provider_key": provider_key, "batch_id": batch_id, "item": item}


def _prepare(user_id):
    """Shared prelude of both run modes: eligibility guard, prompt
    template, and count-based sizing against the provider's exact token
    count (free — see module docstring). Returns everything a submit or
    a sync call needs."""
    from flask import current_app
    from backend.models import User
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.privacy import AI_ALLOWED
    from backend.llm_providers import fit_by_count

    user = User.query.get(user_id)
    if not user:
        raise RuntimeError(f"User {user_id} not found")
    if user.default_ai_usage not in AI_ALLOWED:
        raise RuntimeError(
            f"user {user_id} has default_ai_usage='{user.default_ai_usage}' "
            f"(opted out) — not sending their data to any LLM")
    template, cap, chronological = _template_and_params()
    config = current_app.config
    api_keys = get_api_keys_for_usage(config, "chat")

    from backend.tasks.recent_context import _count_total_eligible_tokens
    # The input must leave room for the response inside the context window.
    limit = (config["SUPPORTED_MODELS"][MODEL_ID].get("context_window",
                                                      1_000_000)
             - BATCH_OUTPUT_TOKENS)
    built, budget, _real = fit_by_count(
        MODEL_ID, api_keys, limit, cap,
        lambda b: _build_messages(user, template, b, chronological),
        corpus_tokens=_count_total_eligible_tokens(user.id),
        max_rounds=MAX_SIZING_ROUNDS,
        safety=config.get("RETRY_SAFETY_FACTOR", 0.99))
    if built is None:
        raise RuntimeError(f"user {user_id}: no AI-readable archive")
    return {"user": user, "template": template, "cap": cap,
            "chronological": chronological, "api_keys": api_keys,
            "built": built, "budget": budget}


def start_infer_intentions_impl(user_id):
    """Size against the provider's exact token count and submit a batch.
    Returns ("batch", ref) with the persisted job's coordinates."""
    from flask import current_app
    from backend.utils.llm_batch import apply_batch_key_override
    prep = _prepare(user_id)
    batch_keys = apply_batch_key_override(prep["api_keys"], current_app.config)
    label = "full-cap" if prep["budget"] == prep["cap"] else "count-calibrated"
    return "batch", _submit(prep["user"], prep["template"], prep["budget"],
                            prep["chronological"], batch_keys, label,
                            built=prep["built"])


# Sync mode: PromptTooLongError rebuild-and-retry rounds after the count-based
# sizing (backstop for count drift — same as backfill_intentions.py).
SYNC_MAX_RETRIES = 3


def run_infer_intentions_sync_impl(user_id, progress=None):
    """Size (same free count as batch mode), then ONE synchronous completion
    at full price, saved before returning. Returns the saved summary dict
    (version, cost_usd, llm_tokens, batch=False). `progress(stage)` is an
    optional callback for the task's PROGRESS state."""
    from backend.llm_providers import LLMProvider, PromptTooLongError
    from backend.utils.tokens import reduce_export_tokens
    prep = _prepare(user_id)
    user, template, chronological = prep["user"], prep["template"], prep["chronological"]
    built, budget = prep["built"], prep["budget"]
    response = None
    for attempt in range(SYNC_MAX_RETRIES + 1):
        if built is None:
            built = _build_messages(user, template, budget, chronological)
        if built is None:
            raise RuntimeError(f"user {user_id}: no AI-readable archive")
        messages, export, est = built
        built = None  # a retry rebuilds at the reduced budget
        if progress:
            progress(f"calling {MODEL_ID} synchronously (~{est:,} export tokens"
                     f"{', retry ' + str(attempt) if attempt else ''})")
        logger.info("intentions user %s: sync attempt %s, export ~%s est tokens, budget=%s",
                    user.id, attempt + 1, est, budget)
        try:
            response = LLMProvider.get_completion(
                MODEL_ID, messages, prep["api_keys"], max_tokens=BATCH_OUTPUT_TOKENS)
            break
        except PromptTooLongError as e:
            if attempt == SYNC_MAX_RETRIES:
                raise
            budget = reduce_export_tokens(budget, e.actual_tokens, e.max_tokens,
                                          export_content=export)
            logger.warning("intentions user %s: prompt too long (%s > %s); retry at budget=%s",
                           user.id, e.actual_tokens, e.max_tokens, budget)
    in_t = response.get("input_tokens", 0)
    out_t = response.get("output_tokens", 0)
    total = response.get("total_tokens") or (in_t + out_t)
    saved = _save(user, response["content"], in_t, out_t, total, batch=False)
    logger.info("intentions user %s: saved v%s from sync run (%s llm tokens, $%.4f)",
                user.id, saved["version"], saved["llm_tokens"], saved["cost_usd"])
    return saved


def pending_intentions_jobs(user_id):
    """Pending ProfileBatchJob rows that carry ONLY this user's intentions
    item (admin runs always submit one-user, one-item jobs, so a profile
    cohort job can never be caught by a cancel)."""
    from backend.models import ProfileBatchJob
    return [job for job in ProfileBatchJob.query.filter_by(status="pending").all()
            if job.items and all(i.get("kind") == KIND and i.get("user_id") == user_id
                                 for i in job.items)]


def _provider_cancel(provider_key, batch_id, keys):
    """Best-effort provider-side cancel. Returns an error string or None.
    Cancelling is advisory on both providers (already-started items may
    still finish and bill) — the job row is marked regardless, so the
    poller never collects it."""
    try:
        if provider_key == "anthropic":
            from anthropic import Anthropic
            Anthropic(api_key=keys.get("anthropic")).messages.batches.cancel(batch_id)
        elif provider_key.startswith("openai:"):
            from openai import OpenAI
            OpenAI(api_key=keys.get("openai")).batches.cancel(batch_id)
        else:
            return f"unknown provider {provider_key}"
        return None
    except Exception as e:  # noqa: BLE001 — cancel must never block the admin
        # SDK errors carry a one-line .message; the str() form dumps the
        # whole response body, which is noise in the admin row.
        body = getattr(e, "body", None)
        msg = (body.get("error", {}).get("message") if isinstance(body, dict) else None) \
            or getattr(e, "message", None) or str(e)
        return f"{type(e).__name__}: {msg}"[:160]


def cancel_pending_intentions(user_id):
    """Cancel every in-flight intentions batch for the user: provider-side
    (best-effort), then the job row is marked "cancelled" so the poller
    skips it — no collect, no overflow-resubmit. Returns
    {"cancelled": n, "errors": [str]}."""
    from datetime import datetime
    from flask import current_app
    from backend.extensions import db
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.llm_batch import apply_batch_key_override
    jobs = pending_intentions_jobs(user_id)
    if not jobs:
        return {"cancelled": 0, "errors": []}
    config = current_app.config
    keys = apply_batch_key_override(get_api_keys_for_usage(config, "chat"), config)
    errors = []
    for job in jobs:
        err = _provider_cancel(job.provider_key, job.batch_id, keys)
        if err:
            errors.append(f"{job.batch_id}: {err}")
            logger.warning("intentions user %s: provider cancel of %s failed (%s) — "
                           "marking the job cancelled anyway", user_id, job.batch_id, err)
        job.status = "cancelled"
        job.collected_at = datetime.utcnow()
        job.items = [{**i, "cancelled": True} for i in job.items]
        logger.info("intentions user %s: cancelled batch %s", user_id, job.batch_id)
    db.session.commit()
    return {"cancelled": len(jobs), "errors": errors}


def apply_intentions_item(user, item, result):
    """Called by poll_profile_batches for a collected kind="intentions"
    item. Saves the artifact at batch price. Idempotent enough for poll
    overlap: a second apply would add a version, so the poller marks the
    job collected in the same pass (like profile items)."""
    saved = _save(user, result["content"],
                  result.get("input_tokens", 0),
                  result.get("output_tokens", 0),
                  result.get("input_tokens", 0) + result.get("output_tokens", 0),
                  batch=True)
    logger.info("intentions user %s: saved v%s from batch (%s llm tokens, $%.4f)",
                user.id, saved["version"], saved["llm_tokens"], saved["cost_usd"])
    return saved


def _calibrated_budget(user, prior_budget, actual_tokens, max_tokens):
    """Budget (in the export builder's DB chars/4 units) sized so the real
    prompt fits. The binding quantity is min(prior budget, the corpus's DB
    token sum) — scaling the raw budget is a no-op whenever the corpus is
    smaller than it (user 110, 2026-09-01: 1M -> 661k budget re-rendered
    the identical full export and overflowed again). Scale what was
    actually used by the provider-reported ratio."""
    from flask import current_app
    from backend.tasks.recent_context import _count_total_eligible_tokens
    safety = current_app.config.get("RETRY_SAFETY_FACTOR", 0.99)
    effective = min(prior_budget or max_tokens,
                    max(_count_total_eligible_tokens(user.id), 1))
    return max(int(effective * max_tokens / actual_tokens * safety), 10_000)


def _failed_item_tokens(provider_key, batch_id, custom_id, keys):
    """Best-effort: read the failed batch item's error and pull the real
    token counts out of it (Anthropic reports "N tokens > M maximum").
    Returns (actual, maximum) or (None, None). Never raises."""
    import re
    try:
        if provider_key == "anthropic":
            from anthropic import Anthropic
            client = Anthropic(api_key=keys.get("anthropic"))
            for entry in client.messages.batches.results(batch_id):
                if entry.custom_id != custom_id:
                    continue
                if entry.result.type == "succeeded":
                    return None, None
                err = getattr(entry.result, "error", None)
                msg = str(err) if err is not None else str(entry.result.type)
                mt = re.search(r"(\d+) tokens > (\d+) maximum", msg)
                if mt:
                    return int(mt.group(1)), int(mt.group(2))
    except Exception:  # noqa: BLE001 — calibration helper must never raise
        pass
    return None, None


def handle_failed_intentions_item(user, item, job, keys):
    """Called by the poller when a kind="intentions" item ended without a
    result. One calibrated resubmit — a new persisted job — sized from the
    provider's real token count when the error carries it (Anthropic does),
    else a 70% shrink. A resubmitted item that fails again gives up."""
    if item.get("resubmitted"):
        # Persist the terminal failure so the admin column can show it
        # (otherwise the abandoned run is invisible — the job is marked
        # collected and no artifact ever appears).
        from backend.extensions import db
        job.items = [{**i, "gave_up": True} if i.get("custom_id") == item.get("custom_id")
                     else i for i in job.items]
        db.session.commit()
        logger.warning("intentions user %s: batch failed again after resubmit — giving up", user.id)
        return
    template, cap, chronological = _template_and_params()
    prior = item.get("budget") or cap
    actual, maximum = _failed_item_tokens(
        job.provider_key, job.batch_id, item["custom_id"], keys)
    if actual and maximum:
        calibrated = _calibrated_budget(user, prior, actual, maximum)
        label = f"real={actual} > max={maximum}"
    else:
        from backend.tasks.recent_context import _count_total_eligible_tokens
        effective = min(prior, max(_count_total_eligible_tokens(user.id), 1))
        calibrated = max(int(effective * 0.7), 10_000)
        label = "no token count in error — 70% shrink"
    logger.warning("intentions user %s: batch item failed (%s) — resubmitting once at budget=%s",
                   user.id, label, calibrated)
    try:
        _submit(user, template, calibrated, chronological, keys,
                "overflow-recalibrated")
    except Exception:
        logger.exception("intentions user %s: resubmit failed", user.id)


@celery.task(bind=True, name="backend.tasks.intentions.infer_intentions")
def infer_intentions(self, user_id, mode="batch"):
    """mode="batch": sizes (via free exact count) and submits, then ends —
    collection is the beat poller's job, so nothing is lost to worker
    restarts; the admin row's persistent state lives in the Users-tab
    Profile column. mode="sync": sizes, calls the model synchronously,
    saves, and returns the saved summary (the admin row shows it)."""
    with flask_app.app_context():
        try:
            if mode == "sync":
                def progress(stage):
                    self.update_state(state="PROGRESS", meta={
                        "user_id": user_id, "stage": stage,
                        "done": None, "total": None})
                progress("sizing the prompt")
                saved = run_infer_intentions_sync_impl(user_id, progress=progress)
                return {"user_id": user_id, "stage": "saved", "mode": "sync",
                        "model_id": MODEL_ID, "total": None, **saved}
            self.update_state(state="PROGRESS", meta={
                "user_id": user_id, "stage": "sizing + submitting batch",
                "done": None, "total": None})
            _kind, payload = start_infer_intentions_impl(user_id)
        except Exception:
            logger.exception("Infer intentions (%s) failed for user %s", mode, user_id)
            raise
        return {"user_id": user_id, "stage": "batch submitted", "mode": "batch",
                "batch_id": payload["batch_id"], "total": None}
