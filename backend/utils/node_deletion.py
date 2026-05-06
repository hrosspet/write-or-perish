"""Soft-delete utility: lock-and-walk subtree algorithm.

The algorithm relies on Postgres FK semantics to close Race A:
INSERT INTO node (parent_id = X, ...) acquires FOR KEY SHARE on the parent
row to validate the FK. FOR UPDATE on the same row conflicts with FOR KEY
SHARE, so while we hold a node's row-level lock, no concurrent INSERT can
proceed against that node as parent. The create-side endpoint takes its
own with_for_update() lock and re-checks deleted_at after acquiring it,
so once we commit our soft-delete, the create wakes up and returns 410.

Walk pattern: lock node first, *then* read its children under that lock.
This catches concurrent inserts that may have raced into the subtree
before our walk reached the parent. Sibling iteration order is id-asc for
deadlock-avoidance defense-in-depth (irrelevant in a strict tree, but
cheap insurance for any future bulk-delete API).
"""

from datetime import datetime
from typing import Optional, Tuple

from flask import jsonify

from backend.extensions import db
from backend.models import Node
from backend.utils.privacy import can_user_edit_node


class ParentDeletedError(ValueError):
    """Raised when an attempt is made to insert a child of a soft-deleted node.

    Subclasses ValueError so existing call sites that broadly catch
    ValueError continue to behave correctly; new code can catch this
    specifically to distinguish "parent gone" from validation errors.
    """


def assert_parent_alive(parent_id) -> Optional[Tuple[object, int]]:
    """Race A guard: lock the parent row, then verify it isn't soft-deleted.

    Use at the top of any route that creates a child node. Acquiring the
    row lock here serializes against the soft-delete endpoint's locking
    walk; if the parent has `deleted_at` set by the time we hold the lock,
    we return a 410 response and the caller aborts.

    Args:
        parent_id: int / str / None. If None, no check (root-level node).

    Returns:
        None if it's safe to proceed (no parent, or parent is alive).
        (response, status) tuple if the caller should return immediately.
    """
    if parent_id is None or parent_id == "":
        return None
    try:
        pid = int(parent_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parent_id"}), 400

    parent = Node.query.with_for_update().get(pid)
    if parent is None:
        return jsonify({"error": "Parent node not found"}), 404
    if parent.deleted_at is not None:
        return jsonify({"error": "Parent node has been deleted"}), 410
    return None


def soft_delete_node(node_id: int, user_id: int, *,
                     with_descendants: bool) -> Optional[int]:
    """Soft-delete `node_id` (and editable descendants if requested).

    Returns the count of nodes flagged with deleted_at, or None if the
    target node does not exist or the user lacks edit permission on it.

    The caller is responsible for the surrounding 403 / 404 / commit/rollback
    handling — this helper just sets in-session state and returns the count.
    """
    now = datetime.utcnow()

    root = Node.query.with_for_update().get(node_id)
    if root is None:
        return None
    if not can_user_edit_node(root, user_id):
        return None

    visited: set[int] = set()
    flagged = 0

    # Process the root first so we can clear pinned_at on it specifically.
    visited.add(root.id)
    if root.deleted_at is None:
        root.deleted_at = now
        root.pinned_at = None
        flagged += 1

    if not with_descendants:
        return flagged

    # BFS-style queue, sorted ascending each iteration for deterministic
    # global lock order across overlapping subtree-deletes.
    to_visit: list[int] = []
    child_rows = (
        Node.query
        .filter_by(parent_id=root.id)
        .with_entities(Node.id)
        .order_by(Node.id.asc())
        .all()
    )
    to_visit.extend(cid for (cid,) in child_rows)

    while to_visit:
        to_visit.sort()
        nid = to_visit.pop(0)
        if nid in visited:
            continue
        visited.add(nid)

        locked = Node.query.with_for_update().get(nid)
        if locked is None:
            # Already purged by cleanup, or never existed (e.g. race).
            continue

        # Other user's node: leave it alive (forces tombstone above), but
        # KEEP WALKING into its descendants — the current user may have
        # replies nested under it. The dialog promises "delete this node
        # and all my replies", which means all my replies in this thread,
        # not "all my replies until I hit someone else's". The lock on
        # this node also prevents new INSERTs under it during our walk.
        editable = can_user_edit_node(locked, user_id)
        if editable and locked.deleted_at is None:
            locked.deleted_at = now
            flagged += 1

        # Re-query children under the lock — catches concurrent inserts that
        # may have raced in before we acquired this node's lock.
        child_rows = (
            Node.query
            .filter_by(parent_id=locked.id)
            .with_entities(Node.id)
            .order_by(Node.id.asc())
            .all()
        )
        to_visit.extend(cid for (cid,) in child_rows if cid not in visited)

    return flagged
