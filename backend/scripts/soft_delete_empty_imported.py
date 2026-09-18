"""Soft-delete the empty imported nodes that predate the #317 guard.

Before #317 the twitter importer created a node for a tweet with no text
(a media-only post, whose text in the export is empty). The row carries
nothing: no words for the profile, a blank card in the Log, and content
that ``set_content()`` stores as the empty string, so no encryption pass
can ever touch it — which is what jammed the #265 backfill (#316).

Prod on 2026-09-18 held 39 of them across 8 accounts, every one a root
with no children, nothing referencing it, nothing pinned. This removes
exactly that shape and REFUSES anything else: a row with children (alive
or tombstoned), a row another node points at through ``linked_node_id``,
a row carrying a context artifact, or a public row is reported and left
alone. The snapshot is re-checked here rather than trusted, because the
graph can change between the survey and the run.

Deletion goes through ``soft_delete_node`` — the same path the app uses —
so the row gets ``deleted_at`` and loses any pin, and stays recoverable
for SOFT_DELETE_GRACE_DAYS. Public-cache invalidation is not needed: this
only touches non-public rows.

    cd /path/to/write-or-perish
    python backend/scripts/soft_delete_empty_imported.py            # dry run
    python backend/scripts/soft_delete_empty_imported.py --apply
    python backend/scripts/soft_delete_empty_imported.py --user-id 6 --apply

Verify after: the dry run reports 0 deletable.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from backend import create_app, db  # noqa: E402
from backend.models import Node, NodeContextArtifact  # noqa: E402
from backend.utils.node_deletion import soft_delete_node  # noqa: E402


def empty_imported_query(user_id=None):
    """Imported, non-public, not-yet-deleted rows with no content."""
    q = Node.query.filter(
        Node.source_key.isnot(None),
        Node.content == "",
        Node.privacy_level != "public",
        Node.deleted_at.is_(None),
    )
    if user_id is not None:
        q = q.filter(Node.human_owner_id == user_id)
    return q


def partition(rows):
    """Split rows into (deletable, held_back) by re-checking the graph.

    Three bulk queries, not a lazy load per node: walking children one
    row at a time is what pegged both prod CPUs on 2026-08-27.
    """
    ids = [n.id for n in rows]
    if not ids:
        return [], {}
    has_child = {pid for (pid,) in db.session.query(Node.parent_id)
                 .filter(Node.parent_id.in_(ids)).distinct().all()}
    linked = {lid for (lid,) in db.session.query(Node.linked_node_id)
              .filter(Node.linked_node_id.in_(ids)).distinct().all()}
    artifacts = {nid for (nid,) in db.session.query(NodeContextArtifact.node_id)
                 .filter(NodeContextArtifact.node_id.in_(ids)).distinct().all()}

    deletable, held = [], {}
    for node in rows:
        why = []
        if node.parent_id is not None:
            why.append("has a parent")
        if node.id in has_child:
            why.append("has children")
        if node.id in linked:
            why.append("referenced as linked_node")
        if node.id in artifacts:
            why.append("carries a context artifact")
        if node.pinned_at is not None:
            why.append("pinned")
        if why:
            held[node.id] = ", ".join(why)
        else:
            deletable.append(node)
    return deletable, held


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Soft-delete empty imported nodes (#317)")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only reports")
    parser.add_argument("--user-id", type=int, default=None,
                        help="restrict to one human owner")
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        rows = empty_imported_query(args.user_id).order_by(Node.id.asc()).all()
        print(f"empty imported nodes: {len(rows)} across "
              f"{len({n.human_owner_id for n in rows})} user(s)")
        if not rows:
            return 0
        deletable, held = partition(rows)
        print(f"  deletable (root, childless, unreferenced): {len(deletable)}")
        print(f"  per user: {dict(Counter(n.human_owner_id for n in deletable))}")
        for node_id, why in held.items():
            print(f"  HELD BACK node {node_id}: {why}")
        if not args.apply:
            print("dry run — nothing written; rerun with --apply")
            return 0

        done, refused = 0, []
        for node in deletable:
            result = soft_delete_node(node.id, node.human_owner_id,
                                      with_descendants=False)
            if result is None or not result.ids:
                # Vanished or unowned between the survey and now.
                refused.append(node.id)
                continue
            done += 1
        db.session.commit()
        print(f"soft-deleted {done} node(s)")
        if refused:
            print(f"  could not delete: {refused}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
