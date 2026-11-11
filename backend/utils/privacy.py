"""Privacy and AI usage utilities for Write or Perish.

This module provides enums, validation, and authorization functions for
the two-column privacy system:
- privacy_level: controls who can access the node (private/circles/public)
- ai_usage: controls how AI can use the node's content (none/chat/train)
"""

from enum import Enum
from typing import Optional
from flask_login import current_user
from sqlalchemy import and_, func, or_, select


class PrivacyLevel(str, Enum):
    """Privacy level controlling who can access a node."""
    PRIVATE = "private"  # Only the owner can read
    CIRCLES = "circles"  # Shared with the author's circles / named people
    PUBLIC = "public"    # Visible to all users


class AIUsage(str, Enum):
    """AI usage permission controlling how AI can use node content."""
    NONE = "none"   # No AI usage allowed
    CHAT = "chat"   # AI can use for generating responses (not training)
    TRAIN = "train" # AI can use for training data


# Valid values for validation
VALID_PRIVACY_LEVELS = {level.value for level in PrivacyLevel}
VALID_AI_USAGE = {usage.value for usage in AIUsage}
# Subset of ai_usage values that permit AI to read the content
AI_ALLOWED = {AIUsage.CHAT.value, AIUsage.TRAIN.value}


def validate_privacy_level(privacy_level: str) -> bool:
    """Validate that a privacy level is valid.

    Args:
        privacy_level: The privacy level to validate

    Returns:
        True if valid, False otherwise
    """
    return privacy_level in VALID_PRIVACY_LEVELS


def validate_ai_usage(ai_usage: str) -> bool:
    """Validate that an AI usage value is valid.

    Args:
        ai_usage: The AI usage value to validate

    Returns:
        True if valid, False otherwise
    """
    return ai_usage in VALID_AI_USAGE


def find_human_owner(node) -> Optional[int]:
    """Find the human owner of an LLM node by traversing up the parent chain.

    For chains like Human → LLM → LLM, this returns the human's user_id.
    Traverses up the parent chain, skipping LLM-authored nodes, until finding
    a human author or running out of parents.

    Args:
        node: The Node object to find the human owner for

    Returns:
        The user_id of the human owner, or None if not found
    """
    current = node.parent
    while current:
        # If this node is NOT an LLM node, we found the human owner
        if getattr(current, 'node_type', 'user') != "llm":
            return current.user_id
        current = current.parent
    return None


def is_node_owner(node, user_id: Optional[int]) -> bool:
    """True when *user_id* owns *node*: for an AI reply the user who asked
    for it (human_owner_id), else the node's author. Things that belong to
    the owner alone, such as a failed reply's error text, go to this user
    only; anyone else who can see the node gets the node without them."""
    return user_id is not None \
        and (node.human_owner_id or node.user_id) == user_id


# ── Circles ──────────────────────────────────────────────────────────────
# A "circles" node is read by its owner and by the audience of the entry
# it belongs to (Node.audience_root_id: the shared entry itself, or the
# shared entry a reply was written under). The audience is read from the
# circles' current members every time, so removing a person, deleting a
# circle or taking an entry back closes it at once. Two readers of the
# rule:
#   * a person (as_ai=False): the API routes.
#   * a model acting for a person (as_ai=True): everything that builds a
#     prompt or a corpus for that person (the thread chain, {quote:ID},
#     read_full, search previews, the export behind profile builds and
#     recent context). It never reads another person's shared entry
#     or reply. The one exception is an AI reply generated in a shared
#     thread itself: its chain is read as the person reads the thread
#     (llm_completion._load_node_chain).

# How the rule is evaluated. Everything about Circles that the access
# rule needs for one user is four small sets of ids, worked out once per
# database session (CircleSets) and handed to the queries as literal
# lists. A user who shares nothing, reads nothing shared and has no
# AI reply in someone else's thread gets the same SQL as before the
# feature, with
# Circles on or off; nobody's thread or Log walk carries a subquery.

