#!/usr/bin/env python3
"""
Backfill Node.token_count for LLM-reply nodes to the chars/4 estimate.

Historically `generate_llm_response` stored the provider-reported
output_tokens into Node.token_count, while every other node-creation
path (user text, voice transcripts, imports — both roles) stored
approximate_token_count (chars/4). Provider tokenizer counts run ~1.5x
chars/4 and drift upward across model generations, so windows mixing
LLM replies with user text were skewed: profile chunk budgets filled
"faster" on chat-heavy data, breaking the approx-equal-chunk promise
and (combined with the rendered chars/4 re-measure) starving the
chunked regen's stopping criterion.

Going forward llm_completion stores chars/4; this script converges the
historical rows. Real token usage remains in APICostLog.

Usage (on prod, from repo root):
    python backend/scripts/backfill_llm_token_counts.py            # dry run
    python backend/scripts/backfill_llm_token_counts.py --execute
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from backend.models import Node
from backend.utils.tokens import approximate_token_count

BATCH_SIZE = 500


def main(execute):
    app = create_app()
    with app.app_context():
        ids = [r[0] for r in (db.session.query(Node.id)
                              .filter(Node.node_type == "llm")
                              .order_by(Node.id).all())]
        print(f"{len(ids)} llm nodes to examine")
        changed = 0
        old_sum = 0
        new_sum = 0
        decrypt_failures = 0
        for start in range(0, len(ids), BATCH_SIZE):
            batch = (Node.query
                     .filter(Node.id.in_(ids[start:start + BATCH_SIZE]))
                     .all())
            for node in batch:
                try:
                    text = node.get_content() or ""
                except Exception:
                    decrypt_failures += 1
                    continue
                new_count = approximate_token_count(text)
                if new_count == node.token_count:
                    continue
                old_sum += node.token_count or 0
                new_sum += new_count
                changed += 1
                if execute:
                    node.token_count = new_count
            if execute:
                db.session.commit()
            done = min(start + BATCH_SIZE, len(ids))
            print(f"  {done}/{len(ids)} examined, {changed} to change",
                  end="\r")
        print()
        print(f"{'updated' if execute else 'would update'}: {changed} nodes")
        print(f"token_count sum over changed nodes: {old_sum} -> {new_sum} "
              f"({new_sum - old_sum:+d})")
        if decrypt_failures:
            print(f"WARNING: {decrypt_failures} nodes failed to decrypt "
                  f"(skipped)")
        if not execute:
            print("\nDry run — re-run with --execute to apply.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill llm-node token_count to chars/4")
    parser.add_argument("--execute", action="store_true",
                        help="apply changes (default: dry run)")
    args = parser.parse_args()
    main(args.execute)
