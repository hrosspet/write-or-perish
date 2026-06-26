#!/usr/bin/env python3
"""
One-time backfill: generate each user's Intentions artifact from their archive,
using the `intentions_detection.txt` prompt.

Mirrors profile generation (backend/tasks/exports.py:generate_user_profile):
it builds the {user_export} via the same engaged-threads topology, runs the
prompt against an LLM, and saves the result as a NEW version of the user's
`intentions` UserArtifact — with the same metadata profile generation records:
  * model        -> UserArtifact.generated_by
  * LLM tokens   -> UserArtifact.tokens_used (input + output)
  * date         -> UserArtifact.created_at
plus an APICostLog row (request_type="intentions_backfill").

Two modes:
  * sync (default) — one synchronous completion per user, with the same
    prompt-too-long retry/shrink loop as profile generation. Our token
    estimate (chars/4) runs well under reality, so the first attempt usually
    overflows; the loop reads the provider's real token count from the
    rejection and shrinks proportionally until it fits.
  * --batch — one Batch API request PER USER (~50% cheaper, async, <=24h SLA).
    A batch request can't retry-shrink mid-flight, and our chars/4 estimate
    runs far under reality, so sizing is driven by a cheap DB token estimate
    (sum of the user's AI-readable node token_counts — the heartbeat estimate):
      - estimate <= --probe-threshold (default 600k): likely fits the context,
        so submit the batch directly at the full cap. If one overflows it's
        reported (our DB estimate + the real count from the failure) for a
        sync re-run — never silently lost.
      - estimate > threshold: send ONE sync "calibration probe" at the full
        budget first. It overflows, the rejected 400 is NOT billed and carries
        the REAL token count for free, which sizes the batch export to fit.
    A probe that unexpectedly fits is saved synchronously (full price).

Non-destructive: each run creates a new artifact version; prior content stays
in the artifact's history.

Recovery: in --batch mode each retrieved result is saved immediately, so a
crash/timeout only leaves the not-yet-retrieved batches unsaved. Their ids
(with user ids) are printed up front and the provider keeps the results
(Anthropic ~29 days), so `--collect <batch_id ...>` fetches and saves them
later.

Privacy: any selected user who has globally opted out of AI usage
(default_ai_usage='none') is skipped entirely — their data is never sent to an
LLM (the gate runs before any export is built, in both modes).

Model per user: their `preferred_model` if set and supported, else Opus 4.8
(override for all with --model). The {user_export?...} params (keep,
max_export_tokens) are read straight from the prompt, so it stays the single
source of truth.

Usage:
    python backend/scripts/backfill_intentions.py                  # just user 1 (operator), sync
    python backend/scripts/backfill_intentions.py 46 27 1 33 50 31 42 45
    python backend/scripts/backfill_intentions.py --batch 46 27 1 33 50 31 42 45   # ~50% cheaper
    python backend/scripts/backfill_intentions.py --model claude-opus-4.8 1 27
    python backend/scripts/backfill_intentions.py --dry-run 1       # build export, skip LLM + save
    python backend/scripts/backfill_intentions.py --collect msgbatch_a msgbatch_b   # recover a crashed/timed-out --batch run
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.app import create_app  # noqa: E402
from backend.extensions import db  # noqa: E402
from backend.models import User, UserArtifact, APICostLog  # noqa: E402
from backend.routes.export_data import build_user_export_content  # noqa: E402
from backend.utils.api_keys import get_api_keys_for_usage  # noqa: E402
from backend.utils.cost import calculate_llm_cost_microdollars  # noqa: E402
from backend.utils.privacy import AI_ALLOWED  # noqa: E402
from backend.utils.placeholders import (  # noqa: E402
    USER_EXPORT_PATTERN, parse_placeholder_params, parse_max_export_tokens,
)
from backend.utils.tokens import (  # noqa: E402
    approximate_token_count, reduce_export_tokens,
)
from backend.utils.llm_batch import (  # noqa: E402
    batch_submit, batch_check_and_collect, apply_batch_key_override,
)
from backend.llm_providers import LLMProvider, PromptTooLongError  # noqa: E402
# Cheap DB token estimate (sum of the user's AI-readable node token_counts —
# no decryption, no export build), the same one the heartbeat/trigger checks
# use to gate work.
from backend.tasks.recent_context import (  # noqa: E402
    _count_total_eligible_tokens,
)

DEFAULT_MODEL = "claude-opus-4.8"
PROMPT_FILE = "intentions_detection.txt"
KIND = "intentions"
MAX_RETRIES = 3
BATCH_OUTPUT_TOKENS = 8192  # output cap — ample for a ~14-item intentions list
# Extra margin applied to the probe-calibrated batch budget: the batch can't
# retry on overflow, so leave headroom for any non-linearity between the
# calibration budget and the rebuilt export's real token count.
BATCH_SAFETY = 0.92
# In --batch mode, only spend a sync calibration probe on users whose cheap DB
# token estimate exceeds this; smaller users are likely to fit the context at
# the full cap, so we submit their batch directly and report if one overflows.
PROBE_THRESHOLD_TOKENS = 600_000


def _resolve_model(app, user, override):
    if override:
        return override
    pref = user.preferred_model
    if pref and pref in app.config["SUPPORTED_MODELS"]:
        return pref
    return DEFAULT_MODEL


def _load_template(app):
    path = os.path.join(app.root_path, "prompts", PROMPT_FILE)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _eligible(app, user, model_id):
    """Privacy + model gate shared by both modes. Prints the skip reason and
    returns False when the user must not be processed. The privacy gate runs
    before any export is built, so an opted-out user's data is never read."""
    if user.default_ai_usage not in AI_ALLOWED:
        print(f"  - user {user.id}: default_ai_usage='{user.default_ai_usage}' "
              f"(opted out) — skipping, not sending data to any LLM")
        return False
    if model_id not in app.config["SUPPORTED_MODELS"]:
        print(f"  ! user {user.id}: unsupported model '{model_id}', skipping")
        return False
    return True


