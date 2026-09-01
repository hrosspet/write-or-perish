"""Estimate the real input tokens an intentions run costs for a Community
Archive account, using the #276 compact export rendering.

Mirrors the prod pipeline offline (no DB needed):

  CA parquet snapshot
    -> to_export_entry()      (backend/utils/community_archive.py)
    -> compact_row()          (backend/utils/twitter_archive.py; drops RTs)
    -> node fields            (backend/routes/import_data.py: content=full_text,
                               created_at=_parse_tweet_ts, token_count=chars//4)
    -> compact run rendering  (backend/routes/export_data.py
                               _build_user_export_incremental)
    -> {user_export} substituted into prompts/intentions_detection.txt
    -> Anthropic count_tokens per model (tokenizers differ by model family)

Usage:
    python estimate_prefill_tokens.py majamediaco \
        --parquet ~/data/twitter/community-archive-snapshot-2026-09-01
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.utils.community_archive import (  # noqa: E402
    fetch_account_parquet, iter_tweets_parquet, to_export_entry)
from backend.utils.twitter_archive import compact_row  # noqa: E402
from backend.routes.import_data import _parse_tweet_ts  # noqa: E402

PROMPT_PATH = os.path.join(REPO, "backend", "prompts", "intentions_detection.txt")
# {user_export?keep=newest&max_export_tokens=1000000}
USER_EXPORT_PATTERN = re.compile(r"\{user_export(\?[^}]*)?\}")
MAX_EXPORT_TOKENS = 1_000_000   # from the prompt placeholder
KEEP_NEWEST = True              # keep=newest -> chronological_order=False

# Models to price. `count` is the model id to count tokens with (token counts
# are model-specific; the Opus 4.7-generation tokenizer is shared by
# opus-4.7/4.8/5, fable-5 and sonnet-5).
MODELS = [
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]


def load_rows(handle, snapshot_dir, include_replies=True):
    """Tweets as the prefill importer would see them (RTs dropped by
    compact_row; replies kept — prefill defaults include_replies=True)."""
    acct = fetch_account_parquet(handle, snapshot_dir)
    if not acct:
        sys.exit(f"@{handle} not in the snapshot")
    seen, rows, retweets = set(), [], 0
    for raw in iter_tweets_parquet(acct["account_id"], snapshot_dir):
        if raw["tweet_id"] in seen:
            continue
        seen.add(raw["tweet_id"])
        entry = to_export_entry(raw)["tweet"]
        row = compact_row(entry)
        if row is None:
            retweets += 1
            continue
        if not include_replies and row["is_reply"]:
            continue
        ts = _parse_tweet_ts(row["created_at"])
        if ts is None:
            continue
        rows.append({"created_at": ts, "content": row["full_text"],
                     "token_count": row["token_count"],
                     "is_reply": row["is_reply"]})
    return acct, rows, retweets


def apply_budget(rows, max_tokens, keep_newest=True):
    """The export budget window, in stored token units (chars//4), keeping
    newest (or oldest) first until the budget is spent."""
    ordered = sorted(rows, key=lambda r: r["created_at"], reverse=keep_newest)
    kept, total = [], 0
    for r in ordered:
        if total + r["token_count"] > max_tokens:
            break
        kept.append(r)
        total += r["token_count"]
    return kept, total


def render_export(username, rows, max_tokens, chronological_order=False):
    """Byte-identical to _build_user_export_incremental's compact path for a
    corpus that is entirely flat imported tweets (no threads, no artifacts)."""
    entries = sorted(rows, key=lambda r: r["created_at"],
                     reverse=not chronological_order)
    lines = []
    lines.append("# Loore - Thread Export")
    lines.append("")
    lines.append(f"**User:** {username}")
    lines.append(
        f"**Export Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"**Entry Points:** {len(entries)}")
    if max_tokens:
        lines.append(f"*(Limited to ~{max_tokens:,} tokens)*")
    lines.append("")
    lines.append("---")
    lines.append("")
    # single compact run: every entry is a childless twitter-origin root
    lines.append(
        f"# Tweets by {username} (imported from Twitter/X) — "
        f"{len(entries)} tweets")
    lines.append("")
    for t in entries:
        lines.append(f"[{t['created_at'].strftime('%Y-%m-%d %H:%M')}] {t['content']}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*End of Export*")
    return "\n".join(lines)


def build_prompt(export):
    with open(PROMPT_PATH, encoding="utf-8") as f:
        template = f.read()
    return USER_EXPORT_PATTERN.sub(lambda _m: export, template, count=1)


def count_tokens(prompt, models):
    import anthropic
    client = anthropic.Anthropic()
    out = {}
    for m in models:
        try:
            r = client.messages.count_tokens(
                model=m, messages=[{"role": "user", "content": prompt}])
            out[m] = r.input_tokens
            print(f"  {m:<20} {r.input_tokens:>10,} tokens", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - report and continue
            out[m] = None
            print(f"  {m:<20} ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("handle")
    p.add_argument("--parquet", required=True)
    p.add_argument("--no-replies", action="store_true")
    p.add_argument("--out", default=None, help="write the rendered export here")
    p.add_argument("--json", default=None, help="write the result JSON here")
    args = p.parse_args()

    snapshot = os.path.expanduser(args.parquet)
    acct, rows, retweets = load_rows(
        args.handle, snapshot, include_replies=not args.no_replies)
    stored_total = sum(r["token_count"] for r in rows)
    chars = sum(len(r["content"]) for r in rows)
    replies = sum(1 for r in rows if r["is_reply"])
    print(f"@{acct['username']}: {len(rows)} importable tweets "
          f"({replies} replies, {retweets} retweets dropped), "
          f"{chars:,} chars, {stored_total:,} stored token units",
          file=sys.stderr)

    kept, kept_stored = apply_budget(rows, MAX_EXPORT_TOKENS, KEEP_NEWEST)
    if len(kept) < len(rows):
        print(f"  budget window: {len(kept)}/{len(rows)} tweets "
              f"({kept_stored:,} of {MAX_EXPORT_TOKENS:,} stored units)",
              file=sys.stderr)

    export = render_export(acct["username"], kept, MAX_EXPORT_TOKENS,
                           chronological_order=not KEEP_NEWEST)
    prompt = build_prompt(export)
    print(f"export: {len(export):,} chars | full prompt: {len(prompt):,} chars",
          file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(prompt)

    print("counting real tokens (Anthropic count_tokens):", file=sys.stderr)
    counts = count_tokens(prompt, MODELS)

    result = {
        "handle": acct["username"],
        "tweets_total": len(rows),
        "tweets_in_export": len(kept),
        "replies": replies,
        "retweets_dropped": retweets,
        "chars": chars,
        "stored_token_units": stored_total,
        "export_chars": len(export),
        "prompt_chars": len(prompt),
        "real_tokens": counts,
    }
    print(json.dumps(result, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
