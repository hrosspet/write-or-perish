"""Parent-chain and subtree walks over the node tree, shared by the Log,
deletion and the public page cache so there is one recursive query to
get right.

"Remaining content" under a root has one definition here: alive nodes
reachable from the root through nodes the viewer can access. The Log's
card fallback, the delete dialog's "only the prompt is left" check and
the server-side re-check all walk with `subtree_walk`, so they agree on
which replies count (another user's private reply, invisible to the
viewer, does not).
"""

from sqlalchemy import func

from backend.extensions import db
from backend.models import Node
from backend.utils.privacy import accessible_nodes_filter_ignoring_deleted

# A parent chain longer than this is treated as a cycle and the walk
# stops (a root is then never found for that start id). The FK tree has
# no cycles by construction; this only bounds a corrupt row's damage,
# where an unbounded recursive CTE would spin until the connection is
# killed. Real threads are hundreds of levels deep at most.
MAX_THREAD_DEPTH = 100_000


def thread_root_of(node_ids):
    """Map each id in `node_ids` to the id of its thread root (the
    topmost ancestor) with one recursive query.

    A root maps to itself. Ids that don't exist, or whose chain exceeds
    MAX_THREAD_DEPTH, are absent from the result. Privacy-blind: callers
    decide what the root's identity means for the viewer.
    """
    ids = list({int(i) for i in node_ids})
    if not ids:
        return {}
    up = db.session.query(
        Node.id.label("start_id"),
        Node.id.label("id"),
        Node.parent_id.label("parent_id"),
        db.literal(0).label("depth"),
    ).filter(Node.id.in_(ids)).cte(name="thread_root_walk", recursive=True)
    parent = db.aliased(Node, flat=True)
    up = up.union_all(
        db.session.query(
            up.c.start_id, parent.id, parent.parent_id,
            (up.c.depth + 1).label("depth"),
        )
        .join(up, parent.id == up.c.parent_id)
        .filter(up.c.depth < MAX_THREAD_DEPTH)
    )
    return dict(
        db.session.query(up.c.start_id, up.c.id)
        .filter(up.c.parent_id.is_(None))
        .all()
    )


def subtree_walk(root_ids, viewer_id=None):
    """A recursive CTE over the subtrees of `root_ids`: every root at
    depth 0, then its descendants, with the columns the callers rank or
    count by (id, parent_id, root_id, depth, user_id, human_owner_id,
    deleted_at, created_at, updated_at).

    With `viewer_id`, the walk only steps through nodes that user can
    access — it does not reach an alive node behind someone else's
    private reply, which is exactly what the viewer cannot see either.
    Tombstones are walked through in both cases: the *caller* filters on
    `deleted_at` to tell alive nodes from deleted ones, so an alive
    grandchild under a deleted entry is still found.
    """
    anchor = db.session.query(
        Node.id.label("id"),
        Node.parent_id.label("parent_id"),
        Node.id.label("root_id"),
        db.literal(0).label("depth"),
        Node.user_id.label("user_id"),
        Node.human_owner_id.label("human_owner_id"),
        Node.deleted_at.label("deleted_at"),
        Node.created_at.label("created_at"),
        Node.updated_at.label("updated_at"),
    ).filter(Node.id.in_(list(root_ids))).cte(name="subtree_walk", recursive=True)
    child = db.aliased(Node, flat=True)
    recursive = db.session.query(
        child.id, child.parent_id, anchor.c.root_id,
        (anchor.c.depth + 1).label("depth"),
        child.user_id, child.human_owner_id, child.deleted_at,
        child.created_at, child.updated_at,
    ).join(anchor, child.parent_id == anchor.c.id)
    if viewer_id is not None:
        recursive = recursive.filter(
            accessible_nodes_filter_ignoring_deleted(child, viewer_id),
        )
    return anchor.union_all(recursive)


def subtree_rows(root_id, viewer_id=None):
    """Every descendant of `root_id` (alive or not), as rows of
    (id, parent_id, user_id, human_owner_id, deleted_at). One recursive
    query; the root itself is not included. See `subtree_walk` for what
    `viewer_id` restricts."""
    walk = subtree_walk([root_id], viewer_id)
    return db.session.query(
        walk.c.id, walk.c.parent_id, walk.c.user_id, walk.c.human_owner_id,
        walk.c.deleted_at,
    ).filter(walk.c.depth > 0).all()


def alive_child_counts(parent_ids):
    """{parent_id: number of alive direct children} for the given ids
    (ids with no alive child are absent)."""
    ids = list({int(i) for i in parent_ids})
    if not ids:
        return {}
    return dict(
        db.session.query(Node.parent_id, func.count(Node.id))
        .filter(Node.parent_id.in_(ids), Node.deleted_at.is_(None))
        .group_by(Node.parent_id).all()
    )
