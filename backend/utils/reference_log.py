"""What Loore recommended, and what the user did with it (#352).

Two logs, facts only:

- FeedPick: every reference Loore put in front of the user in a reply —
  a tweet a Read picked (kind 'read') or a saved reference an agentic
  reply quoted (kind 'quote') — with the model, when it chose
  (decided_at) and what it could know about the reference then
  (prior_read, prior_verdict). Written whatever the user does next, so
  a pick nobody touched is a result too.
- ReferenceAction: every open (a click through to the post), read or
  unread mark and good/bad verdict, with the reply it was done in, or
  None when done outside any reply (the reference page, and marks
  carried over from before the log existed).

Which actions count for which recommendation is computed here, whenever
a page or the report asks, and never stored, so the rule can change
without repairing data:

- A recommendation is BLIND when the reference had no verdict when its
  model chose (prior_verdict is None).
- The blind Read picks of a reference form one group, across threads:
  two Reads run in parallel for two models are usually two threads (a
  home-page Read starts its own), and both ask "is this worth reading
  now". The blind quotes of a reference form a group within one thread
  (a regenerated reply), since a good quote depends on the conversation.
- In a group, the latest verdict given in any member's reply counts for
  every member, so rating the overlap in either of two parallel Reads
  rates it for both. Opens and read marks are shared the same way.
  Actions outside any reply count for the blind Read picks.
- Only actions from the moment a recommendation's model chose count for
  it.
- A recommendation that is not blind (its model could know the user's
  verdict) is judged only by what the user did in its own reply.

Each counted verdict says whether it was given in the recommendation's
own reply ("own") or elsewhere in its group ("shared").
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from backend.extensions import db

KIND_READ = "read"
KIND_QUOTE = "quote"

ACTION_OPEN = "open"
ACTION_READ = "read"
ACTION_UNREAD = "unread"
ACTION_VERDICT = "verdict"

VERDICTS = ("good", "bad")


def _actions_of(item_id):
    from backend.models import ReferenceAction
    return (ReferenceAction.query.filter_by(item_id=item_id)
            .order_by(ReferenceAction.created_at.asc(),
                      ReferenceAction.id.asc()).all())


def _carry_over(item):
    """Before the first logged action on *item*, record the marks it
    already carries (set before the log existed, or by a path that does
    not log) as actions outside any reply, at the times they were set,
    so the log alone says what the user had done and when."""
    from backend.models import ReferenceAction
    has_any = db.session.query(ReferenceAction.id).filter_by(
        item_id=item.id).first() is not None
    if has_any:
        return
    if item.read_at is not None:
        db.session.add(ReferenceAction(
            user_id=item.user_id, item_id=item.id, node_id=None,
            kind=ACTION_READ, created_at=item.read_at))
    if item.feedback in VERDICTS:
        db.session.add(ReferenceAction(
            user_id=item.user_id, item_id=item.id, node_id=None,
            kind=ACTION_VERDICT, value=item.feedback,
            created_at=item.feedback_at or item.read_at
            or datetime.utcnow()))


@dataclass
class _Carried:
    """A mark read off a reference's own columns, standing in for the
    action _carry_over would log (see outcomes)."""
    kind: str
    value: Optional[str]
    created_at: datetime
    node_id: Optional[int] = None
    id: int = 0


def _carried_marks(item):
    """The actions _carry_over would record for *item*, not stored."""
    marks = []
    if item.read_at is not None:
        marks.append(_Carried(ACTION_READ, None, item.read_at))
    if item.feedback in VERDICTS:
        marks.append(_Carried(ACTION_VERDICT, item.feedback,
                              item.feedback_at or item.read_at
                              or datetime.utcnow()))
    return sorted(marks, key=lambda a: a.created_at)


def record_action(item, kind, value=None, node_id=None, at=None):
    """Log one action on *item* (an ExternalItem) BEFORE the caller
    changes the item's own read_at/feedback columns. Adds to the
    session; the caller commits."""
    from backend.models import ReferenceAction
    _carry_over(item)
    row = ReferenceAction(
        user_id=item.user_id, item_id=item.id, node_id=node_id,
        kind=kind, value=value if kind == ACTION_VERDICT else None,
        created_at=at or datetime.utcnow())
    db.session.add(row)
    return row


def reply_node_id(raw, user_id):
    """The reply an action was done in, from a client-supplied id: kept
    only when it is a node of this user's (as its human owner). Anything
    else is logged as done outside any reply rather than refused — the
    action itself still happened."""
    from backend.models import Node
    try:
        node_id = int(raw)
    except (TypeError, ValueError):
        return None
    node = db.session.get(Node, node_id)
    if node is None:
        return None
    owner = node.human_owner_id or node.user_id
    return node.id if owner == user_id else None


def _replay(actions):
    """(read, verdict, verdict_action) after *actions*, in order. An open
    and a verdict both mark the reference read, as the endpoints do."""
    read = False
    verdict = None
    verdict_action = None
    for a in actions:
        if a.kind in (ACTION_OPEN, ACTION_READ):
            read = True
        elif a.kind == ACTION_UNREAD:
            read = False
        elif a.kind == ACTION_VERDICT:
            verdict = a.value
            verdict_action = a
            if a.value:
                read = True
    return read, verdict, verdict_action


def state_at(item, at):
    """(read, verdict, verdict_at) of *item* as of *at*, from the log.
    A reference with nothing logged yet is read off its own columns."""
    actions = _actions_of(item.id)
    if not actions:
        read = item.read_at is not None and item.read_at <= at
        verdict = (item.feedback if item.feedback in VERDICTS
                   and item.feedback_at is not None
                   and item.feedback_at <= at else None)
        return read, verdict, item.feedback_at if verdict else None
    read, verdict, verdict_action = _replay(
        [a for a in actions if a.created_at <= at])
    return read, verdict, (verdict_action.created_at
                           if verdict_action is not None and verdict
                           else None)


def stamp_prior(pick, item, decided_at):
    """Set *pick*'s decided_at and what its model could know then."""
    decided_at = decided_at or datetime.utcnow()
    read, verdict, _ = state_at(item, decided_at)
    pick.decided_at = decided_at
    pick.prior_read = read
    pick.prior_verdict = verdict


