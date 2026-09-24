"""Bring existing Read picks into the #352 shape. One-off, after deploy.

Before #352 every tweet a Read picked became a saved reference
(ExternalItem, source 'community_archive'), and saving that tweet again
(the clipper, the X bookmark sync) made a second row with its own marks.
This script, in order:

1. Relabels the archive rows that exist only because a Read picked them
   to READ_PICK_SOURCE, so they leave the references list, the digest and
   search. A row counts as pick-made when it was written together with
   its first pick: fetched_at within PICK_ROW_WINDOW of that pick's
   created_at (save_feed_picks writes both in one transaction; a tweet
   imported through the Community Archive card was fetched earlier and
   keeps its source).
2. Records the read marks and verdicts every tweet row already carries
   as actions outside any reply (reference_log), so the recommendation
   record starts from what the user had done.
3. Merges each tweet that has both a pick's row and a saved copy into
   the pick's row, which the Read replies' {quote_ext:ID} markers and the
   FeedPick rows point at. The kept row takes the saved copy's source
   (so it is a saved reference again), the fuller text (the user's own
   edit wins), the earliest read mark, the latest verdict, the summed
   surfacing count, and the copy's logged actions and speech; replies
   that quoted the copy get their {quote_ext:ID} marker rewritten. Those
   replies are found among the owner's LLM replies written while the
   copy was being quoted (fetched_at .. last_surfaced_at), and only for a
   copy that was ever quoted, so few nodes are decrypted.
4. Stamps decided_at (the read's submit) and the prior read mark and
   verdict on picks from before these columns.
5. Drops the embeddings of READ_PICK_SOURCE rows (not references, not
   searched; the sweep no longer embeds them).

    cd /path/to/write-or-perish
    python backend/scripts/migrate_read_picks.py            # dry run
    python backend/scripts/migrate_read_picks.py --apply
    python backend/scripts/migrate_read_picks.py --user-id 6 --apply

Idempotent: a second run finds nothing to do. Prints metadata only.
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.getcwd())

from backend import create_app, db  # noqa: E402
from backend.models import (  # noqa: E402
    ExternalItem, ExternalItemEmbedding, FeedPick, FeedRender, Node,
    READ_PICK_SOURCE, ReferenceAction, TTSChunk, TWEET_SOURCES,
)
from backend.utils import reference_log  # noqa: E402

# INTRODUCED HEURISTIC: a pick-made row and its first FeedPick are
# written in one transaction, milliseconds apart; an archive import of
# the same tweet happens on its own, earlier. A minute separates the two
# with room to spare.
PICK_ROW_WINDOW = timedelta(seconds=60)


def _scoped(query, model, user_id):
    return query.filter(model.user_id == user_id) if user_id else query


def relabel_pick_rows(user_id, apply):
    """Step 1: archive rows written by save_feed_picks → READ_PICK_SOURCE."""
    first_pick = dict(
        db.session.query(FeedPick.external_item_id,
                         db.func.min(FeedPick.created_at))
        .filter(FeedPick.kind == reference_log.KIND_READ)
        .group_by(FeedPick.external_item_id).all())
    rows = _scoped(ExternalItem.query.filter(
        ExternalItem.source == "community_archive",
        ExternalItem.id.in_(list(first_pick) or [0])), ExternalItem,
        user_id).all()
    relabel, kept = [], []
    for row in rows:
        picked_at = first_pick[row.id]
        made_with_pick = (row.fetched_at is not None and picked_at is not None
                          and abs(row.fetched_at - picked_at)
                          <= PICK_ROW_WINDOW)
        (relabel if made_with_pick else kept).append(row)
    for row in relabel:
        if apply:
            row.source = READ_PICK_SOURCE
    print(f"1. relabel: {len(relabel)} pick-made archive rows → "
          f"{READ_PICK_SOURCE}; {len(kept)} picked archive rows kept as "
          f"imports (fetched before their first pick)")
    return relabel


def carry_over_marks(user_id, apply):
    """Step 2: marks already on tweet rows → actions outside any reply."""
    has_actions = {r[0] for r in db.session.query(
        ReferenceAction.item_id).distinct().all()}
    rows = _scoped(ExternalItem.query.filter(
        db.or_(ExternalItem.read_at.isnot(None),
               ExternalItem.feedback.isnot(None))), ExternalItem,
        user_id).all()
    todo = [r for r in rows if r.id not in has_actions]
    if apply:
        for row in todo:
            reference_log._carry_over(row)
    print(f"2. marks: {len(todo)} references with a read mark or verdict "
          f"recorded as actions outside any reply")


# The source a merged row keeps, strongest save first: an X bookmark or
# like is what the sync dedupes against, so the row must stay one, or
# the next sync inserts the tweet again.
SOURCE_PRIORITY = ("twitter_bookmark", "twitter_like", "community_archive",
                   READ_PICK_SOURCE)


def _saved_copy_pairs(user_id):
    """([(pick_row, [saved copies])] for tweets that have both, number
    of duplicated tweets skipped). Duplicates are found by a GROUP BY
    first, so only those rows are loaded (the tweet rows carry their
    encrypted text). A pick is a Read pick: a quote row on a saved copy
    (an agentic reply quoted it) does not make the copy a pick."""
    dup = (db.session.query(ExternalItem.user_id, ExternalItem.external_id)
           .filter(ExternalItem.source.in_(TWEET_SOURCES)))
    if user_id:
        dup = dup.filter(ExternalItem.user_id == user_id)
    dup = (dup.group_by(ExternalItem.user_id, ExternalItem.external_id)
           .having(db.func.count(ExternalItem.id) > 1).all())
    if not dup:
        return [], 0
    rows = ExternalItem.query.filter(
        ExternalItem.source.in_(TWEET_SOURCES),
        db.tuple_(ExternalItem.user_id, ExternalItem.external_id).in_(
            [tuple(d) for d in dup])).all()
    by_tweet = defaultdict(list)
    for r in rows:
        by_tweet[(r.user_id, r.external_id)].append(r)
    picked = {r[0] for r in db.session.query(FeedPick.external_item_id)
              .filter(FeedPick.kind == reference_log.KIND_READ,
                      FeedPick.external_item_id.in_([r.id for r in rows]))
              .distinct().all()}
    pairs, skipped = [], 0
    for group in by_tweet.values():
        pick_rows = [r for r in group if r.id in picked]
        copies = [r for r in group
                  if pick_rows and r.id != pick_rows[0].id and r.is_saved]
        if len(pick_rows) != 1 or not copies:
            skipped += 1  # no pick (an older import duplicate), or two
            continue
        pairs.append((pick_rows[0], copies))
    return pairs, skipped


def _quoting_nodes(copy, since):
    """LLM replies of the copy's owner that may quote it: written from
    *since* (a day before the copy or its tweet's first pick, whichever
    came first: a re-clip moves fetched_at forward) until the copy was
    last quoted. None of them when it never was."""
    if not copy.surfaced_count:
        return []
    q = Node.query.filter(
        Node.human_owner_id == copy.user_id,
        Node.node_type == "llm",
        Node.content.isnot(None))
    if since is not None:
        q = q.filter(Node.created_at >= since - timedelta(days=1))
    if copy.last_surfaced_at is not None:
        q = q.filter(Node.created_at <= copy.last_surfaced_at)
    return q.all()


def _repoint_picks(copy, keep):
    """Move the copy's FeedPick rows to the kept row; where a reply has
    a row for both (it quoted the two copies), drop the copy's."""
    kept_nodes = {r[0] for r in db.session.query(FeedPick.node_id)
                  .filter_by(external_item_id=keep.id).all()}
    for row in FeedPick.query.filter_by(external_item_id=copy.id).all():
        if row.node_id in kept_nodes:
            db.session.delete(row)
        else:
            row.external_item_id = keep.id
            kept_nodes.add(row.node_id)
    db.session.flush()


