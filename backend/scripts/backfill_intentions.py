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
    prompt-too-long retry/shrink loop as profile generation.
  * --batch        — one Batch API request PER USER (~50% cheaper, async,
    ≤24h SLA). Each user is still its own request (own model + export +
    result); they're just submitted via the batch endpoint for the discount.
    Because a batch request can't retry-shrink mid-flight, each export is
    pre-sized to fit the model's context window. The script submits one batch
    per user, then polls them all to completion and saves each result.

Non-destructive: each run creates a new artifact version; prior content stays
in the artifact's history.

Privacy: any selected user who has globally opted out of AI usage
(default_ai_usage='none') is skipped entirely — their data is never sent to an
LLM (gate runs before any export is built, in both modes).

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
"""
import argparse
import os
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

DEFAULT_MODEL = "claude-opus-4.8"
PROMPT_FILE = "intentions_detection.txt"
KIND = "intentions"
MAX_RETRIES = 3
BATCH_OUTPUT_TOKENS = 8192  # output cap — ample for a ~14-item intentions list


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


def _substitute_export(template, export):
    # Lambda repl avoids re backreference processing of the export text.
    return USER_EXPORT_PATTERN.sub(lambda _m: export, template, count=1)


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
        # Same scope as the {user_export} placeholder: engaged-threads
        # topology, AI-readable nodes only (ai_usage in {chat, train}).
        export = build_user_export_content(
            user, max_tokens=max_export_tokens, filter_ai_usage=True,
            chronological_order=chronological, include_strategy="engaged_threads",
        )
        if not export:
            print(f"  ! user {user.id}: no AI-readable archive, skipping")
            return
        export_tokens = approximate_token_count(export)
        final_prompt = _substitute_export(template, export)
        print(f"  user {user.id}: model={model_id}, export ~{export_tokens} "
              f"tokens (attempt {attempt + 1}, budget={max_export_tokens})")
        if dry_run:
            print("    [dry-run] skipping LLM call + save")
            return
        messages = [{"role": "user",
                     "content": [{"type": "text", "text": final_prompt}]}]
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

def _build_batch_request(app, user, template, model_id):
    """Gate + pre-size the export to fit the model context (a batch request
    can't retry-shrink mid-flight) + build the per-user request dict.
    Returns (request, provider, export_token_estimate) or None."""
    if not _eligible(app, user, model_id):
        return None
    cfg = app.config["SUPPORTED_MODELS"][model_id]
    prompt_max, chronological = _prompt_export_params(template)
    # Leave headroom for the prompt + output, with a margin for token-estimate
    # error since there's no on-overflow shrink in batch mode.
    ctx = cfg.get("context_window", 200000)
    overhead = approximate_token_count(template) + BATCH_OUTPUT_TOKENS
    budget = int((ctx - overhead) * 0.95)
    if prompt_max:
        budget = min(prompt_max, budget)
    export = build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        chronological_order=chronological, include_strategy="engaged_threads",
    )
    if not export:
        print(f"  ! user {user.id}: no AI-readable archive, skipping")
        return None
    final_prompt = _substitute_export(template, export)
    req = {
        "custom_id": f"int-u{user.id}",
        "model_id": model_id,
        "api_model": cfg["api_model"],
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": final_prompt}]}],
        "max_tokens": BATCH_OUTPUT_TOKENS,
    }
    return req, cfg["provider"], approximate_token_count(export)


def run_batch(app, users, template, model_override, dry_run,
              poll_interval, max_wait):
    keys = apply_batch_key_override(
        get_api_keys_for_usage(app.config, "chat"), app.config)

    # One batch per user (own request / model / export), submitted up front,
    # then polled together so the async wait overlaps.
    pending = {}  # user_id -> {user, model_id, custom_id, provider_key, batch_id}
    for user in users:
        model_id = _resolve_model(app, user, model_override)
        try:
            built = _build_batch_request(app, user, template, model_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ user {user.id}: build FAILED — {type(e).__name__}: {e}")
            continue
        if not built:
            continue
        req, provider, export_tokens = built
        if dry_run:
            print(f"  user {user.id}: model={model_id}, export ~{export_tokens} "
                  f"tokens — [dry-run] not submitting")
            continue
        batch_ids = batch_submit({provider: [req]}, keys, "intentions")
        if not batch_ids:
            print(f"  ✗ user {user.id}: batch submit failed (see logs)")
            continue
        provider_key, batch_id = next(iter(batch_ids.items()))
        pending[user.id] = {
            "user": user, "model_id": model_id, "custom_id": req["custom_id"],
            "provider_key": provider_key, "batch_id": batch_id,
        }
        print(f"  user {user.id}: submitted batch {batch_id} ({provider_key}, "
              f"model={model_id}, export ~{export_tokens} tokens)")

    if dry_run:
        return
    if not pending:
        print("No batches submitted.")
        return

    print(f"Submitted {len(pending)} batch(es). Polling every {poll_interval}s "
          f"(up to {max_wait // 60} min)...")
    waited = 0
    while pending and waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        for uid in list(pending):
            info = pending[uid]
            try:
                results, still, _ = batch_check_and_collect(
                    {info["provider_key"]: info["batch_id"]}, keys)
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
                # Batch ended but this request produced no result (e.g. the
                # pre-sized export still overflowed). Report for a sync re-run.
                print(f"  ✗ user {uid}: batch {info['batch_id']} ended with no "
                      f"result — re-run sync (or with a smaller max_export_tokens)")
                pending.pop(uid)
        if pending:
            print(f"  ... {len(pending)} still processing ({waited // 60}m elapsed)")

    if pending:
        print(f"Timed out after {max_wait // 60} min; {len(pending)} batch(es) "
              f"still processing (≤24h SLA — they'll finish). Batch IDs:")
        for uid, info in pending.items():
            print(f"  user {uid}: {info['batch_id']} ({info['provider_key']})")
        print("Raise --max-wait, or collect & save them later.")


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
                         "batch per user, polled to completion.")
    ap.add_argument("--poll-interval", type=int, default=30,
                    help="Seconds between batch status polls (--batch).")
    ap.add_argument("--max-wait", type=int, default=3600,
                    help="Max seconds to poll before printing batch IDs and "
                         "exiting (--batch).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the export and stop before the LLM call + save.")
    args = ap.parse_args()

    user_ids = args.user_ids or [1]

    app = create_app()
    with app.app_context():
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
                      args.poll_interval, args.max_wait)
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
