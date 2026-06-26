#!/usr/bin/env python3
"""
One-time backfill: generate each user's Intentions artifact from their archive,
using the `intentions_detection.txt` prompt.

Mirrors profile generation (backend/tasks/exports.py:generate_user_profile):
it builds the {user_export} via the same engaged-threads topology, runs a
single LLM completion with the same prompt-too-long retry/shrink loop, and
saves the result as a NEW version of the user's `intentions` UserArtifact —
with the same metadata profile generation records:
  * model        -> UserArtifact.generated_by
  * LLM tokens   -> UserArtifact.tokens_used (total input+output)
  * date         -> UserArtifact.created_at
plus an APICostLog row (request_type="intentions_backfill").

Non-destructive: each run creates a new artifact version; prior content stays
in the artifact's history.

Privacy: any selected user who has globally opted out of AI usage
(default_ai_usage='none') is skipped entirely — their data is never sent to an
LLM.

Model per user: their `preferred_model` if set and supported, else Opus 4.8
(override for all with --model).

The {user_export?...} params (keep, max_export_tokens) are read straight from
the prompt file, so the prompt is the single source of truth. With
max_export_tokens=1000000 the first attempt may overflow the model's context;
the retry loop catches the provider's PromptTooLongError and shrinks the budget
proportionally (reduce_export_tokens) until it fits — up to 3 retries.

Usage:
    python backend/scripts/backfill_intentions.py                  # just user 1 (operator)
    python backend/scripts/backfill_intentions.py 46 27 1 33 50 31 42 45
    python backend/scripts/backfill_intentions.py --model claude-opus-4.8 1 27
    python backend/scripts/backfill_intentions.py --dry-run 1      # build export, skip LLM + save
"""
import argparse
import os
import sys

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
from backend.llm_providers import LLMProvider, PromptTooLongError  # noqa: E402

DEFAULT_MODEL = "claude-opus-4.8"
PROMPT_FILE = "intentions_detection.txt"
KIND = "intentions"
MAX_RETRIES = 3


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


def backfill_user(app, user, template, model_id, dry_run=False):
    # Privacy gate: if the user has globally opted out of AI usage
    # (default_ai_usage='none'), do not send their data to an LLM at all —
    # skip before building the export or calling the model.
    if user.default_ai_usage not in AI_ALLOWED:
        print(f"  - user {user.id}: default_ai_usage='{user.default_ai_usage}' "
              f"(opted out) — skipping, not sending data to any LLM")
        return
    if model_id not in app.config["SUPPORTED_MODELS"]:
        print(f"  ! user {user.id}: unsupported model '{model_id}', skipping")
        return

    # The prompt owns the export params (keep / max_export_tokens).
    m = USER_EXPORT_PATTERN.search(template)
    if not m:
        print("  ! prompt has no {user_export} placeholder, aborting")
        return
    params = parse_placeholder_params(m.group(1) or "")
    max_export_tokens = parse_max_export_tokens(params.get("max_export_tokens"))
    chronological = params.get("keep") == "oldest"

    api_keys = get_api_keys_for_usage(app.config, "chat")

    response = None
    for attempt in range(MAX_RETRIES + 1):
        # Same scope as the {user_export} placeholder: engaged-threads
        # topology, AI-readable nodes only (ai_usage in {chat, train}).
        export = build_user_export_content(
            user,
            max_tokens=max_export_tokens,
            filter_ai_usage=True,
            chronological_order=chronological,
            include_strategy="engaged_threads",
        )
        if not export:
            print(f"  ! user {user.id}: no AI-readable archive, skipping")
            return
        export_tokens = approximate_token_count(export)
        final_prompt = USER_EXPORT_PATTERN.sub(lambda _m: export, template, count=1)
        print(f"  user {user.id}: model={model_id}, export ~{export_tokens} tokens "
              f"(attempt {attempt + 1}, budget={max_export_tokens})")
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

    content = response["content"]
    total_tokens = response["total_tokens"]
    input_tokens = response.get("input_tokens", 0)
    output_tokens = response.get("output_tokens", 0)

    # Cost log — same shape as profile generation.
    cost = calculate_llm_cost_microdollars(model_id, input_tokens, output_tokens)
    db.session.add(APICostLog(
        user_id=user.id, model_id=model_id, request_type="intentions_backfill",
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_microdollars=cost,
    ))

    # New artifact version (non-destructive). ai_usage follows the user's
    # global default (like profile generation); privacy stays "private" (the
    # UserArtifact default). Metadata mirrors the profile: generated_by=model,
    # tokens_used=LLM total, created_at=now.
    artifact = UserArtifact(
        user_id=user.id,
        kind=KIND,
        title=UserArtifact.DEFAULT_KINDS.get(KIND, "Intentions"),
        generated_by=model_id,
        tokens_used=total_tokens,
        ai_usage=user.default_ai_usage,
    )
    artifact.set_content(content)
    db.session.add(artifact)
    db.session.commit()

    version = UserArtifact.query.filter_by(user_id=user.id, kind=KIND).count()
    print(f"  ✓ user {user.id}: saved intentions v{version} "
          f"({len(content)} chars, model={model_id}, "
          f"llm_tokens={total_tokens}, cost=${cost / 1e6:.4f})")


def main():
    ap = argparse.ArgumentParser(
        description="One-time backfill of users' Intentions artifacts.")
    ap.add_argument("user_ids", nargs="*", type=int,
                    help="User IDs to backfill (default: 1, the operator).")
    ap.add_argument("--model", default=None,
                    help="Force this model for all users (default: each user's "
                         f"preferred_model, else {DEFAULT_MODEL}).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the export and stop before the LLM call + save.")
    args = ap.parse_args()

    user_ids = args.user_ids or [1]

    app = create_app()
    with app.app_context():
        template = _load_template(app)
        print(f"Backfilling intentions for users: {user_ids}"
              + (" [DRY RUN]" if args.dry_run else ""))
        for uid in user_ids:
            user = User.query.get(uid)
            if not user:
                print(f"  ! user {uid} not found, skipping")
                continue
            model_id = _resolve_model(app, user, args.model)
            try:
                backfill_user(app, user, template, model_id, dry_run=args.dry_run)
            except Exception as e:  # noqa: BLE001 — one user's failure mustn't abort the rest
                db.session.rollback()
                print(f"  ✗ user {uid}: FAILED — {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
