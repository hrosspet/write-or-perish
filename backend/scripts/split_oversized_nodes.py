#!/usr/bin/env python3
"""
Split existing nodes above NODE_CHAR_CAP into serial chains.

Companion backfill for the per-node content cap (utils/node_split.py):
the cap is enforced at creation going forward; this converges the
historical oversized nodes (e.g. the ~900KB single-node paste that
stalled a user's chunked profile regen for five weeks).

Per node: the original keeps its id and the first segment (preserving
quotes, source_key, audio linkage, artifacts); the remainder becomes
new child nodes chained in series with +1ms created_at steps; existing
children re-parent onto the last part. The original full content is
preserved as a NodeVersion for recoverability.

Usage (on prod, from repo root):
    python backend/scripts/split_oversized_nodes.py            # dry run
    python backend/scripts/split_oversized_nodes.py --execute
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from sqlalchemy import func

from backend.app import create_app
from backend.extensions import db
from backend.models import Node, NodeVersion
from backend.utils.node_split import (
    NODE_CHAR_CAP, split_text_at_cap, split_node_into_chain,
)


def main(execute):
    app = create_app()
    with app.app_context():
        # Prefilter on stored (possibly encrypted) length — ciphertext
        # length is >= plaintext length, so this over-selects and never
        # misses; the decrypted check below decides.
        candidate_ids = [r[0] for r in (
            db.session.query(Node.id)
            .filter(func.length(Node.content) > NODE_CHAR_CAP,
                    Node.deleted_at.is_(None))
            .order_by(Node.id).all())]
        print(f"{len(candidate_ids)} candidate node(s) by stored length")
        split_count = 0
        parts_total = 0
        for nid in candidate_ids:
            node = db.session.get(Node, nid)
            try:
                text = node.get_content() or ""
            except Exception:
                print(f"  node {nid}: decrypt failed — skipped")
                continue
            segments = split_text_at_cap(text)
            if len(segments) <= 1:
                continue
            print(f"  node {nid}: user={node.user_id} type={node.node_type} "
                  f"{len(text)} chars -> {len(segments)} parts")
            if not execute:
                split_count += 1
                parts_total += len(segments)
                continue
            version = NodeVersion(node_id=node.id)
            version.set_content(text)
            db.session.add(version)
            parts = split_node_into_chain(node, segments=segments)
            db.session.commit()
            split_count += 1
            parts_total += 1 + len(parts)
        print(f"\n{'split' if execute else 'would split'}: "
              f"{split_count} node(s) into {parts_total} part(s) total")
        if not execute:
            print("Dry run — re-run with --execute to apply.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Split oversized nodes into serial chains")
    parser.add_argument("--execute", action="store_true",
                        help="apply changes (default: dry run)")
    args = parser.parse_args()
    main(args.execute)
