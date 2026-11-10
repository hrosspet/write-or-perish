"""Privacy and AI usage utilities for Write or Perish.

This module provides enums, validation, and authorization functions for
the two-column privacy system:
- privacy_level: controls who can access the node (private/circles/public)
- ai_usage: controls how AI can use the node's content (none/chat/train)
"""

from enum import Enum
from typing import Optional
from flask_login import current_user
from sqlalchemy import and_, false, func, or_, select


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
#     recent context). It never reads another person's shared entry; the
#     one exception is the reader's explicit "Ask my AI" in that thread,
#     which passes as_ai=False for the thread chain only
#     (llm_completion._load_node_chain).

_NODE_ALIASES = {}


def _node_alias(role):
    """One alias of Node per role, made once. An alias copies every
    column of the table when it is made, and these filters are built for
    every query of an export or a thread: made per call they cost more
    than the queries (about 4 ms each, thousands per profile build)."""
    alias = _NODE_ALIASES.get(role)
    if alias is None:
        from sqlalchemy.orm import aliased
        from backend.models import Node
        alias = _NODE_ALIASES[role] = aliased(
            Node, flat=True, name=f"circle_{role}")
    return alias


def circles_enabled_for(user_id) -> bool:
    """The Circles flag for one user id: CIRCLES_V1 (everyone) or the
    id listed in CIRCLES_USER_IDS. Off by default. With it off nothing
    is shared with or by this user: a "circles" node reads as private."""
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
    """Circles is on for at least one account. While it is off for
    everyone (the default) the access rule has no circles part at all:
    every "circles" node is its owner's alone, and the queries are the
    ones from before the feature."""
    try:
        from flask import current_app
        config = current_app.config
    except RuntimeError:
        return False
    return bool(config.get("CIRCLES_V1", False)
                or config.get("CIRCLES_USER_IDS"))


# How long "no Ask my AI answer exists" is believed while Circles is off
# for everyone (no new one can be made then). INTRODUCED HEURISTIC.
_ANSWERS_CHECK_SECONDS = 600
_answers_seen = {"at": 0.0, "any": False}


def answers_may_exist() -> bool:
    """Whether the corpus filters need their "Ask my AI" clause: while
    Circles is on for anyone, and afterwards for as long as a reply of
    a model that points at an entry exists (an answer made before the
    flag was switched off must stay out of its owner's corpus). While
    Circles is off the look is one indexed query, remembered for
    _ANSWERS_CHECK_SECONDS."""
    if circles_on_anywhere():
        return True
    import time
    now = time.monotonic()
    if now - _answers_seen["at"] > _ANSWERS_CHECK_SECONDS or not _answers_seen["at"]:
        from backend.extensions import db
        from backend.models import Node
        try:
            _answers_seen["any"] = db.session.query(Node.id).filter(
                Node.audience_root_id.isnot(None),
                Node.audience_root_id != Node.id,
                Node.node_type == "llm").first() is not None
        except Exception:  # noqa: BLE001 - no answer is not known: keep the clause
            return True
        _answers_seen["at"] = now
    return _answers_seen["any"]


def _node_owner_id(node):
    return getattr(node, 'human_owner_id', None) or node.user_id


def readable_shared_entries(user_id: int):
    """SELECT of the ids of the shared entries *user_id* reads as part of
    their audience (not as their author): alive "circles" entries of an
    author who is not hidden (#269) and whom the user has not muted, shared with the user by name or through a circle the user
    is in now (and, for a member added with "Only new entries", shared
    with that circle since). Empty while Circles is off for the user."""
    from backend.models import (
        CircleMember, CircleMute, NodeCircle, NodeReader)
    root = _node_alias("shared")
    if not circles_enabled_for(user_id):
        return select(root.id).where(false())
    owner = func.coalesce(root.human_owner_id, root.user_id)
    by_name = select(NodeReader.node_id).where(NodeReader.user_id == user_id)
    by_circle = (
        select(NodeCircle.node_id)
        .join(CircleMember, CircleMember.circle_id == NodeCircle.circle_id)
        .where(CircleMember.user_id == user_id,
               or_(CircleMember.entries_from.is_(None),
                   NodeCircle.shared_at >= CircleMember.entries_from)))
    muted = select(CircleMute.muted_user_id).where(
        CircleMute.user_id == user_id)
    return select(root.id).where(
        root.privacy_level == PrivacyLevel.CIRCLES.value,
        root.audience_root_id == root.id,
        root.deleted_at.is_(None),
        owner != user_id,
        ~owner.in_(muted),
        hidden_owner_filter(root),
        or_(root.id.in_(by_name), root.id.in_(by_circle)),
    )


def _circle_cache():
    """Per-request memo of audience answers (a thread asks once per node;
    its nodes share one entry). None outside a request: a task asks the
    database every time."""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return None
        return request.environ.setdefault("loore.circle_access", {})
    except RuntimeError:
        return None


def forget_circle_access():
    """Drop the request's memo after a change of audience."""
    cache = _circle_cache()
    if cache is not None:
        cache.clear()


def in_audience(entry_id, user_id) -> bool:
    """*user_id* reads the shared entry *entry_id* as part of its
    audience (see readable_shared_entries)."""
    if entry_id is None or user_id is None:
        return False
    cache = _circle_cache()
    key = ("in", entry_id, user_id)
    if cache is not None and key in cache:
        return cache[key]
    from backend.extensions import db
    sub = readable_shared_entries(user_id).subquery()
    found = db.session.execute(
        select(sub.c.id).where(sub.c.id == entry_id)).first() is not None
    if cache is not None:
        cache[key] = found
    return found


