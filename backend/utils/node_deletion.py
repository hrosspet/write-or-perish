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


def orphaned_system_prompt_id(node, user_id: int, *,
                              with_descendants: bool) -> Optional[int]:
    """Return the thread root's id when soft-deleting `node` would leave
    the thread with nothing alive but its system prompt; else None.

    A text/voice session is a system-prompt root with the user's entries
    underneath. Deleting the last entry keeps the root alive, so the Log
    would list a card whose title and preview are the prompt text. The
    delete dialog uses this to offer deleting the prompt as well.

    Mirrors soft_delete_node's selection without locking anything: the
    node itself, plus (with_descendants) every descendant the user can
    edit. Other users' replies stay alive and therefore count as
    remaining content.
    """
    if node.parent_id is None or node.deleted_at is not None:
        return None

    # Up to the root.
    up = db.session.query(
        Node.id.label("id"), Node.parent_id.label("parent_id"),
    ).filter(Node.id == node.id).cte(name="up", recursive=True)
    parent = db.aliased(Node, flat=True)
    up = up.union_all(
        db.session.query(parent.id, parent.parent_id)
        .join(up, parent.id == up.c.parent_id)
    )
    root_id = db.session.query(up.c.id).filter(up.c.parent_id.is_(None)).scalar()
    if root_id is None:
        return None
    root = Node.query.get(root_id)
    if root is None or root.deleted_at is not None or not root.is_system_prompt:
        return None

    # Down from the root: every descendant, alive or not, with what the
    # editability check needs.
    down = db.session.query(
        Node.id.label("id"), Node.parent_id.label("parent_id"),
        Node.user_id.label("user_id"), Node.human_owner_id.label("human_owner_id"),
        Node.deleted_at.label("deleted_at"),
    ).filter(Node.parent_id == root_id).cte(name="down", recursive=True)
    child = db.aliased(Node, flat=True)
    down = down.union_all(
        db.session.query(
            child.id, child.parent_id, child.user_id, child.human_owner_id,
            child.deleted_at,
        ).join(down, child.parent_id == down.c.id)
    )
    rows = db.session.query(
        down.c.id, down.c.parent_id, down.c.user_id, down.c.human_owner_id,
        down.c.deleted_at,
    ).all()

    flagged = {node.id}
    if with_descendants:
        children_of: dict = {}
        for r in rows:
            children_of.setdefault(r.parent_id, []).append(r)
        queue = list(children_of.get(node.id, []))
        while queue:
            r = queue.pop()
            if r.user_id == user_id or r.human_owner_id == user_id:
                flagged.add(r.id)
            queue.extend(children_of.get(r.id, []))

    remaining = [r.id for r in rows if r.deleted_at is None and r.id not in flagged]
    return None if remaining else root_id


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