def circles_enabled_for(user_id) -> bool:
    """The Circles flag for one user id: CIRCLES_V1 (everyone) or the
    id listed in CIRCLES_USER_IDS. Off by default. With it off nothing
    is shared with this user: a "circles" node reads as private."""
    try:
        from flask import current_app
        config = current_app.config
    except RuntimeError:
        return False
    if config.get("CIRCLES_V1", False):
        return True
    return user_id is not None and user_id in (
        config.get("CIRCLES_USER_IDS") or set())


def circles_on_anywhere() -> bool:
    """Circles is on for at least one account."""
    try:
        from flask import current_app
        config = current_app.config
    except RuntimeError:
        return False
    return bool(config.get("CIRCLES_V1", False)
                or config.get("CIRCLES_USER_IDS"))


# How long "no node points at a shared entry" is believed while Circles
# is off for everyone (no new pointer can be made then). INTRODUCED
# HEURISTIC.
_POINTERS_CHECK_SECONDS = 600
_pointers_seen = {"at": 0.0, "any": False}


def pointers_may_exist() -> bool:
    """Whether any node may carry a thread pointer (audience_root_id):
    while Circles is on for anyone, and afterwards for as long as one
    exists (a reply made before the flag was switched off keeps its
    rules). While Circles is off the look is one
    indexed query, remembered for _POINTERS_CHECK_SECONDS. When this is
    False the access rule runs no Circles query at all."""
    if circles_on_anywhere():
        return True
    import time
    now = time.monotonic()
    if (not _pointers_seen["at"]
            or now - _pointers_seen["at"] > _POINTERS_CHECK_SECONDS):
        from backend.extensions import db
        from backend.models import Node
        try:
            _pointers_seen["any"] = db.session.query(Node.id).filter(
                Node.audience_root_id.isnot(None)).first() is not None
        except Exception:  # noqa: BLE001 - not known: keep the rules on
            return True
        _pointers_seen["at"] = now
    return _pointers_seen["any"]


def _node_owner_id(node):
    return getattr(node, 'human_owner_id', None) or node.user_id


class CircleSets:
    """What the access rule needs about Circles for one user, as ids:

    * ``readable``: the shared entries the user reads as part of their
      audience (not as their author): alive "circles" entries of an
      author who is not hidden (#269) and whom the user has not muted,
      shared with the user by name or through a circle the user is in
      now (and, for a member added with "Only new entries", shared with
      that circle since). Empty while Circles is off for the user.
    * ``own_entries``: the user's own nodes that are, or once were,
      shared entries (they point at themselves for good). A reply that
      points at one of them was written into the user's own thread.
    * ``answers``: the AI replies the user asked for in other people's
      shared threads (their pointer is not at an entry of the user's).
    * ``closed_answers``: those of them whose entry the user cannot read
      now. Such a reply is written from the thread, so it is open to its
      owner exactly while the entry is: it closes when the user is removed,
      hides the author, or the author takes the entry back, changes its
      audience, deletes the circle, the entry, their writing or their
      account, and opens again when the entry does.
    """
    __slots__ = ("readable", "own_entries", "answers", "closed_answers")

    def __init__(self, readable=(), own_entries=(), answers=(),
                 closed_answers=()):
        self.readable = frozenset(readable)
        self.own_entries = frozenset(own_entries)
        self.answers = frozenset(answers)
        self.closed_answers = frozenset(closed_answers)


_NO_SETS = CircleSets()


def _session_cache():
    from backend.extensions import db
    return db.session.info.setdefault("circle_sets", {})


def forget_circle_access(session=None, *_):
    """Drop what this database session remembers about Circles. Called
    after every flush, commit and rollback of the session (below), and by
    the code that changes audiences with bulk statements."""
    try:
        if session is None:
            from backend.extensions import db
            session = db.session
        session.info.pop("circle_sets", None)
    except Exception:  # noqa: BLE001 - no session, nothing remembered
        pass