def audience_entry_owner(entry_id):
    """The owner (user id) of the shared entry *entry_id*, or None."""
    if entry_id is None:
        return None
    cache = _circle_cache()
    key = ("owner", entry_id)
    if cache is not None and key in cache:
        return cache[key]
    from backend.extensions import db
    from backend.models import Node
    row = db.session.query(Node.user_id, Node.human_owner_id).filter(
        Node.id == entry_id).first()
    owner = None if row is None else (row[1] or row[0])
    if cache is not None:
        cache[key] = owner
    return owner


def _circles_access(node, user_id, as_ai=False) -> bool:
    """The circles branch of the access rule for a node that is not the
    user's own. A node without an audience entry is private."""
    entry_id = getattr(node, 'audience_root_id', None)
    if entry_id is None or not circles_on_anywhere():
        return False
    # A reply in the user's own shared thread: the thread's author reads
    # every reply written to it (their model too, by each reply's own
    # ai_usage, as in a public thread).
    if entry_id != node.id and audience_entry_owner(entry_id) == user_id:
        return True
    if as_ai:
        return False
    return in_audience(entry_id, user_id)


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
    """*node* is an AI answer its owner asked for in someone else's shared
    thread ("Ask my AI"): written from that person's shared entry, so it
    stays in the thread. It is the reader's to read there, and no part
    of the reader's corpus (export, profile builds, recent context,
    search) or of their other prompts."""
    if getattr(node, 'node_type', None) != "llm":
        return False
    # Whatever its level now: taken back to Only me it is still written
    # from that entry. Two records say so, and either is enough: the
    # answer's marker, and its pointer at an entry that is not its
    # owner's. A pointer whose entry no longer exists counts (the owner
    # of a missing entry is nobody).
    if _has_ask_marker(node):
        return True
    entry_id = getattr(node, 'audience_root_id', None)
    return (isinstance(entry_id, int) and entry_id != node.id
            and audience_entry_owner(entry_id) != _node_owner_id(node))


_ASK_MARKER = "_circle_ask"     # llm_nodes.CIRCLE_ASK_MARKER


def _has_ask_marker(node) -> bool:
    meta = getattr(node, 'tool_calls_meta', None)
    return isinstance(meta, str) and f'"{_ASK_MARKER}"' in meta


def answer_source_open(node) -> bool:
    """The entry an "Ask my AI" answer was written from is still there
    to read: it exists, is not deleted, and its author's account is not
    hidden (#269). When it is not, the answer is closed to everyone, its
    owner included: it holds that entry's content in derived form, and
    goes when the entry goes (node_cleanup sweeps it). It opens again if
    the author restores the entry."""
    from backend.extensions import db
    from backend.models import Node
    entry_id = getattr(node, 'audience_root_id', None)
    if entry_id is None or entry_id == node.id:
        return False
    entry = db.session.get(Node, entry_id)
    return (entry is not None and entry.deleted_at is None
            and not owner_hidden(entry))


def answer_source_open_filter(node_model):
    """Query form of answer_source_open."""
    entry = _node_alias("source")
    return select(entry.id).where(
        entry.id == node_model.audience_root_id,
        entry.id != node_model.id,
        entry.deleted_at.is_(None),
        hidden_owner_filter(entry),
    ).exists()


def others_thread_answer_filter(node_model):
    """Query form of is_others_thread_answer."""
    entry = _node_alias("thread")
    owner = func.coalesce(node_model.human_owner_id, node_model.user_id)
    return and_(
        node_model.node_type == "llm",
        or_(
            # The marker (JSON text; the quotes keep the match exact).
            func.coalesce(node_model.tool_calls_meta, "").contains(
                f'"{_ASK_MARKER}"', autoescape=True),
            and_(
                node_model.audience_root_id.isnot(None),
                node_model.audience_root_id != node_model.id,
                # Not EXISTS: an entry that is gone is nobody's.
                ~select(entry.id).where(
                    entry.id == node_model.audience_root_id,
                    func.coalesce(entry.human_owner_id, entry.user_id)
                    == owner,
                ).exists())))


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
        # The user's own. The exception is the answer their "Ask my AI"
        # wrote in someone else's shared thread: never for a model (it
        # stays in that thread), and for the user only while the entry
        # it was written from is there to read.
        if is_others_thread_answer(node):
            return not as_ai and answer_source_open(node)
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


def _circles_filter(node_model, user_id, as_ai=False):
    """Query form of _circles_access."""
    if not circles_on_anywhere():
        return false()
    entry = _node_alias("answer")
    # A reply in the user's own shared thread.
    in_own_thread = and_(
        node_model.audience_root_id != node_model.id,
        select(entry.id).where(
            entry.id == node_model.audience_root_id,
            func.coalesce(entry.human_owner_id, entry.user_id) == user_id,
        ).exists())
    reads = [in_own_thread]
    if not as_ai:
        reads.append(node_model.audience_root_id.in_(
            readable_shared_entries(user_id)))
    return and_(
        node_model.privacy_level == PrivacyLevel.CIRCLES.value,
        node_model.audience_root_id.isnot(None),
        or_(*reads))


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
    """
    own = or_(node_model.user_id == user_id,
              node_model.human_owner_id == user_id)
    if answers_may_exist():
        answer = others_thread_answer_filter(node_model)
        own = and_(own, ~answer if as_ai else or_(
            ~answer, answer_source_open_filter(node_model)))
    return or_(
        own,
        node_model.privacy_level == PrivacyLevel.PUBLIC,
        _circles_filter(node_model, user_id, as_ai=as_ai),
    )


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
