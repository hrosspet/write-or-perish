"""Which proposals a user may act on.

A proposal is an AI reply that offers its user something to accept: todo
changes, an issue to file, feedback to send, posts to save as shares. The
completion task marks a pending one (llm_completion._auto_create_drafts)
with a Draft on the reply, labelled by kind (PROPOSAL_TOOLS), and writes
the matching propose_* entry into the reply's tool_calls_meta.

Accepting one, by the card's button, by the voice and text tools, or by a
todo merge that is started, applied again or retried, acts only on the
user's own live proposal (is_own_live_proposal).
"""
import json

from backend.models import Draft
from backend.utils.privacy import can_user_access_node

# Pending-draft label -> the reply's tool_calls_meta entry for that kind.
PROPOSAL_TOOLS = {
    "todo_pending": "propose_todo",
    "github_issue_pending": "propose_github_issue",
    "feedback_pending": "propose_feedback",
    "share_pending": "propose_share",
}


def node_is_users(node, user_id):
    """True when *node* belongs to *user_id*: they wrote it, or (an AI
    reply) asked for it."""
    return (node.human_owner_id or node.user_id) == user_id


def _has_tool_entry(node, tool_name):
    if not node.tool_calls_meta:
        return False
    try:
        meta = json.loads(node.tool_calls_meta)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(meta, list):
        return False
    return any(isinstance(e, dict) and e.get("name") == tool_name
               for e in meta)


def is_own_live_proposal(node, user_id, label):
    """True when *node* is a proposal of kind *label* that *user_id* may
    accept: an AI reply they asked for, not deleted, visible to them, that
    carries the propose_* entry of that kind."""
    return (node is not None
            and node.node_type == "llm"
            and node_is_users(node, user_id)
            and node.deleted_at is None
            and can_user_access_node(node, user_id)
            and _has_tool_entry(node, PROPOSAL_TOOLS[label]))


def find_own_pending_proposal(start_node, user_id, label):
    """The user's pending proposal of kind *label* nearest *start_node*,
    walking up from the node itself: ``(draft, node)``, or ``(None, None)``.

    The walk stops at the first pending draft it meets. That draft counts
    only when its node is the user's own live proposal; an older one
    further up is never taken in its place."""
    current = start_node
    visited = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        draft = Draft.query.filter_by(
            user_id=user_id, parent_id=current.id, label=label,
        ).first()
        if draft is not None:
            if is_own_live_proposal(current, user_id, label):
                return draft, current
            return None, None
        current = current.parent
    return None, None