def _prompt_export_params(template):
    """Read keep / max_export_tokens straight from the prompt's {user_export}."""
    m = USER_EXPORT_PATTERN.search(template)
    if not m:
        raise ValueError("prompt has no {user_export} placeholder")
    params = parse_placeholder_params(m.group(1) or "")
    return (parse_max_export_tokens(params.get("max_export_tokens")),
            params.get("keep") == "oldest")


def _build_messages(user, template, budget, chronological):
    """Build the export (engaged-threads, chat/train) at `budget` and
    substitute it into the prompt. Returns (messages, export, est_tokens) or
    None when the user has no AI-readable archive."""
    export = build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        chronological_order=chronological, include_strategy="engaged_threads",
    )
    if not export:
        return None
    # Lambda repl avoids re backreference processing of the export text.
    final_prompt = USER_EXPORT_PATTERN.sub(lambda _m: export, template, count=1)
    messages = [{"role": "user",
                 "content": [{"type": "text", "text": final_prompt}]}]
    return messages, export, approximate_token_count(export)


def _save_intentions(user, model_id, content, input_tokens, output_tokens,
                     total_tokens, batch):
    """Save a new intentions artifact version + APICostLog, mirroring the
    metadata profile generation records. Returns (version, cost_microdollars,
    total_tokens). ai_usage follows the user's global default (like the
    profile); privacy stays "private" (the UserArtifact default)."""
    cost = calculate_llm_cost_microdollars(
        model_id, input_tokens, output_tokens, batch=batch)
    db.session.add(APICostLog(
        user_id=user.id, model_id=model_id, request_type="intentions_backfill",
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_microdollars=cost,
    ))
    artifact = UserArtifact(
        user_id=user.id, kind=KIND,
        title=UserArtifact.DEFAULT_KINDS.get(KIND, "Intentions"),
        generated_by=model_id, tokens_used=total_tokens,
        ai_usage=user.default_ai_usage,
    )
    artifact.set_content(content)
    db.session.add(artifact)
    db.session.commit()
    version = UserArtifact.query.filter_by(user_id=user.id, kind=KIND).count()
    return version, cost, total_tokens


