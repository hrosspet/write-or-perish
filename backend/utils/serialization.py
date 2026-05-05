"""Centralized node serialization helpers.

The functions here are the single decision point for "should this node render
as a tombstone, an inaccessible stub, or as full content?" — implementing the
§3 rules from the soft-delete plan exactly once.

Routes that emit node JSON should call serialize_node_status() first; if it
returns a dict, the route uses that as the payload. If it returns None, the
node is alive and accessible, and the route serializes the full content as
usual (since the JSON shape varies per endpoint).
"""

from typing import Optional

from backend.utils.privacy import (
    can_user_access_node,
    can_user_view_tombstone,
)


def serialize_node_status(node, viewer_id: int) -> Optional[dict]:
    """Decide tombstone / inaccessible / full-content for a node + viewer.

    Returns:
        - dict with `"deleted": True` and minimal metadata when the node is
          soft-deleted and the viewer would have had access pre-deletion.
        - dict with `"inaccessible": True` when the node is privacy-blocked
          for the viewer, or soft-deleted and the viewer would NOT have had
          pre-deletion access (avoids leaking metadata).
        - None when the node is alive and accessible — the caller should emit
          its full per-route payload as normal.

    The "should we even include this in the response?" decision (e.g. for
    tree-walkers: omit tombstones with no live accessible descendants) is the
    call site's responsibility, not this helper's. Breadcrumb and
    inline-quote paths always include.
    """
    if getattr(node, "deleted_at", None) is not None:
        if can_user_view_tombstone(node, viewer_id):
            return {
                "id": node.id,
                "deleted": True,
                "deleted_at": node.deleted_at.isoformat(),
                "username": node.user.username if node.user else None,
                "node_type": node.node_type,
                "created_at": node.created_at.isoformat(),
            }
        return {"id": node.id, "inaccessible": True}

    if not can_user_access_node(node, viewer_id):
        return {"id": node.id, "inaccessible": True}

    return None