def _install_cache_reset():
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    for name in ("after_flush", "after_commit", "after_rollback"):
        event.listen(Session, name, forget_circle_access)


_install_cache_reset()


def _load_circle_sets(user_id) -> CircleSets:
    from backend.extensions import db
    from backend.models import (
        CircleMember, CircleMute, Node, NodeCircle, NodeReader)
    readable = set()
    if circles_enabled_for(user_id):
        # From the user's own rows (indexed), then the entries by key.
        named = {i for (i,) in db.session.query(NodeReader.node_id).filter(
            NodeReader.user_id == user_id)}
        named.update(i for (i,) in db.session.query(NodeCircle.node_id).join(
            CircleMember, CircleMember.circle_id == NodeCircle.circle_id
        ).filter(CircleMember.user_id == user_id,
                 or_(CircleMember.entries_from.is_(None),
                     NodeCircle.shared_at >= CircleMember.entries_from)))
        if named:
            owner = func.coalesce(Node.human_owner_id, Node.user_id)
            muted = [i for (i,) in db.session.query(
                CircleMute.muted_user_id).filter(
                CircleMute.user_id == user_id)]
            ids = sorted(named)
            for start in range(0, len(ids), 900):
                q = db.session.query(Node.id).filter(
                    Node.id.in_(ids[start:start + 900]),
                    Node.privacy_level == PrivacyLevel.CIRCLES.value,
                    Node.audience_root_id == Node.id,
                    Node.deleted_at.is_(None),
                    owner != user_id,
                    hidden_owner_filter(Node))
                if muted:
                    q = q.filter(~owner.in_(muted))
                readable.update(i for (i,) in q)
    # The user's own nodes that carry a pointer: their shared entries
    # (pointing at themselves) and what they wrote into threads.
    own_entries, pointing = set(), []
    for nid, pointer, node_type in db.session.query(
            Node.id, Node.audience_root_id, Node.node_type).filter(
            Node.audience_root_id.isnot(None),
            or_(Node.user_id == user_id, Node.human_owner_id == user_id)):
        if pointer == nid:
            own_entries.add(nid)
        elif node_type == "llm":
            pointing.append((nid, pointer))
    answers = {nid for nid, pointer in pointing if pointer not in own_entries}
    closed = {nid for nid, pointer in pointing
              if nid in answers and pointer not in readable}
    # With Circles off for the user nothing is shared by them either: a
    # reply in their own thread reads as private, like the rest.
    if not circles_enabled_for(user_id):
        own_entries = set()
    return CircleSets(readable, own_entries, answers, closed)


def circle_sets(user_id) -> CircleSets:
    """The CircleSets of *user_id*, remembered for the database session
    (a request, or a task between its commits)."""
    if user_id is None or not pointers_may_exist():
        return _NO_SETS
    try:
        cache = _session_cache()
    except Exception:  # noqa: BLE001 - no session to remember in
        return _load_circle_sets(user_id)
    key = (user_id, circles_enabled_for(user_id))
    sets = cache.get(key)
    if sets is None:
        sets = cache[key] = _load_circle_sets(user_id)
    return sets


def readable_entry_ids(user_id):
    """Ids of the shared entries *user_id* reads as part of their
    audience, sorted (CircleSets.readable)."""
    return sorted(circle_sets(user_id).readable)


def in_audience(entry_id, user_id) -> bool:
    """*user_id* reads the shared entry *entry_id* as part of its
    audience."""
    return (entry_id is not None and user_id is not None
            and entry_id in circle_sets(user_id).readable)


def audience_entry_owner(entry_id):
    """The owner (user id) of the entry *entry_id*, or None when it no
    longer exists."""
    if entry_id is None:
        return None
    from backend.extensions import db
    from backend.models import Node
    try:
        cache = _session_cache().setdefault("owners", {})
    except Exception:  # noqa: BLE001
        cache = {}
    if entry_id not in cache:
        row = db.session.query(Node.user_id, Node.human_owner_id).filter(
            Node.id == entry_id).first()
        cache[entry_id] = None if row is None else (row[1] or row[0])
    return cache[entry_id]