# ── Synchronous path (default) ─────────────────────────────────────────────

def backfill_user(app, user, template, model_id, dry_run=False):
    if not _eligible(app, user, model_id):
        return
    max_export_tokens, chronological = _prompt_export_params(template)
    api_keys = get_api_keys_for_usage(app.config, "chat")

    response = None
    for attempt in range(MAX_RETRIES + 1):
        built = _build_messages(user, template, max_export_tokens, chronological)
        if built is None:
            print(f"  ! user {user.id}: no AI-readable archive, skipping")
            return
        messages, export, export_tokens = built
        print(f"  user {user.id}: model={model_id}, export ~{export_tokens} "
              f"tokens (attempt {attempt + 1}, budget={max_export_tokens})")
        if dry_run:
            print("    [dry-run] skipping LLM call + save")
            return
        try:
            response = LLMProvider.get_completion(model_id, messages, api_keys)
            break
        except PromptTooLongError as e:
            if attempt == MAX_RETRIES:
                raise
            max_export_tokens = reduce_export_tokens(
                max_export_tokens, e.actual_tokens, e.max_tokens,
                export_content=export,
            )
            print(f"    prompt too long ({e.actual_tokens} > {e.max_tokens}); "
                  f"retry with budget={max_export_tokens}")

    version, cost, total = _save_intentions(
        user, model_id, response["content"],
        response.get("input_tokens", 0), response.get("output_tokens", 0),
        response["total_tokens"], batch=False,
    )
    print(f"  ✓ user {user.id}: saved intentions v{version} "
          f"({len(response['content'])} chars, model={model_id}, "
          f"llm_tokens={total}, cost=${cost / 1e6:.4f})")


# ── Batch path (--batch, ~50% cheaper) ─────────────────────────────────────

def _failed_batch_tokens(provider_key, batch_id, custom_id, keys):
    """Best-effort: fetch a failed batch item's error and pull the real token
    count out of it. Returns (actual_tokens_or_None, error_summary_or_None).
    Anthropic only — OpenAI batch errors are reported without a parsed count.
    Must never raise (it's only used to enrich a failure report)."""
    try:
        if provider_key == "anthropic":
            from anthropic import Anthropic
            client = Anthropic(api_key=keys["anthropic"])
            for entry in client.messages.batches.results(batch_id):
                if entry.custom_id != custom_id:
                    continue
                if entry.result.type == "succeeded":
                    return None, None
                err = getattr(entry.result, "error", None)
                msg = str(err) if err is not None else str(entry.result.type)
                mt = re.search(r"(\d+) tokens > (\d+) maximum", msg)
                if mt:
                    return int(mt.group(1)), msg
                return None, msg
    except Exception as e:  # noqa: BLE001 — reporting helper must never raise
        return None, f"(could not read error: {type(e).__name__})"
    return None, None


def _submit_batch_for(app, user, template, model_id, budget, chronological,
                      batch_keys, db_tokens, pending, est_label):
    """Build the export at `budget`, submit a one-user batch, and record it in
    `pending` (with db_tokens, for failure reporting)."""
    built = _build_messages(user, template, budget, chronological)
    if built is None:
        print(f"  ! user {user.id}: no AI-readable archive, skipping")
        return
    messages, _, est = built
    cfg = app.config["SUPPORTED_MODELS"][model_id]
    req = {"custom_id": f"int-u{user.id}", "model_id": model_id,
           "api_model": cfg["api_model"], "messages": messages,
           "max_tokens": BATCH_OUTPUT_TOKENS}
    batch_ids = batch_submit({cfg["provider"]: [req]}, batch_keys, "intentions")
    if not batch_ids:
        print(f"  ✗ user {user.id}: batch submit failed (see logs)")
        return
    provider_key, batch_id = next(iter(batch_ids.items()))
    pending[user.id] = {
        "user": user, "model_id": model_id, "custom_id": req["custom_id"],
        "provider_key": provider_key, "batch_id": batch_id,
        "db_tokens": db_tokens,
    }
    print(f"  user {user.id}: submitted batch {batch_id} ({provider_key}, "
          f"model={model_id}, {est_label} export ~{est} tokens, "
          f"db_est={db_tokens})")


