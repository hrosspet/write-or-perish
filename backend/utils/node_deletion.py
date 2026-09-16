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
from typing import NamedTuple, Optional, Tuple

from flask import jsonify

from backend.extensions import db
from backend.models import Node
from backend.utils.privacy import can_user_edit_node
from backend.utils.thread_tree import subtree_rows, thread_root_of


def lock_node(node_id: int):
    """SELECT ... FOR UPDATE on one node row, returning the row as it is
    *now*. A plain `with_for_update().get()` hands back the copy already
    in the identity map without touching its attributes, so a check on
    `deleted_at` after the lock would read whatever an earlier unlocked
    query saw. `populate_existing` overwrites that copy with the locked
    read (pending changes are flushed first, as for any query), which is
    the whole point of checking under the lock."""
    return db.session.get(
        Node, node_id, with_for_update=True, populate_existing=True,
    )


def prompt_root_of(node, user_id: int, *, lock: bool = False):
    """The alive system-prompt root of `node`'s thread when the user may
    delete it; else None.

    With `lock`, the root row is taken FOR UPDATE before anything else in
    the delete transaction: an INSERT under the root needs FOR KEY SHARE
    on it, so no new entry can land in the session between the
    "nothing left" check and the root's own soft-delete. The lock is
    only taken once the unlocked row has passed the checks — a reply in
    someone else's session must not block that user's inserts for the
    length of the delete walk — and the checks run again on the locked
    row, which is re-read from the database.
    """
    if node.parent_id is None or node.deleted_at is not None:
        return None
    root_id = thread_root_of([node.id]).get(node.id)
    if root_id is None or root_id == node.id:
        return None

    def deletable(root):
        return (root is not None and root.deleted_at is None
                and root.is_system_prompt
                and can_user_edit_node(root, user_id))

    root = Node.query.get(root_id)
    if not deletable(root):
        return None
    if lock:
        root = lock_node(root_id)
        if not deletable(root):
            return None
    return root


def subtree_has_alive_nodes(root_id: int, viewer_id: int) -> bool:
    """True when anything under `root_id` (not the root itself) is
    alive and reachable for `viewer_id` — the definition of remaining
    content shared with the Log (see thread_tree). Pending soft-deletes
    in the session are flushed first, so this reads the state a commit
    would produce."""
    db.session.flush()
    return any(r.deleted_at is None for r in subtree_rows(root_id, viewer_id))


def soft_delete_session_if_empty(root, user_id: int) -> bool:
    """The "delete the system prompt too" half of a DELETE: once the
    target is flagged in the session, tombstone the (locked) prompt
    `root` when nothing the user can see is left alive under it. Returns
    whether it did. Entries that landed in the meantime keep the root
    alive."""
    if subtree_has_alive_nodes(root.id, user_id):
        return False
    root.deleted_at = datetime.utcnow()
    root.pinned_at = None
    return True


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
    edit. What counts as remaining is what the Log would show: alive
    nodes the user can reach (another user's public reply stays alive
    and counts; their private reply is invisible to this user and does
    not). A root the user may not delete (a reply pinned in someone
    else's session) is never offered.
    """
    root = prompt_root_of(node, user_id)
    if root is None:
        return None
    rows = subtree_rows(root.id, user_id)

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
    return None if remaining else root.id


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

    parent = lock_node(pid)
    if parent is None:
        return jsonify({"error": "Parent node not found"}), 404
    if parent.deleted_at is not None:
        return jsonify({"error": "Parent node has been deleted"}), 410
    return None


class Deleted(NamedTuple):
    """What a soft-delete flagged: which nodes, and which of them were
    pinned (each pinned node is a Log card of its own, so the client
    drops those cards along with the target's)."""
    ids: list
    pinned_ids: list

    @property
    def count(self) -> int:
        return len(self.ids)


def soft_delete_node(node_id: int, user_id: int, *,
                     with_descendants: bool) -> Optional[Deleted]:
    """Soft-delete `node_id` (and editable descendants if requested).

    Returns what was flagged with deleted_at, or None if the target node
    does not exist or the user lacks edit permission on it.

    The caller is responsible for the surrounding 403 / 404 / commit/rollback
    handling — this helper just sets in-session state and returns the result.
    """
    now = datetime.utcnow()

    root = lock_node(node_id)
    if root is None:
        return None
    if not can_user_edit_node(root, user_id):
        return None

    visited: set[int] = set()
    flagged: list[int] = []
    pinned_ids: list[int] = []

    # Process the root first so we can clear pinned_at on it specifically.
    visited.add(root.id)
    if root.deleted_at is None:
        if root.pinned_at is not None:
            pinned_ids.append(root.id)
        root.deleted_at = now
        root.pinned_at = None
        flagged.append(root.id)

    if not with_descendants:
        return Deleted(flagged, pinned_ids)

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

        locked = lock_node(nid)
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
            if locked.pinned_at is not None:
                pinned_ids.append(locked.id)
            locked.deleted_at = now
            flagged.append(locked.id)

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

    return Deleted(flagged, pinned_ids)