def log_quotes(node, text, user_id, decided_at, picked_by, already):
    """One FeedPick (kind 'quote') per saved reference *text* quotes
    ({quote_ext:ID}) on *node*, in the order quoted, once per item per
    turn (*already*, the caller's set). Skips references this reply
    already has a row for — a Read reply's picks are quoted in its own
    text. Adds to the session; the caller commits."""
    from backend.models import ExternalItem, FeedPick
    from backend.utils.quotes import find_ext_quote_ids
    ids = []
    for item_id in find_ext_quote_ids(text or ""):
        if item_id not in ids and item_id not in already:
            ids.append(item_id)
    if not ids or node is None:
        return []
    items = {i.id: i for i in ExternalItem.query.filter(
        ExternalItem.id.in_(ids), ExternalItem.user_id == user_id).all()}
    have = {r[0] for r in db.session.query(FeedPick.external_item_id)
            .filter(FeedPick.node_id == node.id,
                    FeedPick.external_item_id.in_(ids)).all()}
    rows = []
    rank = 0
    for item_id in ids:
        item = items.get(item_id)
        if item is None:
            continue
        already.add(item_id)
        if item_id in have:
            continue
        rank += 1
        row = FeedPick(
            user_id=user_id, node_id=node.id, external_item_id=item.id,
            kind=KIND_QUOTE, rank=rank, relevance=None, recommended=False,
            picked_by=picked_by)
        stamp_prior(row, item, decided_at)
        db.session.add(row)
        rows.append(row)
    return rows


@dataclass
class Outcome:
    """What counts for one recommendation (see the module docstring)."""
    verdict: Optional[str] = None
    # Given in another member's reply or outside any reply.
    verdict_shared: bool = False
    verdict_at: Optional[datetime] = None
    opened: bool = False
    opened_shared: bool = False
    read: bool = False
    read_shared: bool = False
    blind: bool = True


def outcomes(recs):
    """{FeedPick.id: Outcome} for *recs*, in a handful of queries: every
    recommendation and every action of the references involved, and the
    thread roots of the quotes."""
    from backend.models import FeedPick, ReferenceAction
    from backend.utils.thread_tree import thread_root_of
    recs = list(recs)
    if not recs:
        return {}
    item_ids = {r.external_item_id for r in recs}
    mates = FeedPick.query.filter(
        FeedPick.external_item_id.in_(item_ids)).all()
    by_item = defaultdict(list)
    for m in mates:
        by_item[m.external_item_id].append(m)
    actions = defaultdict(list)
    for a in (ReferenceAction.query
              .filter(ReferenceAction.item_id.in_(item_ids))
              .order_by(ReferenceAction.created_at.asc(),
                        ReferenceAction.id.asc()).all()):
        actions[a.item_id].append(a)
    # A reference nothing has been logged for yet (marked before the log
    # existed, not yet carried over by the migration script) counts its
    # own marks as done outside any reply, as _carry_over will store them.
    for rec in recs:
        if rec.external_item_id not in actions:
            actions[rec.external_item_id] = _carried_marks(rec.item)
    quote_nodes = {m.node_id for m in mates if m.kind == KIND_QUOTE}
    roots = thread_root_of(quote_nodes) if quote_nodes else {}

    result = {}
    for rec in recs:
        blind = rec.prior_verdict is None
        if blind:
            group = [m for m in by_item[rec.external_item_id]
                     if m.prior_verdict is None and m.kind == rec.kind
                     and (rec.kind != KIND_QUOTE
                          or roots.get(m.node_id) == roots.get(rec.node_id))]
            nodes = {m.node_id for m in group} | {rec.node_id}
            outside = rec.kind == KIND_READ
        else:
            nodes = {rec.node_id}
            outside = False
        counted = [a for a in actions[rec.external_item_id]
                   if (a.node_id in nodes
                       or (outside and a.node_id is None))
                   and (rec.decided_at is None
                        or a.created_at >= rec.decided_at)]
        read, verdict, verdict_action = _replay(counted)
        own = [a for a in counted if a.node_id == rec.node_id]
        opened = any(a.kind == ACTION_OPEN for a in counted)
        own_read, _, _ = _replay(own)
        result[rec.id] = Outcome(
            verdict=verdict,
            verdict_shared=bool(verdict and verdict_action.node_id
                                != rec.node_id),
            verdict_at=verdict_action.created_at if verdict else None,
            opened=opened,
            opened_shared=opened and not any(
                a.kind == ACTION_OPEN for a in own),
            read=read,
            read_shared=read and not own_read,
            blind=blind,
        )
    return result


def rated_before(rec, item):
    """For a recommendation that is not blind: the verdict its model
    could know and when it was given — the page shows "You rated this
    good on …" beside an empty control. None for a blind one."""
    if rec.prior_verdict is None or rec.decided_at is None:
        return None
    _, verdict, verdict_at = state_at(item, rec.decided_at)
    if not verdict:
        return None
    return {"verdict": verdict, "at": verdict_at}