def _dispatch_batch_user(app, user, template, model_id, probe_budget,
                         chronological, probe_threshold, api_keys, batch_keys,
                         dry_run, pending):
    """Per-user batch dispatch. A cheap DB token estimate decides the path:
    small archives (<= threshold) go straight to a full-cap batch — likely to
    fit the context; large ones first spend a free sync calibration probe (its
    rejected 400 carries the real token count) so the batch is sized to fit.
    A probe that unexpectedly fits is saved synchronously (full price)."""
    db_tokens = _count_total_eligible_tokens(user.id)

    if db_tokens <= probe_threshold:
        # Likely fits the context at the full cap — skip the probe.
        if dry_run:
            print(f"  user {user.id}: db_est={db_tokens} <= {probe_threshold} "
                  f"— [dry-run] would batch directly at the full cap")
            return
        _submit_batch_for(app, user, template, model_id, probe_budget,
                          chronological, batch_keys, db_tokens, pending,
                          est_label="full-cap")
        return

    # Large archive — calibration probe first.
    built = _build_messages(user, template, probe_budget, chronological)
    if built is None:
        print(f"  ! user {user.id}: no AI-readable archive, skipping")
        return
    messages, export, est = built
    if dry_run:
        print(f"  user {user.id}: db_est={db_tokens} > {probe_threshold} — "
              f"[dry-run] would probe (export ~{est}) then batch")
        return
    try:
        result = LLMProvider.get_completion(model_id, messages, api_keys)
    except PromptTooLongError as e:
        # Free 400 — calibrate the batch export to the real token count.
        calibrated = reduce_export_tokens(
            probe_budget, e.actual_tokens, e.max_tokens, export_content=export)
        batch_budget = max(1, int(calibrated * BATCH_SAFETY))
        print(f"  user {user.id}: probe real={e.actual_tokens} > {e.max_tokens} "
              f"max (db_est={db_tokens}) — batch budget={batch_budget}")
        _submit_batch_for(app, user, template, model_id, batch_budget,
                          chronological, batch_keys, db_tokens, pending,
                          est_label="calibrated")
        return

    # Probe fit despite the large estimate — already have the answer.
    in_t, out_t = result.get("input_tokens", 0), result.get("output_tokens", 0)
    version, cost, total = _save_intentions(
        user, model_id, result["content"], in_t, out_t,
        result["total_tokens"], batch=False)
    print(f"  ✓ user {user.id}: fit in one sync call, saved intentions v{version} "
          f"({len(result['content'])} chars, llm_tokens={total}, "
          f"cost=${cost / 1e6:.4f}, full price)")