def _circles_access(node, user_id, as_ai=False) -> bool:
    """The circles branch of the access rule for a node that is not the
    user's own. A node without an audience entry is private."""
    entry_id = getattr(node, 'audience_root_id', None)
    if entry_id is None:
        return False
    sets = circle_sets(user_id)
    # A reply in the user's own shared thread: the thread's author reads
    # every reply written to it (their model too, by each reply's own
    # ai_usage, as in a public thread).
    if entry_id != node.id and entry_id in sets.own_entries:
        return True
    if as_ai:
        return False
    return entry_id in sets.readable


def foreign_shared(node, user_id) -> bool:
    """*node* is circle-shared content that is not *user_id*'s and not
    written into *user_id*'s own shared thread: what a model acting for
    the user reads only when the user asks it to in that thread."""
    if getattr(node, 'privacy_level', None) != PrivacyLevel.CIRCLES:
        return False
    if _node_owner_id(node) == user_id or node.user_id == user_id:
        return False
    return not _circles_access(node, user_id, as_ai=True)


def is_others_thread_answer(node) -> bool:
    """*node* is an AI reply its owner asked for in someone else's shared
    thread. It is the owner's: in the thread, their Log, their search,
    their data download and their profile builds (Peter, 2026-11-11:
    "both Ben's own writing and LLM where Ben's the human owner should
    go to Ben's profile"). It is written from that thread, so two rules
    are its own:

    * its owner, and a model acting for them, reads it exactly while the
      entry is open to the owner (answer_open_to_owner), like the thread
      itself, and it is deleted with the entry;
    * it never goes out on the training key (is_others_shared_writing).

    Whatever its level now: set to Only me it is still written from that
    thread. The record is its pointer at an entry that is not its
    owner's (an entry that no longer exists is nobody's)."""
    if getattr(node, 'node_type', None) != "llm":
        return False
    entry_id = getattr(node, 'audience_root_id', None)
    return (isinstance(entry_id, int) and entry_id != node.id
            and audience_entry_owner(entry_id) != _node_owner_id(node))


def is_others_shared_writing(node) -> bool:
    """*node* was written into a shared thread by someone other than the
    thread's author (a reader's reply, an AI reply a reader asked for).
    In anyone else's prompt such writing is never licensed for training,
    whatever its own ai_usage: its writer shared it with people."""
    entry_id = getattr(node, 'audience_root_id', None)
    return (isinstance(entry_id, int) and entry_id != node.id
            and audience_entry_owner(entry_id) != _node_owner_id(node))


def others_shared_writing_ids(node_ids):
    """Those of *node_ids* that are is_others_shared_writing, in one
    query (none while no pointer exists)."""
    ids = {i for i in (node_ids or ()) if i is not None}
    if not ids or not pointers_may_exist():
        return set()
    from backend.extensions import db
    from backend.models import Node
    out = set()
    for nid, owner, human_owner, pointer in db.session.query(
            Node.id, Node.user_id, Node.human_owner_id,
            Node.audience_root_id).filter(
            Node.id.in_(sorted(ids)), Node.audience_root_id.isnot(None),
            Node.audience_root_id != Node.id):
        if audience_entry_owner(pointer) != (human_owner or owner):
            out.add(nid)
    return out


def answer_open_to_owner(node, user_id) -> bool:
    """An AI reply asked for in someone else's shared thread is open to
    its owner exactly while the entry of that thread is
    (CircleSets.closed_answers)."""
    entry_id = getattr(node, 'audience_root_id', None)
    return (isinstance(entry_id, int) and entry_id != node.id
            and entry_id in circle_sets(user_id).readable)


