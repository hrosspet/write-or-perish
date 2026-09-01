"""Chunked intentions run: split a user's corpus into N time-ordered chunks of
roughly equal token weight and submit one intentions request per (chunk, model)
through the Batch API.

Purpose: Haiku 4.5's 200K window can't hold the whole export, so the single-shot
run only saw ~32% of the corpus — which confounds "cheap model" with "less
data". Three chunks give it full coverage, making the comparison against luna's
whole-context single call apples-to-apples.

No aggregation step: each chunk's intentions are written out separately for
manual comparison.

Run inside the backend container:

    docker compose cp experiments/cheap-model-intentions/chunked_batch_run.py \
        backend:/app/chunked_batch_run.py
    docker compose exec -T backend python /app/chunked_batch_run.py 21 --submit
    docker compose exec -T backend python /app/chunked_batch_run.py 21 --collect

(the container mounts only backend/, so the script is copied in)
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, "/app")

from backend.app import create_app  # noqa: E402
from backend.models import Node, User  # noqa: E402
from backend.routes.export_data import build_user_export_content  # noqa: E402
from backend.llm_providers import LLMProvider  # noqa: E402
from backend.utils.api_keys import get_api_keys_for_usage  # noqa: E402
from backend.utils.llm_batch import (  # noqa: E402
    apply_batch_key_override, batch_submit, batch_check_and_collect)

# The admin "Infer intentions" path (backend/tasks/intentions.py) uses the
# PUBLIC fork for tweet corpora — it frames the archive as a performed
# register and tells the model to ground every claim in what was posted.
# backfill_intentions.py uses the private-archive prompt; comparing the two
# confounds prompt with model, so default to the public one here.
PROMPT_FILE = "intentions_detection_public.txt"
USER_EXPORT_PATTERN = re.compile(r"\{user_export(\?[^}]*)?\}")
MODELS = ["claude-haiku-4.5", "gpt-5.6-luna"]
BATCH_OUTPUT_TOKENS = 8192
STATE = "/app/chunk_batch_state.json"
OUT_DIR = "/app"


# Per-entry scaffolding in the compact export ("[YYYY-MM-DD HH:MM] " plus the
# blank line), in stored units. Balancing on node token_count alone under-counts
# chunks made of many short tweets: measured on this corpus the export is
# 387,868 stored units vs 305,451 of node content over 14,636 entries, i.e.
# ~5.6 units of scaffolding each. Ignoring it put chunk 3 (24% more, shorter
# tweets) 5K real tokens over Haiku's window.
ENTRY_OVERHEAD = 5.63


def chunk_bounds(user_id, n):
    """Time-ordered split points holding ~equal *rendered* weight."""
    rows = (Node.query.filter_by(user_id=user_id, origin="twitter")
            .filter(Node.deleted_at.is_(None))
            .order_by(Node.created_at).all())
    def w(r):
        return (r.token_count or 0) + ENTRY_OVERHEAD
    total = sum(w(r) for r in rows)
    target, acc, bounds = total / n, 0, []
    for r in rows:
        acc += w(r)
        if acc >= target * (len(bounds) + 1) and len(bounds) < n - 1:
            bounds.append(r.created_at)
    print(f"  {len(rows):,} nodes, {sum(r.token_count or 0 for r in rows):,} "
          f"content units, {total:,.0f} weighted, target {target:,.0f}/chunk")
    return bounds, total


def build_chunk(user, template, lo, hi):
    """Export restricted to (lo, hi]; None/None = unbounded."""
    export = build_user_export_content(
        user, max_tokens=1_000_000, filter_ai_usage=True,
        created_after=lo, created_before=hi,
        chronological_order=False, include_strategy="engaged_threads")
    if not export:
        return None, 0
    prompt = USER_EXPORT_PATTERN.sub(lambda _m: export, template, count=1)
    return prompt, export.count("\n[")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id", type=int)
    ap.add_argument("--chunks", type=int, default=3)
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--only", choices=["anthropic", "openai"], default=None,
                    help="submit only this provider's batch (the other may "
                         "already be in flight)")
    ap.add_argument("--sync", action="store_true",
                    help="run every request synchronously instead of batching")
    ap.add_argument("--prompt-file", default=PROMPT_FILE)
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        keys = apply_batch_key_override(
            get_api_keys_for_usage(app.config, "chat"), app.config)

        if args.collect:
            state = json.load(open(STATE))
            results, pending, durations = batch_check_and_collect(
                {k: v for k, v in state["batch_ids"].items()}, keys)
            if pending:
                print(f"still pending: {pending}")
            for cid, r in sorted(results.items()):
                path = os.path.join(OUT_DIR, f"intentions_chunk_{cid}.txt")
                open(path, "w", encoding="utf-8").write(r["content"])
                print(f"  {cid:<28} in={r['input_tokens']:>8,} "
                      f"out={r['output_tokens']:>6,} -> {path}")
            print(f"durations: {durations}")
            return

        user = User.query.get(args.user_id)
        template = open(os.path.join(app.root_path, "prompts",
                                     args.prompt_file), encoding="utf-8").read()
        print(f"  prompt: {args.prompt_file}")
        bounds, total = chunk_bounds(args.user_id, args.chunks)
        edges = [None] + bounds + [None]

        reqs, meta = {"anthropic": [], "openai": []}, {}
        for i in range(args.chunks):
            lo, hi = edges[i], edges[i + 1]
            prompt, ntweets = build_chunk(user, template, lo, hi)
            if not prompt:
                print(f"  chunk {i+1}: empty, skipping")
                continue
            messages = [{"role": "user",
                         "content": [{"type": "text", "text": prompt}]}]
            for m in MODELS:
                cfg = app.config["SUPPORTED_MODELS"][m]
                real = LLMProvider.count_tokens(m, messages, keys)
                limit = cfg["context_window"] - BATCH_OUTPUT_TOKENS
                fits = "OK" if (real or 0) <= limit else "OVERFLOW"
                short = m.split("-")[1][:5]
                cid = f"c{i+1}_{short}"
                print(f"  chunk {i+1} [{lo or 'start'} .. {hi or 'end'}] "
                      f"{ntweets:,} tweets | {m}: {real:,} tok {fits}")
                if fits == "OVERFLOW":
                    print("    !! skipping this request")
                    continue
                reqs[cfg["provider"]].append({
                    "custom_id": cid, "model_id": m,
                    "api_model": cfg["api_model"], "messages": messages,
                    "max_tokens": BATCH_OUTPUT_TOKENS})
                meta[cid] = {"chunk": i + 1, "model": m, "tweets": ntweets,
                             "input_tokens": real}

        if args.sync:
            for req in reqs["anthropic"] + reqs["openai"]:
                r = LLMProvider.get_completion(
                    req["model_id"], req["messages"], keys,
                    max_tokens=req["max_tokens"])
                path = os.path.join(
                    OUT_DIR, f"intentions_chunk_{req['custom_id']}.txt")
                open(path, "w", encoding="utf-8").write(r["content"])
                print(f"  {req['custom_id']:<10} in={r.get('input_tokens',0):>8,} "
                      f"out={r.get('output_tokens',0):>6,} -> {path}")
            return

        if not args.submit:
            print("\n(dry run — pass --submit to send the batches)")
            return
        if args.only:
            reqs = {args.only: reqs.get(args.only, [])}
        batch_ids = batch_submit(reqs, keys, "intentions-chunked")
        # merge, so submitting one provider doesn't drop the other's id
        prev = json.load(open(STATE)) if os.path.exists(STATE) else {}
        batch_ids = {**prev.get("batch_ids", {}), **batch_ids}
        meta = {**prev.get("meta", {}), **meta}
        json.dump({"batch_ids": batch_ids, "meta": meta},
                  open(STATE, "w"), indent=2, default=str)
        print(f"\nsubmitted: {batch_ids}")


if __name__ == "__main__":
    main()
