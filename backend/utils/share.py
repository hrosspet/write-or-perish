"""Shared helper for saving Share drafts from a proposal node (SHARE_V1).

The shareable text lives in the visible node content as fenced
``:::share`` blocks (see ``parse_share``; legacy ``### Share`` headings
still parse); it is only persisted to the ``ShareDraft`` table when the
user explicitly confirms — via a block's Save button
(``/api/share/save-proposal``) or the ``apply_share`` tool. Even then it is
saved as a PRIVATE draft: publication is a separate, deliberate action on
the Share page. Keeping the parse + persist logic here means both
confirmation paths behave identically.

Which node's blocks the Save button may save, and how a node's share
state follows an edit of its text, live here too (``may_save_shares_from``,
``sync_share_state_after_edit``).
"""
from collections import Counter

from backend.models import Draft, ShareDraft
from backend.extensions import db
from backend.utils.tool_meta import (
    get_tool_meta_entry, parse_share, share_owner_id, update_tool_meta,
)
from backend.utils.tool_record import OWNER_ONLY_KEY

SHARE_PENDING_LABEL = "share_pending"


def may_save_shares_from(node, user_id):
    """Whether *user_id* may save the ``:::share`` blocks of *node* with
    its Save button: the node is not deleted and is theirs — an entry
    they wrote, or an AI reply written for them (its human owner). The
    same owner the pages show the button to (isOwner on the web,
    ThreadModel.isOwner on the iPhone).

    No pending proposal is needed: the click is the user's consent, and
    what it makes is a private draft, as a share typed on the Share page
    is. The pending ``share_pending`` Draft the reply task leaves on an AI
    proposal gates the ``apply_share`` tool only (a spoken "yes, save
    that")."""
    if node is None or node.deleted_at is not None:
        return False
    return share_owner_id(node) == user_id


def saved_state_updates(node, entry, updates):
    """*updates* for the node's propose_share entry after a save, marked
    for the owner only when the entry holds nothing but the saved state:
    it is new (an entry, or an AI reply that proposed no share), or the
    node is not an AI reply. An AI proposal's own entry stays as the
    reply task wrote it, name and status for every viewer."""
    if entry is None or node.node_type != "llm":
        return {**updates, OWNER_ONLY_KEY: True}
    return updates


def saved_positions(entry, total):
    """The positions (0-based, content order) of a node's share blocks
    already saved, from its ``propose_share`` entry, within *total*
    blocks. A proposal saved whole before saves were kept per block
    (apply_status "completed", no ``saved_indexes``) counts as all."""
    entry = entry or {}
    indexes = entry.get("saved_indexes")
    if not indexes and entry.get("apply_status") == "completed":
        return set(range(total))
    return {i for i in (indexes or [])
            if isinstance(i, int) and 0 <= i < total}


def _block_key(block):
    # A block counts as the same block when its text and its type word
    # are both unchanged: that is what a save stored.
    return (block.get("content") or "", block.get("share_type") or "")


def sync_share_state_after_edit(node, old_content, new_content):
    """Keep *node*'s share state true to its text after its owner edited
    it (PUT /nodes/<id>; the caller commits).

    Saved blocks are tracked by position (``saved_indexes`` in the node's
    ``propose_share`` entry). After an edit a block counts as saved only
    when the same block (text and type word) was saved before the edit,
    wherever it now stands; a changed or new block gets its Save button,
    and a removed one is forgotten. The shares already saved are not
    touched: each holds its own copy of the text, so an edit never
    changes a draft or what is public.

    An AI proposal's pending Draft goes once no block is left to save
    (all removed, or all saved), so a spoken "yes, save that" cannot act
    on text that is gone."""
    # The saved state is marked for the owner only and so is not among the
    # names stored beside the record: a node with a record is read.
    pending = Draft.query.filter_by(
        parent_id=node.id, label=SHARE_PENDING_LABEL).all()
    if not node.tool_calls_meta_stored and not pending:
        return

    new_blocks = parse_share(new_content or "").get("shares") or []
    saved_now = []
    entry = get_tool_meta_entry(node, "propose_share")
    if entry is not None:
        saved_now = _saved_after_edit(entry, old_content, new_blocks)
        status = entry.get("apply_status")
        if new_blocks and len(saved_now) == len(new_blocks):
            status = "completed"
        elif saved_now:
            status = "partial"
        elif status in ("completed", "partial"):
            status = "pending_approval"
        if (saved_now != (entry.get("saved_indexes") or [])
                or status != entry.get("apply_status")):
            update_tool_meta(node, "propose_share", saved_state_updates(
                node, entry, {
                    "apply_status": status,
                    "saved_indexes": saved_now,
                }))

    if pending and len(saved_now) >= len(new_blocks):
        for draft in pending:
            db.session.delete(draft)


def _saved_after_edit(entry, old_content, new_blocks):
    """The positions in *new_blocks* of blocks saved before the edit: each
    block of the old text that was saved counts once, for the first block
    of the new text with the same text and type word."""
    old_blocks = parse_share(old_content or "").get("shares") or []
    left = Counter(_block_key(old_blocks[i])
                   for i in saved_positions(entry, len(old_blocks)))
    saved = []
    for i, block in enumerate(new_blocks):
        key = _block_key(block)
        if left[key] > 0:
            left[key] -= 1
            saved.append(i)
    return saved


def save_share_drafts_from_node(origin_node, user_id, indexes=None, skip=(),
                                content=None):
    """Parse the share proposal(s) out of *origin_node*'s content and persist
    ShareDraft rows with status "draft" (flushed, not committed — the caller
    commits).

    *indexes* selects which blocks to save, as 0-based positions in content
    order (None = all); *skip* positions are excluded either way — callers
    pass the already-saved set so a save-all after per-block saves can't
    duplicate. *content*: the node's text when the caller already read it
    (one decryption instead of two).

    Returns ``(saved, total, error)``: *saved* is a list of ``(index,
    ShareDraft)`` pairs, *total* the number of parseable blocks in the node.
    On failure *saved* is empty and *error* explains (no parseable share
    content, or nothing selected / index out of range).
    """
    if content is None:
        content = origin_node.get_content()
    parsed = parse_share(content or "")
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
