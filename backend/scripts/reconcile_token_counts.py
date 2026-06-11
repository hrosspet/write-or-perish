#!/usr/bin/env python3
"""
Reconcile Node.token_count with chars/4 of each node's current content.

Node.token_count is the platform's information-content measure (chunk
windowing, profile-update gates, and balance decisions all sum it), but
several historical paths let it drift from the content:

- classic-upload voice transcription never updated it after writing the
  transcript (nodes kept the ~9-token placeholder estimate, making long
  recordings nearly invisible to chunk windowing and update gates);
- node edits (PUT /nodes/<id>) never recomputed it, freezing the
  creation-time count regardless of later changes;
- native LLM replies stored provider output_tokens (converged earlier
  by backfill_llm_token_counts.py — they no-op here).

Both leaks are fixed at the source alongside this script; this
converges the historical rows. Idempotent: re-running reports 0.

Usage (on prod, from repo root):
    python backend/scripts/reconcile_token_counts.py            # dry run
    python backend/scripts/reconcile_token_counts.py --execute
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
# Only report individual nodes with at least this much drift; tiny
# diffs are still fixed but not worth a log line each.
REPORT_THRESHOLD = 1000


def main(execute):
    app = create_app()
    with app.app_context():
        ids = [r[0] for r in (db.session.query(Node.id)
                              .order_by(Node.id).all())]
        print(f"{len(ids)} nodes to examine")
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
                if new_count == (node.token_count or 0):
                    continue
                drift = new_count - (node.token_count or 0)
                if abs(drift) >= REPORT_THRESHOLD:
                    print(f"  node {node.id}: user={node.user_id} "
                          f"type={node.node_type} "
                          f"{node.token_count} -> {new_count} ({drift:+d})")
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
        description="Reconcile Node.token_count with current content")
    parser.add_argument("--execute", action="store_true",
                        help="apply changes (default: dry run)")
    args = parser.parse_args()
    main(args.execute)
