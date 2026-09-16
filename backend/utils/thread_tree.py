"""Parent-chain walks over the node tree, shared by the Log, deletion
and the public page cache so there is one recursive query to get right.
"""

from backend.extensions import db
from backend.models import Node


def thread_root_of(node_ids):
    """Map each id in `node_ids` to the id of its thread root (the
    topmost ancestor) with one recursive query.

    A root maps to itself. Ids that don't exist are absent from the
    result. Privacy-blind: callers decide what the root's identity means
    for the viewer.
    """
    ids = list({int(i) for i in node_ids})
    if not ids:
        return {}
    up = db.session.query(
        Node.id.label("start_id"),
        Node.id.label("id"),
        Node.parent_id.label("parent_id"),
    ).filter(Node.id.in_(ids)).cte(name="thread_root_walk", recursive=True)
    parent = db.aliased(Node, flat=True)
    up = up.union_all(
        db.session.query(up.c.start_id, parent.id, parent.parent_id)
        .join(up, parent.id == up.c.parent_id)
    )
    return dict(
        db.session.query(up.c.start_id, up.c.id)
        .filter(up.c.parent_id.is_(None))
        .all()
    )


def subtree_rows(root_id):
    """Every descendant of `root_id` (alive or not), as rows of
    (id, parent_id, user_id, human_owner_id, deleted_at). One recursive
    query; the root itself is not included."""
    down = db.session.query(
        Node.id.label("id"), Node.parent_id.label("parent_id"),
        Node.user_id.label("user_id"), Node.human_owner_id.label("human_owner_id"),
        Node.deleted_at.label("deleted_at"),
    ).filter(Node.parent_id == root_id).cte(name="subtree_walk", recursive=True)
    child = db.aliased(Node, flat=True)
    down = down.union_all(
        db.session.query(
            child.id, child.parent_id, child.user_id, child.human_owner_id,
            child.deleted_at,
        ).join(down, child.parent_id == down.c.id)
    )
    return db.session.query(
        down.c.id, down.c.parent_id, down.c.user_id, down.c.human_owner_id,
        down.c.deleted_at,
    ).all()