def run_batch(app, users, template, model_override, dry_run,
              poll_interval, max_wait, probe_threshold):
    api_keys = get_api_keys_for_usage(app.config, "chat")
    batch_keys = apply_batch_key_override(api_keys, app.config)
    probe_budget, chronological = _prompt_export_params(template)

    pending = {}  # user_id -> {user, model_id, custom_id, provider_key, batch_id, db_tokens}
    for user in users:
        model_id = _resolve_model(app, user, model_override)
        if not _eligible(app, user, model_id):
            continue
        try:
            _dispatch_batch_user(app, user, template, model_id, probe_budget,
                                 chronological, probe_threshold, api_keys,
                                 batch_keys, dry_run, pending)
        except Exception as e:  # noqa: BLE001 — isolate per-user failures
            db.session.rollback()
            print(f"  ✗ user {user.id}: FAILED — {type(e).__name__}: {e}")

    if dry_run:
        return
    if not pending:
        print("No batches pending (all selected users fit synchronously, were "
              "skipped, or failed).")
        return

    # Print the full batch-id list up front (before any polling) so it's never
    # lost — even if the script is killed mid-poll, the batches still complete
    # provider-side and can be collected later from these ids.
    print(f"Submitted {len(pending)} batch(es):")
    for uid, info in pending.items():
        print(f"    user {uid}: {info['batch_id']} ({info['provider_key']})")
    print(f"Polling every {poll_interval}s (up to {max_wait // 60} min)...")
    waited = 0
    while pending and waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        for uid in list(pending):
            info = pending[uid]
            try:
                results, still, _ = batch_check_and_collect(
                    {info["provider_key"]: info["batch_id"]}, batch_keys)
            except Exception as e:  # noqa: BLE001
                print(f"  ! user {uid}: poll error ({type(e).__name__}: {e})")
                continue
            r = results.get(info["custom_id"])
            if r:
                try:
                    in_t, out_t = r["input_tokens"], r["output_tokens"]
                    version, cost, total = _save_intentions(
                        info["user"], info["model_id"], r["content"],
                        in_t, out_t, in_t + out_t, batch=True)
                    print(f"  ✓ user {uid}: saved intentions v{version} "
                          f"({len(r['content'])} chars, model={info['model_id']}, "
                          f"llm_tokens={total}, cost=${cost / 1e6:.4f}, batch 50%)")
                except Exception as e:  # noqa: BLE001
                    db.session.rollback()
                    print(f"  ✗ user {uid}: save FAILED — {type(e).__name__}: {e}")
                pending.pop(uid)
            elif not still:
                # Batch ended but this request produced no result — most likely
                # an overflow on a small (no-probe) user. Report our cheap DB
                # estimate plus the real count from the failure, if available.
                actual, errmsg = _failed_batch_tokens(
                    info["provider_key"], info["batch_id"],
                    info["custom_id"], batch_keys)
                real = f"real={actual}" if actual else "real=unavailable"
                tail = f" [{errmsg}]" if errmsg else ""
                print(f"  ✗ user {uid}: batch {info['batch_id']} produced no "
                      f"result (db_est={info['db_tokens']}, {real}) — "
                      f"re-run sync for this user{tail}")
                pending.pop(uid)
        if pending:
            print(f"  ... {len(pending)} still processing ({waited // 60}m elapsed)")

    if pending:
        print(f"Timed out after {max_wait // 60} min; {len(pending)} batch(es) "
              f"still processing (<=24h SLA — they'll finish). Batch IDs:")
        for uid, info in pending.items():
            print(f"  user {uid}: {info['batch_id']} ({info['provider_key']})")
        print("Raise --max-wait, or collect them later with --collect <ids>.")


# ── Collect (recover a crashed/timed-out --batch run) ──────────────────────

def _provider_key_for_batch_id(batch_id):
    """Infer the batch_check_and_collect provider key from a batch id's prefix
    (Anthropic ids start with 'msgbatch_', OpenAI with 'batch_')."""
    if batch_id.startswith("msgbatch_"):
        return "anthropic"
    if batch_id.startswith("batch_"):
        return "openai:collect"  # only the 'openai:' prefix matters to the helper
    return None


