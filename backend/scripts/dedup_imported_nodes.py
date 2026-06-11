#!/usr/bin/env python3
"""One-off removal of pre-source_key import duplicates (issue #136 / PR #174).

Before PR #174 the import confirm endpoints had no dedup, so importing
the same archive N times created N identical copies of every imported
message. Those copies have no ``source_key`` (the column didn't exist),
so the new dedup can't see them. This script deletes ALL copies of the
duplicated import data — the user can re-import the archive afterwards,
which recreates it once, with proper source keys.

Detection
---------
Prod diagnosis for user 44 showed duplicate copies share content but
NOT ``created_at`` (the archive timestamps differed between imports —
e.g. zip file mtimes change on re-download), so the dedup key is the
content fingerprint alone, restricted to nodes that are import-shaped:

  - ``created_at`` backdated: imports write the archive's timestamp,
    so insert time (``updated_at``) is much later. Native nodes get
    ``created_at`` = insert time. A native node whose ``updated_at``
    moved later through edits is excluded by the next rule;
  - never edited (no NodeVersion rows) and not pinned: anything the
    user touched is never deleted.

Within those candidates, nodes sharing a content fingerprint with at
least one other candidate are duplicates. A group is only deleted when
its copies were inserted in at least two distinct batches (minute-level
``updated_at`` clusters) — same-batch duplicates were duplicated in the
source archive itself, not by re-importing, and are left alone.

What --execute does
-------------------
Soft-deletes every member of every duplicate group (``deleted_at =
now``) — the same machinery as in-app deletion: reversible for
SOFT_DELETE_GRACE_DAYS, then the daily cleanup task purges rows and
re-points links/drafts. Alive replies chained onto a deleted copy stay
alive; their parent becomes a content-wiped tombstone shell, exactly as
when a user deletes a thread others have replied to (the dry run
reports how many such replies exist).

Nothing is hard-deleted and the whole run is one transaction.

Output is metadata-only (ids, counts, timestamps) — no content is ever
printed. Run with the production env loaded so encrypted content can be
fingerprinted:
    set -a; source ~/write-or-perish/.env.production; set +a
    python backend/scripts/dedup_imported_nodes.py --user-id 44            # dry run
    python backend/scripts/dedup_imported_nodes.py --user-id 44 --execute  # apply
"""

import argparse
import hashlib
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.app import create_app                      # noqa: E402
from backend.extensions import db                       # noqa: E402
from backend.models import Node, NodeVersion            # noqa: E402
from backend.utils.encryption import (                  # noqa: E402
    decrypt_content, is_encryption_enabled,
)

# Nodes whose content couldn't be fingerprinted (KMS errors / missing
# encryption env). Anything here makes the run untrustworthy.
FP_FAILURES = 0

# created_at this much older than updated_at marks a node import-shaped
# (insert long after the content's own timestamp).
BACKDATE_MIN = timedelta(days=1)


def _chunked(seq, size=1000):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _plaintext(node):
    """Decrypted content; None when not comparable."""
    global FP_FAILURES
    if not node.content:
        return None
    try:
        text = decrypt_content(node.content)
    except Exception:
        FP_FAILURES += 1
        return None
    if not text:
        return None
    if text.startswith("ENC:"):
        # decrypt_content returned ciphertext as-is (encryption env not
        # configured in this shell) — each row has a unique DEK/nonce,
        # so hashing it would be garbage.
        FP_FAILURES += 1
        return None
    return text


