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
    CIRCLES = "circles"  # Shared with specific user-defined groups (future)
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


def _can_user_access_ignoring_deleted(node, user_id: int) -> bool:
    """Same body as can_user_access_node, minus the deleted_at short-circuit.

    Internal helper used by can_user_view_tombstone to check whether a viewer
    *would have* had access to a node before it was soft-deleted. Routes
    should never call this directly — they'd bypass the leak protection in
    serialize_node.
    """
    if node.user_id == user_id:
        return True

    if getattr(node, 'human_owner_id', None) and node.human_owner_id == user_id:
        return True

    privacy_level = getattr(node, 'privacy_level', PrivacyLevel.PRIVATE)

    if privacy_level == PrivacyLevel.PRIVATE:
        return False
    elif privacy_level == PrivacyLevel.PUBLIC:
        return True
    elif privacy_level == PrivacyLevel.CIRCLES:
        # TODO: Implement circles membership check when circles feature is built
        return False

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


def can_user_access_node(node, user_id: Optional[int] = None) -> bool:
    """Check if a user can currently access a node (alive + privacy passes).

    Soft-deleted nodes are treated as inaccessible regardless of ownership;
    if you need to know whether the viewer *would have* had access pre-deletion
    (for tombstone rendering), use can_user_view_tombstone via serialize_node.

    Args:
        node: The Node object to check access for
        user_id: The user ID to check (defaults to current_user.id)

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
    if not _can_user_access_ignoring_deleted(node, user_id):
        return False
    # Someone else's node whose author deleted the account and is in the
    # grace period (#269). Checked last: the viewer's own nodes and nodes
    # they cannot see anyway need no lookup of the author.
    if node.user_id == user_id or (
            getattr(node, 'human_owner_id', None)
            and node.human_owner_id == user_id):
        return True
    return not owner_hidden(node)


def can_user_view_tombstone(node, user_id: Optional[int] = None) -> bool:
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

    return _can_user_access_ignoring_deleted(node, user_id)


def can_user_see_node_or_tombstone(node, user_id: Optional[int] = None) -> bool:
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
    return (can_user_access_node(node, user_id)
            or can_user_view_tombstone(node, user_id))


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


def can_user_see_node_or_placeholder(node, user_id) -> bool:
    """For walks that show a deleted node as a placeholder in its place
    (the context of an AI reply): True when *user_id* can see *node*, or
    its placeholder, where a node hidden with its owner's deleted account
    (#269) counts as deleted. The walk then passes through it as it does
    after the purge; its text is never read."""
    return (can_user_access_node(node, user_id)
            or can_user_view_tombstone(node, user_id))


def accessible_nodes_filter(node_model, user_id: int):
    """Return a SQLAlchemy filter clause for nodes accessible by the given user.

    This is the query-level counterpart of can_user_access_node().
    Use it to filter list queries (e.g. the feed) so that only
    accessible nodes are returned from the database. Soft-deleted nodes are
    excluded.

    Currently allows: owner's own nodes + public nodes (alive only).
    When circles are implemented, update this alongside can_user_access_node().
    """
    return and_(
        node_model.deleted_at.is_(None),
        accessible_nodes_filter_ignoring_deleted(node_model, user_id),
        # Not by an account deleted and in its grace period (#269). The
        # ignoring-deleted variant below walks through such nodes, like
        # through tombstones.
        or_(node_model.user_id == user_id,
            node_model.human_owner_id == user_id,
            hidden_owner_filter(node_model)),
    )


def accessible_nodes_filter_ignoring_deleted(node_model, user_id: int):
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
    return or_(
        node_model.user_id == user_id,
        node_model.human_owner_id == user_id,
        node_model.privacy_level == PrivacyLevel.PUBLIC,
        # TODO: add circles membership subquery when circles feature is built
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
    return effective_ai_usage(node) == AIUsage.TRAIN.value


# Node.provenance of the entries Loore fetched through the X API
# (routes/import_data.PROVENANCE_PREFILL_X). X's Developer Agreement
# forbids using the X API or X Content to train a foundation or frontier
# model, so these entries are never at Train (#689): they are created at
# Chat at most, no route raises them to Train, and the training licence
# reads them as Chat whatever is stored. The Community Archive and the
# user's own archive upload are not the X API: their entries may be Train.
X_API_PROVENANCES = ("prefill_x",)
TRAIN_REFUSED_MESSAGE = (
    "Posts fetched from X can't be used for training, under X's rules "
    "for developers.")
TRAIN_REFUSED_CODE = "x_api_no_train"


# What a payload took in that keeps text written from it off training
# (Node / UserArtifact / UserTodo .unlicensed_input, #689):
X_API_INPUT = "x_api"         # posts fetched through the X API, or text a
#                               model wrote from them: Chat at most, and
#                               Train is never offered
EXTERNAL_INPUT = "external"   # other people's saved writing (references,
#                               Glean tweets): the licence reads it as Chat
UNLICENSED_INPUTS = (X_API_INPUT, EXTERNAL_INPUT)
# The documents Loore's models write from the person's writing: profile
# versions and windows, recent context, the todo list, the artifacts
# (intentions, memory, digest, ...). Of an account that has had an X API
# pre-fill, all of them read as X API input (x_api_account).
DERIVED_TABLES = ("user_profile", "user_recent_context", "user_todo",
                  "user_artifact")
# APICostLog.request_type of an X API pre-fill (tasks/imports): written
# before the tweets are imported, kept when they are deleted.
X_API_PREFILL_REQUEST = "x_prefill"


def train_refused_for(provenance) -> bool:
    """Whether an entry with this Node.provenance may never be at Train."""
    return provenance in X_API_PROVENANCES


def x_api_account(user_id) -> bool:
    """Whether the account has ever had an X API pre-fill: its cost record
    (``APICostLog`` request_type ``x_prefill``), which stays when the
    tweets are deleted. Then every document a model wrote from its writing
    may quote the posts (intentions quote "in their words"), so all of
    them read as Chat at most, whatever the account's setting (#689).
    Asked once per user per database session (a task or a request; the
    session is removed when its app context ends). A yes is never
    revoked; a no is asked again in the next session."""
    if user_id is None:
        return False
    from backend.extensions import db
    from backend.models import APICostLog
    memo = db.session.info.setdefault("x_api_accounts", {})
    if user_id in memo:
        return memo[user_id]
    found = db.session.query(APICostLog.id).filter(
        APICostLog.user_id == user_id,
        APICostLog.request_type == X_API_PREFILL_REQUEST,
    ).first() is not None
    memo[user_id] = found
    return found


def unlicensed_input(row):
    """What *row* holds that no training may take, or None:

    - ``X_API_INPUT`` — an entry fetched through the X API (provenance),
      a model-written node, artifact or todo version marked so, or any
      document derived from the writing of an account that has had an X
      API pre-fill (``x_api_account``);
    - ``EXTERNAL_INPUT`` — a model-written row marked as written from
      other people's saved writing.
    """
    mark = getattr(row, "unlicensed_input", None)
    if mark == X_API_INPUT:
        return X_API_INPUT
    if getattr(row, "provenance", None) in X_API_PROVENANCES:
        return X_API_INPUT
    if (getattr(row, "__tablename__", None) in DERIVED_TABLES
            and x_api_account(getattr(row, "user_id", None))):
        return X_API_INPUT
    return mark if mark in UNLICENSED_INPUTS else None


def train_refused(row) -> bool:
    """Whether *row* may never be at Train: it holds X API input."""
    return unlicensed_input(row) == X_API_INPUT


def cap_ai_usage(ai_usage, provenance):
    """*ai_usage* as an entry with *provenance* may hold it: Train becomes
    Chat for content fetched through the X API; None and Chat stay."""
    if ai_usage == AIUsage.TRAIN.value and train_refused_for(provenance):
        return AIUsage.CHAT.value
    return ai_usage


def chat_at_most(ai_usage):
    """Train becomes Chat; None and Chat stay."""
    return (AIUsage.CHAT.value if ai_usage == AIUsage.TRAIN.value
            else ai_usage)


def effective_ai_usage(row):
    """The AI usage the training licence reads for *row*: its stored
    ai_usage, Chat at most when the row holds X API or other people's
    input (``unlicensed_input``, #689), so nothing stored at Train before
    the rule reaches the training key. Other rows read as stored."""
    stored = getattr(row, 'ai_usage', AIUsage.NONE.value)
    return chat_at_most(stored) if unlicensed_input(row) else stored


def shown_ai_usage(row):
    """The AI usage the screens show for *row*: Chat at most when Train is
    refused for it (X API input), else as stored. A row marked only for
    other people's writing shows the user's own setting (Peter,
    2026-09-29: such replies may be set to Train); the licence still
    reads it as Chat."""
    stored = getattr(row, 'ai_usage', AIUsage.NONE.value)
    return chat_at_most(stored) if train_refused(row) else stored


def node_train_licence_clause(node_model):
    """SQL for "this node is licensed for training": at Train, not
    fetched through the X API and not written from unlicensed input. The
    query form of effective_ai_usage for nodes."""
    return and_(
        node_model.ai_usage == AIUsage.TRAIN.value,
        func.coalesce(node_model.provenance, "").notin_(X_API_PROVENANCES),
        node_model.unlicensed_input.is_(None),
    )


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

# Node.provenance of the tweets a pre-fill brought in: Loore fetched them
# from the Community Archive or the X API (routes/import_data). A user's
# own archive upload is not one of them.
PREFILL_PROVENANCES = ("prefill_ca", "prefill_x")


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


def withheld_prefill_clauses(user_id):
    """SQL clauses for a query of the nodes an AI read takes from a user's
    writing as a whole (profile builds and their gates, intentions, the
    recent-context summary, the writing a reply or poll draft reads as
    background, the AI's archive search in utils/embeddings): after the
    user said no to the tweet seed, on the card or later in Settings
    (#662), the tweets a pre-fill brought in are left out. They stay in
    the user's Loore; only these reads skip them.

    An empty list for every other account, so their queries do not
    change. The answer is read from the database at each call, so a no
    given while a job runs counts from the job's next query. A thread
    the user opens and replies to still reads its own entries, a tweet
    among them."""
    from sqlalchemy import func
    from backend.extensions import db
    from backend.models import Node, User
    consent = db.session.query(User.prefill_consent).filter(
        User.id == user_id).scalar()
    if consent != "no":
        return []
    return [func.coalesce(Node.provenance, "").notin_(PREFILL_PROVENANCES)]


def get_default_privacy_settings() -> dict:
    """Get the default privacy settings for new nodes.

    Returns:
        Dictionary with default privacy_level and ai_usage
    """
    return {
        'privacy_level': PrivacyLevel.PRIVATE,
        'ai_usage': AIUsage.NONE
    }
