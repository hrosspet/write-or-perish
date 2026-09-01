"""Admin "Infer intentions": generate the intentions artifact for a
pre-filled account from its PUBLIC tweets, via the Batch API (~50%
cheaper), using the public fork of the tested intentions_detection prompt.

Mirrors the --batch mode of backend/scripts/backfill_intentions.py. A batch
can't retry-shrink mid-flight, so sizing happens BEFORE submit:
  * cheap DB token estimate <= PROBE_THRESHOLD: the corpus comfortably fits
    the 1M context at the prompt's own budget — submit the batch directly.
  * estimate > threshold: one sync calibration probe at full budget; its
    "prompt too long" rejection is unbilled and carries the provider's real
    token count, which sizes the batch export (newest kept, oldest cut) to
    fit. A probe that unexpectedly fits is saved synchronously (full price).
If the batch item itself still overflows, it is resubmitted once, calibrated
from the reported count.

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
PROBE_THRESHOLD_TOKENS = 560_000


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


def _submit(user, template, budget, chronological, keys, label):
    """Build at `budget`, submit a one-user batch, and PERSIST it as a
    ProfileBatchJob whose item is tagged kind="intentions" — the beat
    poller (poll_profile_batches) collects and saves it, so the flight
    survives worker restarts and deploys."""
    from datetime import datetime
    from flask import current_app
    from backend.extensions import db
    from backend.models import ProfileBatchJob
    from backend.utils.llm_batch import batch_submit
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


def start_infer_intentions_impl(user_id):
    """Size (maybe probe) and submit. Returns ("done", result) when the
    probe unexpectedly fit (saved synchronously), else ("batch", ref) with
    the persisted job's coordinates."""
    from flask import current_app
    from backend.models import User
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.llm_batch import apply_batch_key_override
    from backend.utils.privacy import AI_ALLOWED
    from backend.llm_providers import LLMProvider, PromptTooLongError
    from backend.tasks.recent_context import _count_total_eligible_tokens

    user = User.query.get(user_id)
    if not user:
        raise RuntimeError(f"User {user_id} not found")
    if user.default_ai_usage not in AI_ALLOWED:
        raise RuntimeError(
            f"user {user_id} has default_ai_usage='{user.default_ai_usage}' "
            f"(opted out) — not sending their data to any LLM")
    template, budget, chronological = _template_and_params()
    config = current_app.config
    batch_keys = apply_batch_key_override(
        get_api_keys_for_usage(config, "chat"), config)
    # Stored token_counts are chars/4-ish; a model whose tokenizer runs
    # hotter declares token_multiplier in SUPPORTED_MODELS (e.g. Sol 2.0
    # / measured 3.2x on tweets). Opus 4.8 declares none -> no scaling.
    # Mis-sizing is self-healing anyway: an overflowing batch item is
    # unbilled and resubmitted once, calibrated from the real count.
    multiplier = config["SUPPORTED_MODELS"][MODEL_ID].get("token_multiplier", 1.0)
    db_tokens = int(_count_total_eligible_tokens(user.id) * multiplier)

    if db_tokens <= PROBE_THRESHOLD_TOKENS:
        return "batch", _submit(user, template, budget, chronological,
                                batch_keys, "full-cap")

    # Large corpus — free sync calibration probe (rejected 400 isn't billed).
    api_keys = get_api_keys_for_usage(config, "chat")
    built = _build_messages(user, template, budget, chronological)
    if built is None:
        raise RuntimeError(f"user {user_id}: no AI-readable archive")
    messages, export, _ = built
    try:
        result = LLMProvider.get_completion(MODEL_ID, messages, api_keys)
    except PromptTooLongError as e:
        calibrated = _calibrated_budget(user, budget, e.actual_tokens, e.max_tokens)
        logger.info("intentions user %s: probe real=%s > max=%s — batch budget=%s",
                    user_id, e.actual_tokens, e.max_tokens, calibrated)
        return "batch", _submit(user, template, calibrated, chronological,
                                batch_keys, "calibrated")
    # Probe fit — we already paid full price for the answer; save it.
    return "done", _save(user, result["content"],
                         result.get("input_tokens", 0),
                         result.get("output_tokens", 0),
                         result["total_tokens"], batch=False)


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
def infer_intentions(self, user_id):
    """Sizes (maybe probes) and submits, then ends — collection is the
    beat poller's job, so nothing is lost to worker restarts. The admin
    row's persistent state lives in the Users-tab Profile column."""
    with flask_app.app_context():
        try:
            self.update_state(state="PROGRESS", meta={
                "user_id": user_id, "stage": "sizing + submitting batch",
                "done": None, "total": None})
            kind, payload = start_infer_intentions_impl(user_id)
        except Exception:
            logger.exception("Infer intentions failed for user %s", user_id)
            raise
        if kind == "done":
            return {"user_id": user_id, "stage": "done", "model_id": MODEL_ID,
                    "total": None, **payload}
        return {"user_id": user_id, "stage": "batch submitted",
                "batch_id": payload["batch_id"], "total": None}