def merge_copies(user_id, apply):
    """Step 3: fold each saved copy into the pick's row. Returns the ids
    of the rows it made saved references again."""
    pairs, skipped = _saved_copy_pairs(user_id)
    candidates = rewritten = 0
    first_pick = dict(db.session.query(
        FeedPick.external_item_id, db.func.min(FeedPick.created_at))
        .filter(FeedPick.kind == reference_log.KIND_READ,
                FeedPick.external_item_id.in_(
                    [k.id for k, _ in pairs] or [0]))
        .group_by(FeedPick.external_item_id).all())
    for keep, copies in pairs:
        sources = [keep.source] + [c.source for c in copies]
        final_source = min(sources, key=lambda src: (
            SOURCE_PRIORITY.index(src) if src in SOURCE_PRIORITY
            else len(SOURCE_PRIORITY)))
        for copy in copies:
            starts = [t for t in (copy.fetched_at, first_pick.get(keep.id))
                      if t]
            nodes = _quoting_nodes(copy, min(starts) if starts else None)
            candidates += len(nodes)
            if not apply:
                continue
            marker = re.compile(r"\{quote_ext:%d\}" % copy.id)
            for node in nodes:
                text = node.get_content() or ""
                if marker.search(text):
                    node.set_content(
                        marker.sub("{quote_ext:%d}" % keep.id, text))
                    rewritten += 1
                    print(f"   reply {node.id}: quote of {copy.id} → {keep.id}")
            # The fuller text, unless one of the two was edited by hand.
            keep_text = keep.get_content() or ""
            copy_text = copy.get_content() or ""
            take_copy = (copy.edited_at is not None
                         or (keep.edited_at is None
                             and len(copy_text) > len(keep_text)))
            if take_copy:
                keep.set_content(copy_text)
                keep.title = copy.title or keep.title
                keep.edited_at = copy.edited_at or keep.edited_at
                ExternalItemEmbedding.query.filter_by(
                    item_id=keep.id).delete()
            reads = [t for t in (keep.read_at, copy.read_at) if t]
            keep.read_at = min(reads) if reads else None
            verdicts = [(r.feedback_at, r.feedback) for r in (keep, copy)
                        if r.feedback]
            if verdicts:
                keep.feedback_at, keep.feedback = max(
                    verdicts, key=lambda v: v[0] or datetime.min)
            keep.surfaced_count = ((keep.surfaced_count or 0)
                                   + (copy.surfaced_count or 0))
            lasts = [t for t in (keep.last_surfaced_at,
                                 copy.last_surfaced_at) if t]
            keep.last_surfaced_at = max(lasts) if lasts else None
            if copy.public_source is False:
                keep.public_source = False
            keep_has_speech = db.session.query(TTSChunk.id).filter_by(
                item_id=keep.id).first() is not None
            if not keep_has_speech and not keep.audio_tts_url \
                    and copy.audio_tts_url:
                keep.audio_tts_url = copy.audio_tts_url
                TTSChunk.query.filter_by(item_id=copy.id).update(
                    {"item_id": keep.id}, synchronize_session=False)
            else:
                TTSChunk.query.filter_by(item_id=copy.id).delete()
            ReferenceAction.query.filter_by(item_id=copy.id).update(
                {"item_id": keep.id}, synchronize_session=False)
            _repoint_picks(copy, keep)
            db.session.expire(copy)
            db.session.delete(copy)
            db.session.flush()
        if apply:
            keep.source = final_source
            db.session.flush()
        print(f"   tweet {keep.external_id} (user {keep.user_id}): "
              f"{len(copies)} copies into {keep.id}, source {final_source}")
    n_copies = sum(len(c) for _, c in pairs)
    print(f"3. merge: {n_copies} saved copies of picked tweets folded into "
          f"{len(pairs)} pick rows; {skipped} duplicated tweets skipped (no "
          f"Read pick, or two); {candidates} replies to check for their "
          f"quotes" + (f", {rewritten} rewritten" if apply else ""))
    return {k.id for k, _ in pairs}