def others_thread_answer_filter(node_model):
    """Query form of is_others_thread_answer by the pointer, for the
    jobs that run over every account (the embedding sweep, the cleanup).
    The per-user filters use the id lists of CircleSets instead."""
    from sqlalchemy.orm import aliased
    from backend.models import Node
    global _ANSWER_ENTRY
    if _ANSWER_ENTRY is None:
        _ANSWER_ENTRY = aliased(Node, flat=True, name="circle_answer_entry")
    entry = _ANSWER_ENTRY
    owner = func.coalesce(node_model.human_owner_id, node_model.user_id)
    return and_(
        node_model.node_type == "llm",
        node_model.audience_root_id.isnot(None),
        node_model.audience_root_id != node_model.id,
        # Not EXISTS: an entry that is gone is nobody's.
        ~select(entry.id).where(
            entry.id == node_model.audience_root_id,
            func.coalesce(entry.human_owner_id, entry.user_id) == owner,
        ).exists())


_ANSWER_ENTRY = None


def own_answers_filter(node_model, user_id, closed_only=False):
    """For a listing of the user's own nodes that does not go through
    accessible_nodes_filter (keyword search, the Log): a condition that
    leaves out the user's AI replies in other people's shared threads
    (with *closed_only*, only the ones closed to them now), or None when
    the user has none."""
    sets = circle_sets(user_id)
    ids = sets.closed_answers if closed_only else sets.answers
    return node_model.id.notin_(sorted(ids)) if ids else None


def _can_user_access_ignoring_deleted(node, user_id: int,
                                      as_ai: bool = False) -> bool:
    """Same body as can_user_access_node, minus the deleted_at short-circuit.

    Internal helper used by can_user_view_tombstone to check whether a viewer
    *would have* had access to a node before it was soft-deleted. Routes
    should never call this directly — they'd bypass the leak protection in
    serialize_node.
    """
    owned = node.user_id == user_id or (
        getattr(node, 'human_owner_id', None)
        and node.human_owner_id == user_id)
    if owned:
        # The user's own. The exception is an AI reply they asked for
        # in someone else's shared thread: theirs (and their models')
        # only while the entry of the thread is there to read.
        if is_others_thread_answer(node):
            return answer_open_to_owner(node, user_id)
        return True

    privacy_level = getattr(node, 'privacy_level', PrivacyLevel.PRIVATE)

    if privacy_level == PrivacyLevel.PRIVATE:
        return False
    elif privacy_level == PrivacyLevel.PUBLIC:
        return True
    elif privacy_level == PrivacyLevel.CIRCLES:
        return _circles_access(node, user_id, as_ai=as_ai)

    return False


def owner_hidden(node) -> bool:
    """The node's owner deleted the account and it is in its grace
    period (#269). Until the purge (or a restore) the node behaves like a
    soft-deleted one for everyone: not accessible, shown as a tombstone
    to those who could see it, and other people's replies below it stay
    reachable. The owner cannot be signed in meanwhile. The owner row
    comes from the session's identity map after the first look, so a
    thread costs one query per author."""
    return owner_hidden_since(node) is not None


def _owner(node):
    from backend.extensions import db
    from backend.models import User
    owner_id = getattr(node, 'human_owner_id', None) or node.user_id
    return db.session.get(User, owner_id) if owner_id else None


def owner_hidden_since(node):
    """When the node's owner deleted the account, while it is in its
    grace period (#269); None otherwise."""
    owner = _owner(node)
    return owner.deleted_at if owner is not None else None


def author_gone(node) -> bool:
    """The node's author deleted the account: it is in its grace period
    (#269), or already deleted (its placeholders then belong to the
    ``loore-erased`` account). A tombstone of such a node shows no name,
    so other people cannot tell an account in its grace period from a
    deleted one, nor read a system account's name on it."""
    from backend.utils.system_accounts import SYSTEM_USERNAMES
    owner = _owner(node)
    return owner is not None and (owner.deleted_at is not None
                                  or owner.username in SYSTEM_USERNAMES)


