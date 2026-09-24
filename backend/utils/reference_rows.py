"""One row per tweet for a Read pick and the saved reference (#352).

A tweet a Read picks gets an ExternalItem so the pick has somewhere to
keep the user's read mark and verdict. When the user has not saved the
tweet, that row's source is READ_PICK_SOURCE and it is not a reference
(the references list, digest, embedding sweep and reference search skip
it). When the user then saves the tweet — the clipper, the X bookmark
sync, the JSON bookmark import, the Community Archive import — the
pick's row becomes the saved reference in place, instead of a second
row with marks of its own. And a pick of a tweet the user already saved
reuses that row. So one tweet has one row and one set of marks.
"""
from datetime import datetime

from backend.extensions import db


def find_tweet_row(user_id, tweet_id):
    """The user's row for *tweet_id* in any tweet source: a saved one
    first (the oldest, when an older import left two), else the Read
    pick's. None when the tweet has no row."""
    from backend.models import ExternalItem, TWEET_SOURCES
    rows = (ExternalItem.query
            .filter(ExternalItem.user_id == user_id,
                    ExternalItem.source.in_(TWEET_SOURCES),
                    ExternalItem.external_id == str(tweet_id))
            .order_by(ExternalItem.id.asc()).all())
    saved = [r for r in rows if r.is_saved]
    if saved:
        return saved[0]
    return rows[0] if rows else None


def pick_rows_by_tweet(user_id):
    """{tweet_id: row} of the user's Read picks they have not saved — the
    rows a save turns into references. A small set (up to 20 per read)."""
    from backend.models import ExternalItem, READ_PICK_SOURCE
    return {r.external_id: r for r in ExternalItem.query.filter_by(
        user_id=user_id, source=READ_PICK_SOURCE).all()}


def save_pick_row(item, source):
    """Make the Read pick's row *item* a saved reference of *source*.
    fetched_at moves, because the saved corpus changed: the nightly
    digest sees a newer item and rebuilds, and the embedding sweep, which
    skips picks, embeds it."""
    item.source = source
    item.fetched_at = datetime.utcnow()
    db.session.add(item)
    return item