def stamp_old_picks(user_id, apply):
    """Step 4: decided_at and the prior marks on picks from before them."""
    picks = _scoped(FeedPick.query.filter(FeedPick.decided_at.is_(None)),
                    FeedPick, user_id).all()
    renders = dict(db.session.query(FeedRender.node_id, FeedRender.created_at)
                   .filter(FeedRender.node_id.in_(
                       [p.node_id for p in picks] or [0])).all())
    if apply:
        for p in picks:
            node = db.session.get(Node, p.node_id)
            decided_at = (renders.get(p.node_id)
                          or (node.created_at if node else None)
                          or p.created_at)
            reference_log.stamp_prior(p, p.item, decided_at)
    print(f"4. picks: {len(picks)} picks stamped with when the model chose "
          f"and what it could know")


def drop_pick_embeddings(user_id, apply, relabeled=(), merged=()):
    """Step 5: READ_PICK_SOURCE rows are not searched. *relabeled* are
    step 1's rows, which a dry run has not relabeled; *merged* are the
    rows step 3 made saved references again, which keep theirs."""
    ids = [r[0] for r in _scoped(
        db.session.query(ExternalItem.id).filter(
            ExternalItem.source == READ_PICK_SOURCE), ExternalItem,
        user_id).all()]
    ids = sorted((set(ids) | {r.id for r in relabeled}) - set(merged))
    n = ExternalItemEmbedding.query.filter(
        ExternalItemEmbedding.item_id.in_(ids or [0])).count()
    if apply and n:
        ExternalItemEmbedding.query.filter(
            ExternalItemEmbedding.item_id.in_(ids)).delete(
                synchronize_session=False)
    print(f"5. embeddings: {n} embeddings of Read picks dropped")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: dry run)")
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        print("APPLY" if args.apply else "DRY RUN (nothing is written)")
        relabeled = relabel_pick_rows(args.user_id, args.apply)
        db.session.flush()
        carry_over_marks(args.user_id, args.apply)
        db.session.flush()
        merged = merge_copies(args.user_id, args.apply)
        db.session.flush()
        stamp_old_picks(args.user_id, args.apply)
        db.session.flush()
        drop_pick_embeddings(args.user_id, args.apply, relabeled, merged)
        if args.apply:
            db.session.commit()
            print("committed")
        else:
            db.session.rollback()


if __name__ == "__main__":
    main()