def hidden_owner_filter(node_model):
    """Query-level counterpart of owner_hidden: the node's owner has not
    deleted the account. An uncorrelated subquery over the accounts in
    their grace period, which is usually empty."""
    from backend.models import User
    hidden = select(User.id).where(User.deleted_at.isnot(None))
    return ~func.coalesce(
        node_model.human_owner_id, node_model.user_id).in_(hidden)


def can_user_access_node(node, user_id: Optional[int] = None,
                         as_ai: bool = False) -> bool:
    """Check if a user can currently access a node (alive + privacy passes).

    Soft-deleted nodes are treated as inaccessible regardless of ownership;
    if you need to know whether the viewer *would have* had access pre-deletion
    (for tombstone rendering), use can_user_view_tombstone via serialize_node.

    Args:
        node: The Node object to check access for
        user_id: The user ID to check (defaults to current_user.id)
        as_ai: the reader is a model acting for the user (see Circles
            above): another person's shared entry is closed to it.

    Returns:
        True if user can access, False otherwise
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id

    # Soft-deleted: treat as inaccessible. Tombstone rendering goes through
    # serialize_node, which uses can_user_view_tombstone.
    if getattr(node, 'deleted_at', None) is not None:
        return False
    if not _can_user_access_ignoring_deleted(node, user_id, as_ai=as_ai):
        return False
    # Someone else's node whose author deleted the account and is in the
    # grace period (#269). Checked last: the viewer's own nodes and nodes
    # they cannot see anyway need no lookup of the author.
    if node.user_id == user_id or (
            getattr(node, 'human_owner_id', None)
            and node.human_owner_id == user_id):
        return True
    return not owner_hidden(node)


def can_user_view_tombstone(node, user_id: Optional[int] = None,
                            as_ai: bool = False) -> bool:
    """Check if a viewer should see a tombstone placeholder for a soft-deleted node.

    Returns True only when the viewer *would have* had access pre-deletion
    (owner / human_owner / public / future circles). This prevents the
    tombstone's metadata (username + timestamp + node_type) from leaking to
    viewers who could not have seen the original node.

    The "should this tombstone be included at all" decision (e.g. is there a
    live descendant the viewer can reach) is the call site's responsibility,
    not this helper's. Breadcrumb and inline-quote paths always include;
    generic tree-walkers can apply additional filtering with their own
    pre-computed live-descendant set.

    Args:
        node: The (soft-deleted) Node object
        user_id: The user ID to check (defaults to current_user.id)

    Returns:
        True if a tombstone with metadata should be rendered for this viewer.
    """
    if getattr(node, 'deleted_at', None) is None and not owner_hidden(node):
        # Not deleted (nor hidden with its author's deleted account, #269)
        # — this helper isn't meant for live nodes.
        return False

    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id

    return _can_user_access_ignoring_deleted(node, user_id, as_ai=as_ai)


def can_user_see_node_or_tombstone(node, user_id: Optional[int] = None,
                                   as_ai: bool = False) -> bool:
    """True when the viewer can see *node* now, or could see it before it
    was soft-deleted (the case where they see its tombstone).

    Use it when a client-supplied id names a node to build on, such as a
    reply's parent or a thread's ancestors. The caller then handles
    deletion on its own, e.g. a 410 for a deleted parent or a scrubbed
    message for a deleted ancestor.

    A live node whose author deleted the account and is in its grace
    period (#269) is not seen at all here, even by someone who sees its
    placeholder in a thread: nobody else may reply to it, link it, or
    have it read into an AI reply's context.

    Args:
        node: The Node object to check
        user_id: The user ID to check (defaults to current_user.id)
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    if hidden_from(node, user_id):
        return False
    return (can_user_access_node(node, user_id, as_ai=as_ai)
            or can_user_view_tombstone(node, user_id, as_ai=as_ai))


def hidden_from(node, user_id) -> bool:
    """*node* is someone else's live node whose owner deleted the account
    and is in its grace period (#269). For *user_id* it counts as deleted,
    as it will after the purge. The viewer's own nodes never count."""
    return (getattr(node, 'deleted_at', None) is None
            and node.user_id != user_id
            and getattr(node, 'human_owner_id', None) != user_id
            and owner_hidden(node))


def shown_as_deleted(node, user_id) -> bool:
    """For *user_id*, *node* is a deleted placeholder: soft-deleted, or
    hidden with its owner's deleted account (#269)."""
    return (getattr(node, 'deleted_at', None) is not None
            or hidden_from(node, user_id))


def can_user_see_node_or_placeholder(node, user_id,
                                     as_ai: bool = False) -> bool:
    """For walks that show a deleted node as a placeholder in its place
    (the context of an AI reply): True when *user_id* can see *node*, or
    its placeholder, where a node hidden with its owner's deleted account
    (#269) counts as deleted. The walk then passes through it as it does
    after the purge; its text is never read."""
    return (can_user_access_node(node, user_id, as_ai=as_ai)
            or can_user_view_tombstone(node, user_id, as_ai=as_ai))


def can_ai_access_node(node, user_id) -> bool:
    """can_user_access_node for a model acting for *user_id*."""
    return can_user_access_node(node, user_id, as_ai=True)


def can_ai_view_tombstone(node, user_id) -> bool:
    return can_user_view_tombstone(node, user_id, as_ai=True)


def can_ai_see_node_or_tombstone(node, user_id) -> bool:
    return can_user_see_node_or_tombstone(node, user_id, as_ai=True)


def can_ai_see_node_or_placeholder(node, user_id) -> bool:
    return can_user_see_node_or_placeholder(node, user_id, as_ai=True)


def accessible_nodes_filter(node_model, user_id: int, as_ai: bool = False):
    """Return a SQLAlchemy filter clause for nodes accessible by the given user.

    This is the query-level counterpart of can_user_access_node().
    Use it to filter list queries (e.g. the feed) so that only
    accessible nodes are returned from the database. Soft-deleted nodes are
    excluded.

    Allows: the owner's own nodes, public nodes and circle-shared nodes
    the user reads (alive only). With *as_ai* (a model acting for the
    user, or the user's own corpus: export, profile builds) another
    person's shared entry is left out; see Circles above.
    """
    return and_(
        node_model.deleted_at.is_(None),
        accessible_nodes_filter_ignoring_deleted(
            node_model, user_id, as_ai=as_ai),
        # Not by an account deleted and in its grace period (#269). The
        # ignoring-deleted variant below walks through such nodes, like
        # through tombstones.
        or_(node_model.user_id == user_id,
            node_model.human_owner_id == user_id,
            hidden_owner_filter(node_model)),
    )


def accessible_nodes_filter_ignoring_deleted(node_model, user_id: int,
                                             as_ai: bool = False):
    """Same access criteria as accessible_nodes_filter, minus the
    deleted_at AND clause. Use in recursive CTE walks where the
    traversal needs to step through tombstones to reach an alive node
    further down — the outer query is responsible for re-filtering on
    deleted_at to distinguish alive nodes from tombstones.

    Without this, a chain like R(deleted) → C(deleted) → G(alive) would
    have C excluded by the recursive arm's filter, the join from G
    onto C would miss, and G would never appear in the CTE result —
    making G unreachable from Log even though it's alive and
    accessible. The §4a Case 2 feed swap depends on this traversal.

    Circles adds literal id lists only (CircleSets), and only for a user
    they concern: the entries whose threads the user reads, and the
    user's AI replies in other people's threads that are closed to them. Without either the expression is the one from before the
    feature.
    """
    sets = circle_sets(user_id)
    hidden_answers = sets.closed_answers
    entries = sets.own_entries if as_ai else (
        sets.own_entries | sets.readable)
    if not hidden_answers and not entries:
        return or_(
            node_model.user_id == user_id,
            node_model.human_owner_id == user_id,
            node_model.privacy_level == PrivacyLevel.PUBLIC,
        )
    own = or_(node_model.user_id == user_id,
              node_model.human_owner_id == user_id)
    if hidden_answers:
        own = and_(own, node_model.id.notin_(sorted(hidden_answers)))
    clauses = [own, node_model.privacy_level == PrivacyLevel.PUBLIC]
    if entries:
        clauses.append(and_(
            node_model.privacy_level == PrivacyLevel.CIRCLES.value,
            node_model.audience_root_id.in_(sorted(entries))))
    return or_(*clauses)


def can_user_edit_node(node, user_id: Optional[int] = None) -> bool:
    """Check if a user can edit a node.

    A user can edit a node if they are:
    1. The owner of the node (node.user_id == user_id)
    2. The "human owner" - the first non-LLM ancestor in the parent chain
       (useful when users want to edit AI responses they requested, even in
       chains like Human → LLM → LLM)

    Args:
        node: The Node object to check edit permissions for
        user_id: The user ID to check (defaults to current_user.id)

    Returns:
        True if user can edit, False otherwise
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id

    return (
        node.user_id == user_id
        or (getattr(node, 'human_owner_id', None) and node.human_owner_id == user_id)
    )


def can_ai_use_node_for_chat(node) -> bool:
    """Check if AI can use a node's content for generating chat responses.

    Args:
        node: The Node object to check

    Returns:
        True if AI can use for chat, False otherwise
    """
    ai_usage = getattr(node, 'ai_usage', AIUsage.NONE)
    return ai_usage in {AIUsage.CHAT, AIUsage.TRAIN}


def can_ai_use_node_for_training(node) -> bool:
    """Check if AI can use a node's content for training data.

    Args:
        node: The Node object to check

    Returns:
        True if AI can use for training, False otherwise
    """
    ai_usage = getattr(node, 'ai_usage', AIUsage.NONE)
    return ai_usage == AIUsage.TRAIN


def speech_allowed(entity) -> bool:
    """Whether text-to-speech may send *entity*'s text to the speech model.

    - A row that has an ai_usage (entries, a model's replies, profile
      versions) is spoken only when its ai_usage lets AI read it, the rule
      the speaker icon applies on the web and in the app. A reply is never
      generated where AI may not read (llm_nodes.reply_refusal), so a
      reply marked 'none' was imported that way or set by its owner, and
      is not spoken either.
    - Rows without an ai_usage (saved references) may be spoken.

    Speech that already exists is not affected: callers check this only
    before generating new speech."""
    if not hasattr(entity, "ai_usage"):
        return True
    return entity.ai_usage in AI_ALLOWED


SPEECH_REFUSED_MESSAGE = (
    "AI usage is set to none here, so no speech can be generated.")


def account_allows_ai(user) -> bool:
    """The account-level switch (#346). When ``default_ai_usage`` is not
    in AI_ALLOWED, no automatic or background job sends this user's data
    to a model or an embedding API. Jobs check it when they execute, not
    only when they are queued, because the setting can change in between.

    Switching the account does not change existing rows, so jobs also
    check each row's own ``ai_usage``."""
    return user is not None and user.default_ai_usage in AI_ALLOWED


PREFILL_REFUSAL_CODES = ("ai_opt_out", "prefill_declined")


def prefill_refusal(user):
    """Why an admin pre-fill or intentions run must not run for this
    account: ``(code, message)``, or None when it may. An unanswered
    consent (NULL) does not block: not every signup answers it (#346)."""
    if not account_allows_ai(user):
        return "ai_opt_out", "User has opted out of AI usage."
    if user.prefill_consent == "no":
        return ("prefill_declined",
                "This user declined the tweet seed (pre-fill consent: no).")
    return None


def get_default_privacy_settings() -> dict:
    """Get the default privacy settings for new nodes.

    Returns:
        Dictionary with default privacy_level and ai_usage
    """
    return {
        'privacy_level': PrivacyLevel.PRIVATE,
        'ai_usage': AIUsage.NONE
    }
