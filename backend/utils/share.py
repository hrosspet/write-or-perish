"""Shared helper for saving Share drafts from a proposal node (SHARE_V1).

The shareable text lives in the visible node content as fenced
``:::share`` blocks (see ``parse_share``; legacy ``### Share`` headings
still parse); it is only persisted to the ``ShareDraft`` table when the
user explicitly confirms — via a block's Save button
(``/api/share/save-proposal``) or the ``apply_share`` tool. Even then it is
saved as a PRIVATE draft: publication is a separate, deliberate action on
the Share page. Keeping the parse + persist logic here means both
confirmation paths behave identically.
"""
from backend.models import ShareDraft
from backend.extensions import db
from backend.utils.tool_meta import parse_share


def save_share_drafts_from_node(origin_node, user_id, indexes=None, skip=()):
    """Parse the share proposal(s) out of *origin_node*'s content and persist
    ShareDraft rows with status "draft" (flushed, not committed — the caller
    commits).

    *indexes* selects which blocks to save, as 0-based positions in content
    order (None = all); *skip* positions are excluded either way — callers
    pass the already-saved set so a save-all after per-block saves can't
    duplicate.

    Returns ``(saved, total, error)``: *saved* is a list of ``(index,
    ShareDraft)`` pairs, *total* the number of parseable blocks in the node.
    On failure *saved* is empty and *error* explains (no parseable share
    content, or nothing selected / index out of range).
    """
    parsed = parse_share(origin_node.get_content() or "")
    items = parsed.get("shares") or []
    total = len(items)
    if total == 0:
        return [], 0, "Could not parse share from proposal"
    if indexes is None:
        indexes = range(total)
    saved = []
    for i in indexes:
        if not (0 <= i < total) or i in skip:
            continue
        item = items[i]
        share_type = item.get("share_type") or "other"
        if share_type not in ShareDraft.SHARE_TYPES:
            share_type = "other"
        share = ShareDraft(user_id=user_id, share_type=share_type,
                           status="draft", source_node_id=origin_node.id)
        share.set_content(item["content"])
        db.session.add(share)
        saved.append((i, share))
    if not saved:
        return [], total, "No share block to save"
    db.session.flush()
    return saved, total, None