def collect_batches(app, batch_ids, model_override):
    """Fetch already-submitted batches by id and save their results — recovers
    a --batch run that crashed or timed out before retrieving them. The user
    is read from each result's custom_id ('int-u<id>'); the model is re-resolved
    per user (pass --model to force one). Batches already saved before the
    crash are independent — re-collecting one just writes another version."""
    keys = apply_batch_key_override(
        get_api_keys_for_usage(app.config, "chat"), app.config)
    for batch_id in batch_ids:
        provider_key = _provider_key_for_batch_id(batch_id)
        if provider_key is None:
            print(f"  ! batch {batch_id}: unrecognized id format, skipping")
            continue
        try:
            results, still, _ = batch_check_and_collect(
                {provider_key: batch_id}, keys)
        except Exception as e:  # noqa: BLE001
            print(f"  ! batch {batch_id}: fetch error ({type(e).__name__}: {e})")
            continue
        if still:
            print(f"  - batch {batch_id}: still processing — try again later")
            continue
        if not results:
            print(f"  ✗ batch {batch_id}: ended with no usable results "
                  f"(every item errored?)")
            continue
        for cid, r in results.items():
            m = re.match(r"int-u(\d+)$", cid)
            if not m:
                print(f"  ! batch {batch_id}: unrecognized custom_id '{cid}', "
                      f"skipping")
                continue
            uid = int(m.group(1))
            user = User.query.get(uid)
            if not user:
                print(f"  ! batch {batch_id}: user {uid} not found, skipping")
                continue
            model_id = _resolve_model(app, user, model_override)
            try:
                in_t, out_t = r["input_tokens"], r["output_tokens"]
                version, cost, total = _save_intentions(
                    user, model_id, r["content"], in_t, out_t,
                    in_t + out_t, batch=True)
                print(f"  ✓ batch {batch_id} user {uid}: saved intentions "
                      f"v{version} ({len(r['content'])} chars, model={model_id}, "
                      f"llm_tokens={total}, cost=${cost / 1e6:.4f}, batch 50%)")
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                print(f"  ✗ batch {batch_id} user {uid}: save FAILED — "
                      f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser(
        description="One-time backfill of users' Intentions artifacts.")
    ap.add_argument("user_ids", nargs="*", type=int,
                    help="User IDs to backfill (default: 1, the operator).")
    ap.add_argument("--model", default=None,
                    help="Force this model for all users (default: each user's "
                         f"preferred_model, else {DEFAULT_MODEL}).")
    ap.add_argument("--batch", action="store_true",
                    help="Submit via the Batch API (~50%% cheaper, async): one "
                         "right-sized batch per user, after a free sync probe.")
    ap.add_argument("--poll-interval", type=int, default=30,
                    help="Seconds between batch status polls (--batch).")
    ap.add_argument("--max-wait", type=int, default=3600,
                    help="Max seconds to poll before printing batch IDs and "
                         "exiting (--batch).")
    ap.add_argument("--probe-threshold", type=int,
                    default=PROBE_THRESHOLD_TOKENS,
                    help="(--batch) Only spend a sync calibration probe on "
                         "users whose cheap DB token estimate exceeds this; "
                         "smaller users batch directly at the full cap "
                         f"(default {PROBE_THRESHOLD_TOKENS}).")
    ap.add_argument("--collect", nargs="+", metavar="BATCH_ID", default=None,
                    help="Recover a crashed/timed-out --batch run: fetch these "
                         "batch ids and save their results (user read from each "
                         "result's custom_id). Ignores user_ids/other flags "
                         "except --model.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the export and stop before the LLM call + save.")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        if args.collect:
            print(f"Collecting {len(args.collect)} batch(es)...")
            collect_batches(app, args.collect, args.model)
            return

        user_ids = args.user_ids or [1]
        template = _load_template(app)
        mode = "batch (~50%)" if args.batch else "sync"
        print(f"Backfilling intentions ({mode}) for users: {user_ids}"
              + (" [DRY RUN]" if args.dry_run else ""))
        users = []
        for uid in user_ids:
            user = User.query.get(uid)
            if not user:
                print(f"  ! user {uid} not found, skipping")
                continue
            users.append(user)

        if args.batch:
            run_batch(app, users, template, args.model, args.dry_run,
                      args.poll_interval, args.max_wait, args.probe_threshold)
        else:
            for user in users:
                model_id = _resolve_model(app, user, args.model)
                try:
                    backfill_user(app, user, template, model_id,
                                  dry_run=args.dry_run)
                except Exception as e:  # noqa: BLE001 — isolate per-user failures
                    db.session.rollback()
                    print(f"  ✗ user {user.id}: FAILED — {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
