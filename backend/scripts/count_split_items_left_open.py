#!/usr/bin/env python3
"""
Count imported items that a re-import left partly open to AI (#638).
Read-only; reads metadata only, never content.

Before #638 a re-import applied its privacy and AI usage to the last
entry of a split item only (a Word or PDF document saved as one entry
per chapter, a file or message above the per-entry cap saved as parts).
This counts the items whose last entry, the one holding the dedup key,
is set to None while an entry before it in the same item still allows
AI.

The entries of an item are judged from metadata, more loosely than a
re-import judges them (a re-import changes only what it can prove
against the re-imported text): walking up from the keyed entry, a
parent counts when it is alive, has no key and no split_item token, has
the same author, owner and type, an origin equal to the keyed entry's
or none (imports before 2026-08-27 recorded no origin on the parts
before the last), and is dated exactly 1 ms earlier (the part before it
of the same text) or, in a Word or PDF document, up to 1 s earlier (the
chapter before). So the count is an upper bound, to decide on.

Prints three numbers: items, entries, accounts.

Usage (on prod, from repo root):
    python backend/scripts/count_split_items_left_open.py
"""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from sqlalchemy import or_  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import Node  # noqa: E402
from backend.utils.node_split import PART_STEP  # noqa: E402
from backend.utils.privacy import AI_ALLOWED  # noqa: E402

SPLIT_ORIGINS = ("markdown", "docx", "pdf", "chatgpt", "claude")
CHAPTER_FORMATS = ("docx", "pdf")
CHAPTER_STEP = timedelta(seconds=1)
_COLS = (Node.id, Node.parent_id, Node.source_key, Node.split_item,
         Node.user_id, Node.human_owner_id, Node.node_type, Node.origin,
         Node.created_at, Node.deleted_at, Node.ai_usage)


def _looks_like_part(parent, cur, tip):
    if (parent.deleted_at is not None or parent.source_key is not None
            or parent.split_item is not None
            or parent.created_at is None or cur.created_at is None
            or (parent.user_id, parent.human_owner_id, parent.node_type)
            != (tip.user_id, tip.human_owner_id, tip.node_type)
            or parent.origin not in (tip.origin, None)):
        return False
    gap = cur.created_at - parent.created_at
    return gap == PART_STEP or (tip.origin in CHAPTER_FORMATS
                                and timedelta(0) < gap <= CHAPTER_STEP)


def count():
    """(items, entries, accounts) left partly open; in an app context."""
    tips = db.session.query(*_COLS).filter(
        Node.source_key.isnot(None), Node.deleted_at.is_(None),
        Node.split_item.is_(None), Node.ai_usage == "none",
        or_(Node.origin.in_(SPLIT_ORIGINS), Node.origin.is_(None)),
    ).all()
    items, entries, owners = set(), set(), set()
    walking = [(t, t) for t in tips]
    while walking:
        ids = list({cur.parent_id for _, cur in walking if cur.parent_id})
        parents = {}
        for start in range(0, len(ids), 1000):
            for r in db.session.query(*_COLS).filter(
                    Node.id.in_(ids[start:start + 1000])):
                parents[r.id] = r
        step = []
        for tip, cur in walking:
            parent = parents.get(cur.parent_id)
            if parent is None or not _looks_like_part(parent, cur, tip):
                continue
            if parent.ai_usage in AI_ALLOWED:
                items.add(tip.id)
                entries.add(parent.id)
                owners.add(tip.human_owner_id)
            step.append((tip, parent))
        walking = step
    return len(items), len(entries), len(owners)


def main():
    from backend.app import create_app
    app = create_app()
    with app.app_context():
        items, entries, accounts = count()
        print(f"{items} items, {entries} entries, {accounts} accounts")


if __name__ == "__main__":
    main()
