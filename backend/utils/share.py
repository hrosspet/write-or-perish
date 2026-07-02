"""Shared helper for saving a Share draft from a proposal node (SHARE_V1).

The shareable text the AI proposes lives in the visible node content under a
`### Share` heading (see ``parse_share``); it is only persisted to the
``ShareDraft`` table when the user explicitly confirms — via the Save button
(``/api/share/save-proposal``) or the ``apply_share`` tool. Even then it is
saved as a PRIVATE draft: publication is a separate, deliberate action on the
Share page. Keeping the parse + persist logic here means both confirmation
paths behave identically.
"""
from backend.models import ShareDraft
from backend.extensions import db
from backend.utils.tool_meta import parse_share


def save_share_draft_from_node(origin_node, user_id):
    """Parse the share proposal out of *origin_node*'s content and persist a
    ShareDraft row with status "draft" (flushed, not committed — the caller
    commits).

    Returns ``(share_draft, None)`` on success or ``(None, error_message)``
    when the proposal has no parseable share content.
    """
    parsed = parse_share(origin_node.get_content() or "")
    content = (parsed.get("content") or "").strip()
    if not content:
        return None, "Could not parse share from proposal"
    share_type = parsed.get("share_type") or "other"
    if share_type not in ShareDraft.SHARE_TYPES:
        share_type = "other"
    share = ShareDraft(user_id=user_id, share_type=share_type,
                       status="draft", source_node_id=origin_node.id)
    share.set_content(content)
    db.session.add(share)
    db.session.flush()
    return share, None
