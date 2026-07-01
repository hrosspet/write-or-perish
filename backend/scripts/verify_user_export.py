#!/usr/bin/env python3
"""
Verify build_user_export_content for a specific user, mimicking the
{user_export?keep=newest&max_export_tokens=10000} placeholder call.

Prints:
  1. The actual export content (decrypted) — only safe for the script
     operator's own user_id.
  2. Per-node metadata for every node id in scope: created_at, author,
     ai_usage, token_count, and a flag for NULL token_count.
  3. Summary stats: total chars, approximate tokens, oldest/newest in
     scope, and a histogram by month.

Usage:
    python backend/scripts/verify_user_export.py [USER_ID] [MAX_TOKENS]

USER_ID defaults to 1 (operator). MAX_TOKENS defaults to 10000.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from collections import Counter
from datetime import datetime

from backend.app import create_app
from backend.extensions import db
from backend.models import Node, User
from backend.routes.export_data import build_user_export_content
from backend.utils.tokens import approximate_token_count


def _author_label(node):
    if node.node_type == "llm":
        return f"AI({node.llm_model or '?'})"
    return f"user({node.user.username if node.user else '?'})"


def main(user_id: int, max_tokens: int):
    app = create_app()
    with app.app_context():
        user = User.query.get(user_id)
        if user is None:
            print(f"No user with id={user_id}")
            return

        print(f"=== build_user_export_content for {user.username} "
              f"(id={user_id}) ===")
        print(f"  max_tokens={max_tokens}")
        print(f"  filter_ai_usage=True (matches {{user_export}})")
        print(f"  chronological_order=False (keep=newest)")
        print(f"  include_strategy='engaged_threads'")
        print(f"  created_before=now (same effect as placeholder cutoff)")
        print()

        result = build_user_export_content(
            user,
            max_tokens=max_tokens,
            filter_ai_usage=True,
            chronological_order=False,
            include_strategy="engaged_threads",
            created_before=datetime.utcnow(),
            return_metadata=True,
        )
        if result is None:
            print("Export returned None")
            return

        content = result["content"]
        node_ids = result["node_ids"]

        # ── Summary ─────────────────────────────────────────────────────
        print("--- Summary ---")
        print(f"chars              : {len(content)}")
        print(f"approx tokens      : {approximate_token_count(content)}")
        print(f"node_ids in CTE    : {len(node_ids)} (pre-budget set)")
        print(f"earliest_in_scope  : {result['earliest_node_created_at']}")
        print(f"latest_in_scope    : {result['latest_node_created_at']}")
        print()

        # ── Per-node metadata for everything in scope ───────────────────
        nodes = (
            Node.query
            .filter(Node.id.in_(list(node_ids)))
            .order_by(Node.created_at.desc())
            .all()
        )

        # print(f"--- Per-node metadata (newest first, n={len(nodes)}) ---")
        # print(f"{'created_at':<22} {'id':>8} {'author':<22} "
        #       f"{'ai_usage':<8} {'token_count':>12} null?")
        # for n in nodes:
        #     tk_repr = (
        #         f"{n.token_count}" if n.token_count is not None else "NULL"
        #     )
        #     null_flag = "<-- NULL" if n.token_count is None else ""
        #     print(
        #         f"{n.created_at!s:<22} {n.id:>8} "
        #         f"{_author_label(n):<22} {n.ai_usage:<8} "
        #         f"{tk_repr:>12} {null_flag}"
        #     )
        # print()

        # ── Histogram of CTE rows by month ──────────────────────────────
        by_month = Counter(
            n.created_at.strftime("%Y-%m") for n in nodes
        )
        null_by_month = Counter(
            n.created_at.strftime("%Y-%m")
            for n in nodes if n.token_count is None
        )
        sum_tokens_by_month = Counter()
        for n in nodes:
            sum_tokens_by_month[n.created_at.strftime("%Y-%m")] += (
                n.token_count or 0
            )

        print("--- Per-month histogram (CTE row set, pre-budget) ---")
        print(f"{'month':<8} {'count':>6} {'null_tk':>8} {'sum_tk':>10}")
        for month in sorted(by_month):
            print(
                f"{month:<8} {by_month[month]:>6} "
                f"{null_by_month[month]:>8} "
                f"{sum_tokens_by_month[month]:>10}"
            )
        print()

        total_null = sum(null_by_month.values())
        if total_null:
            print(
                f"WARNING: {total_null} node(s) have NULL token_count. "
                f"The budget loop treats NULL as 0, so these "
                f"slip past the cap."
            )
            print()

        # ── Full content dump ───────────────────────────────────────────
        # print("=" * 70)
        # print("EXPORT CONTENT (decrypted — operator's own user only)")
        # print("=" * 70)
        # print(content)


if __name__ == "__main__":
    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    main(user_id, max_tokens)