def _hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _normalized(text):
    """Content with the import-added H1 line stripped and whitespace
    collapsed — catches copies that differ only in title prefix /
    formatting drift between imports."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return " ".join(" ".join(lines).split())


def main():
    parser = argparse.ArgumentParser(
        description="Delete pre-source_key import duplicates for one user."
    )
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply changes. Without this flag the script only reports.",
    )
    parser.add_argument(
        "--normalized", action="store_true",
        help="Group on normalized content (H1 title line stripped, "
             "whitespace collapsed) instead of exact content — for "
             "copies that drifted in formatting between imports. Both "
             "keys are always reported; this picks which one acts.",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        nodes = (
            Node.query
            .filter(Node.human_owner_id == args.user_id)
            .filter(Node.deleted_at.is_(None))
            .all()
        )
        print(f"user {args.user_id}: {len(nodes)} alive nodes "
              f"({sum(n.token_count or 0 for n in nodes):,} tokens)")

        enc = sum(1 for n in nodes
                  if n.content and n.content.startswith("ENC:"))
        print(f"encryption env configured: {is_encryption_enabled()}; "
              f"nodes encrypted at rest: {enc}, "
              f"plaintext: {len(nodes) - enc}")

        # ── Import-shaped candidates ─────────────────────────────────
        backdated = [
            n for n in nodes
            if n.created_at and n.updated_at
            and (n.updated_at - n.created_at) > BACKDATE_MIN
        ]
        edited_ids = set()
        for chunk in _chunked([n.id for n in backdated]):
            edited_ids.update(
                nid for (nid,) in
                db.session.query(NodeVersion.node_id)
                .filter(NodeVersion.node_id.in_(chunk)).distinct()
            )
        candidates = [
            n for n in backdated
            if n.id not in edited_ids and not n.pinned_at
        ]
        print(f"import-shaped candidates (backdated, never edited, "
              f"not pinned): {len(candidates)} of {len(nodes)} "
              f"(excluded: {len(nodes) - len(backdated)} not backdated, "
              f"{len(edited_ids)} edited, "
              f"{sum(1 for n in backdated if n.pinned_at)} pinned)")

        # ── Diagnostics: import batches and node types ────────────────
        def minute(n):
            return n.updated_at.strftime("%Y-%m-%d %H:%M")

        batch_nodes = Counter(minute(n) for n in candidates if n.updated_at)
        batch_tokens = defaultdict(int)
        for n in candidates:
            if n.updated_at:
                batch_tokens[minute(n)] += n.token_count or 0
        print(f"\ninsert-time batches among candidates "
              f"({len(batch_nodes)} distinct minutes; top 15):")
        for ts, count in batch_nodes.most_common(15):
            print(f"  {ts}  {count:6d} nodes  "
                  f"{batch_tokens[ts]:10,d} tokens")

        types = Counter((n.node_type, n.llm_model or "-")
                        for n in candidates)
        print(f"candidate (node_type, llm_model) distribution: "
              f"{dict(types)}")

        # ── Group candidates by content fingerprint ──────────────────
        exact_groups = defaultdict(list)
        norm_groups = defaultdict(list)
        for n in candidates:
            text = _plaintext(n)
            if text is None:
                continue
            exact_groups[_hash(text)].append(n)
            norm_groups[_hash(_normalized(text))].append(n)

        if FP_FAILURES:
            print(f"WARNING: {FP_FAILURES} nodes could not be "
                  f"fingerprinted (decryption failed or unavailable) "
                  f"and were EXCLUDED from grouping. Fix the encryption "
                  f"env (GCP_KMS_KEY_NAME etc.) before trusting this "
                  f"run.")

        def batches(group):
            """Distinct insert-time minutes across a group's copies."""
            return {minute(n) for n in group if n.updated_at}

        def split(groups):
            multi = [g for g in groups.values() if len(g) > 1]
            cross = [g for g in multi if len(batches(g)) >= 2]
            intra = [g for g in multi if len(batches(g)) < 2]
            return cross, intra

        exact_cross, exact_intra = split(exact_groups)
        norm_cross, norm_intra = split(norm_groups)
        for label, cross in (("exact", exact_cross),
                             ("normalized", norm_cross)):
            nodes_n = sum(len(g) for g in cross)
            toks = sum(n.token_count or 0 for g in cross for n in g)
            print(f"{label}-content groups spanning >=2 batches: "
                  f"{len(cross)} groups, {nodes_n} nodes, "
                  f"{toks:,} tokens")

        if args.normalized:
            groups, dup_groups, intra_batch = (
                norm_groups, norm_cross, norm_intra)
            print("acting on: NORMALIZED content key")
        else:
            groups, dup_groups, intra_batch = (
                exact_groups, exact_cross, exact_intra)
            print("acting on: exact content key "
                  "(use --normalized to act on the normalized key)")

        if intra_batch:
            print(f"same-batch duplicate groups left untouched "
                  f"(duplicated inside the source archive, not by "
                  f"re-importing): {len(intra_batch)} groups, "
                  f"{sum(len(g) for g in intra_batch)} nodes")

        if not dup_groups:
            print("No re-import duplicate groups found — nothing to do.")
            return

        doomed = [n for g in dup_groups for n in g]
        doomed_ids = {n.id for n in doomed}
        removed_tokens = sum(n.token_count or 0 for n in doomed)

        # ── Report (metadata only) ───────────────────────────────────
        copies_hist = Counter(len(g) for g in dup_groups)
        print(f"\nduplicate groups (distinct archive messages): "
              f"{len(dup_groups)}")
        print(f"copies-per-group histogram: "
              f"{dict(sorted(copies_hist.items()))}")
        print(f"nodes to soft-delete (ALL copies): {len(doomed)} "
              f"({removed_tokens:,} tokens)")

        batch_hist = Counter(
            n.updated_at.strftime("%Y-%m-%d %H:%M")
            for n in doomed if n.updated_at
        )
        print("\ntop insert-time clusters among duplicates "
              "(import batches, minute resolution):")
        for ts, count in batch_hist.most_common(10):
            print(f"  {ts}  {count} nodes")

        attached_alive = 0
        for chunk in _chunked(doomed_ids):
            for c in Node.query.filter(Node.parent_id.in_(chunk)):
                if c.id not in doomed_ids and c.deleted_at is None:
                    attached_alive += 1
        print(f"\nalive replies chained onto deleted copies "
              f"(will keep tombstone parents): {attached_alive}")

        singletons = sum(1 for g in groups.values() if len(g) == 1)
        print(f"imported-looking singletons left untouched (would "
              f"duplicate once on the next re-import, FYI): "
              f"{singletons}")

        if not args.execute:
            print("\nDry run — nothing changed. "
                  "Re-run with --execute to apply.")
            return

        # ── Apply ────────────────────────────────────────────────────
        now = datetime.utcnow()
        for n in doomed:
            n.deleted_at = now
        db.session.commit()
        print(f"\nDone: {len(doomed)} duplicate nodes soft-deleted "
              f"({removed_tokens:,} tokens). Purge follows the normal "
              f"grace period via the daily cleanup task.")


if __name__ == "__main__":
    main()
