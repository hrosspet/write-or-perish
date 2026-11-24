"""How many public entries one account adds to other people's threads in
a day.

A public reply under someone else's public piece is shown on that
person's public page. An account's own threads, its private writing and
its AI replies (created private) have no such count.

Counted from the Node table, so nothing new is stored: the account's
public entries created in the last 24 hours whose thread's first entry
belongs to someone else. An entry longer than the per-entry cap is
stored as several entries and counts as that many; the count is asked
once per request, before anything is written, so one long entry is
never cut part-way.
"""
from datetime import datetime, timedelta

from flask import jsonify

# INTRODUCED CONSTANT: public entries one account may add to other
# people's threads in 24 hours. Real use is a handful of replies a day;
# a long day of discussion under several pieces, with typed follow-ups,
# is a few dozen. 200 leaves that several times over and still bounds
# what one account can put on other people's public pages in a day.
PUBLIC_REPLIES_PER_DAY = 200

TOO_MANY_PUBLIC_REPLIES_CODE = "too_many_public_replies"
TOO_MANY_PUBLIC_REPLIES_MESSAGE = (
    "You've reached today's limit of public replies under other "
    "people's posts. Try again tomorrow.")


def _owner_of_thread(node_id):
    """The account that owns the first entry of *node_id*'s thread."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.thread_tree import thread_root_of
    root_id = thread_root_of([node_id]).get(node_id)
    if root_id is None:
        return None
    row = db.session.query(Node.human_owner_id, Node.user_id).filter(
        Node.id == root_id).first()
    return (row[0] or row[1]) if row else None


def public_replies_today(user_id, now=None):
    """The public entries *user_id* created in the last 24 hours in
    threads whose first entry belongs to someone else. Ids only."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.thread_tree import thread_root_of
    since = (now or datetime.utcnow()) - timedelta(hours=24)
    ids = [nid for (nid,) in db.session.query(Node.id).filter(
        (Node.human_owner_id == user_id) | (Node.user_id == user_id),
        Node.privacy_level == "public",
        Node.parent_id.isnot(None),
        Node.deleted_at.is_(None),
        Node.created_at >= since,
    ).all()]
    if not ids:
        return 0
    roots = thread_root_of(ids)
    root_ids = set(roots.values())
    own_roots = {rid for (rid,) in db.session.query(Node.id).filter(
        Node.id.in_(root_ids),
        (Node.human_owner_id == user_id) | (Node.user_id == user_id),
    ).all()}
    return sum(1 for nid in ids
               if nid in roots and roots[nid] not in own_roots)


def public_reply_refusal(user_id, parent_id, privacy_level):
    """A 429 (``too_many_public_replies``) when *user_id* is about to add
    a public entry under *parent_id* in someone else's thread and has
    added PUBLIC_REPLIES_PER_DAY in the last 24 hours; else None. Asked
    before anything is written. No parent, a private entry and the
    account's own threads are never refused."""
    if privacy_level != "public" or parent_id in (None, ""):
        return None
    try:
        parent_id = int(parent_id)
    except (TypeError, ValueError):
        return None
    owner = _owner_of_thread(parent_id)
    if owner is None or owner == user_id:
        return None
    if public_replies_today(user_id) < PUBLIC_REPLIES_PER_DAY:
        return None
    return jsonify({"error": TOO_MANY_PUBLIC_REPLIES_MESSAGE,
                    "code": TOO_MANY_PUBLIC_REPLIES_CODE}), 429
