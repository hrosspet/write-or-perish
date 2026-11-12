"""Circles: entries shared with people their author chose.

A circle is a named list of people owned by one user (Circle,
CircleMember). An entry is shared with circles (NodeCircle) and/or with
people named for that entry only (NodeReader). Who reads a shared entry
is worked out from those rows every time it is opened
(privacy.CircleSets), so removing a person, deleting a
circle or taking the entry back closes it at once.

Replies written into a shared thread point at the shared entry
(Node.audience_root_id) and are read by exactly its audience: the author
and the readers. Only the entry's author changes the audience.

Nobody but the owner sees a circle or its name: a reader sees the people
who can read an entry, never which circle they are in.

This module holds what the routes share: the flag, the exact-name lookup,
the audience of an entry, setting and clearing it, the note a reader gets
and the per-reader mute of a sender. Ids, usernames and timestamps only:
nothing here decrypts content.
"""
import logging
from datetime import datetime

from sqlalchemy import and_, func, or_, select

from backend.extensions import db
from backend.models import (
    Circle, CircleMember, CircleMute, Node, NodeCircle, NodeReader, User,
    UserNotification,
)
from backend.utils.privacy import (
    PrivacyLevel, circles_enabled_for, forget_circle_access,
)

logger = logging.getLogger(__name__)

CIRCLES = PrivacyLevel.CIRCLES.value
NOTE_TYPE = "circle_share"

# ── Heuristics (named in the PR) ────────────────────────────────────────
# Limits against abuse and runaway lists, far above ordinary use.
MAX_CIRCLES_PER_USER = 50
MAX_MEMBERS_PER_CIRCLE = 150
MAX_ONE_OFF_NAMES = 20
MAX_CIRCLE_NAME_CHARS = 60
# Exact-name lookups that found nobody, per user per hour, before the
# lookup answers 429: enough for typos, too few to probe for names.
LOOKUP_MISSES_PER_HOUR = 20
LOOKUP_WINDOW_SECONDS = 3600

NO_SUCH_NAME = "No one on Loore has that name"
NO_READERS = ("Nobody could read this entry yet. Choose a circle with "
              "people in it, or name a person.")
REPLY_AUDIENCE = ("Replies in a shared thread are read by the people the "
                  "entry is shared with.")
ONLY_AUTHOR = "Only the author of the entry can change who reads this thread."
ONLY_ME_BRANCH = ("This stays Only me: the node it answers is not read by "
                  "the people of this thread.")


class AudienceError(Exception):
    """A sharing request that is refused, with the plain message the
    form shows. ``code`` lets a client tell the cases apart."""

    def __init__(self, message, code="circle_audience", status=400,
                 unknown=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.unknown = unknown or []

    def response(self):
        from flask import jsonify
        body = {"error": self.message, "code": self.code}
        if self.unknown:
            body["unknown"] = self.unknown
        return jsonify(body), self.status


def circles_enabled(user) -> bool:
    return user is not None and circles_enabled_for(getattr(user, "id", None))


def circles_user_fields(user):
    """The current-user payload's flag: every Circles screen and the
    Circles choice in the pickers key off it."""
    return {"circles_enabled": circles_enabled(user)}


# ── People ──────────────────────────────────────────────────────────────

def clean_name(raw):
    """A typed Loore name without the leading @ and surrounding space."""
    return (raw or "").strip().lstrip("@").strip()


def split_names(raw):
    """Names typed into the "for this entry only" field: a list, or one
    string separated by commas or spaces."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    names, seen = [], set()
    for item in raw:
        name = clean_name(item if isinstance(item, str) else "")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def find_person(username, asking_user_id=None):
    """The account with exactly this Loore name (case ignored), or None.
    None also for anyone who cannot be shared with: the asker, AI and
    system accounts, accounts that are deleted, deactivated or not
    approved, and accounts without Circles. The caller answers every
    None the same way, so the answer says nothing about which it was."""
    from backend.utils.system_accounts import SYSTEM_USERNAMES
    name = clean_name(username)
    if not name or len(name) > 64:
        return None
    rows = User.query.filter(func.lower(User.username) == name.lower()).all()
    user = next((u for u in rows if u.username == name),
                rows[0] if len(rows) == 1 else None)
    if user is None or user.id == asking_user_id:
        return None
    if (user.username in SYSTEM_USERNAMES
            or (user.twitter_id or "").startswith("llm-")
            or user.deleted_at is not None
            or user.deactivated_at is not None
            or not user.approved
            or not circles_enabled(user)):
        return None
    return user


def _miss_key(user_id):
    return f"loore:circle-lookup-miss:{user_id}"


def lookup_blocked(user_id) -> bool:
    """The user has used up the hour's failed name lookups. Without
    Redis the limit is not enforced (logged once per call site)."""
    try:
        from backend.utils.run_locks import _client
        value = _client().get(_miss_key(user_id))
        return value is not None and int(value) >= LOOKUP_MISSES_PER_HOUR
    except Exception as e:  # noqa: BLE001
        logger.warning("circle lookup limit not read (%s)", type(e).__name__)
        return False


def record_lookup_miss(user_id):
    try:
        from backend.utils.run_locks import _client
        client = _client()
        key = _miss_key(user_id)
        if client.incr(key) == 1:
            client.expire(key, LOOKUP_WINDOW_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("circle lookup miss not counted (%s)",
                       type(e).__name__)


def resolve_names(names, asking_user_id, count_misses=True):
    """(users, unknown names) for typed Loore names."""
    users, unknown = [], []
    for name in split_names(names):
        user = find_person(name, asking_user_id)
        if user is None:
            unknown.append(name)
            if count_misses:
                record_lookup_miss(asking_user_id)
        elif user.id not in {u.id for u in users}:
            users.append(user)
    return users, unknown


# ── Circles ─────────────────────────────────────────────────────────────

def own_circle(circle_id, user_id):
    """The user's own circle, or None (someone else's looks the same as
    one that does not exist)."""
    try:
        circle_id = int(circle_id)
    except (TypeError, ValueError):
        return None
    return Circle.query.filter_by(id=circle_id, user_id=user_id).first()


def circle_members(circle_ids):
    """{circle_id: [(CircleMember, User)]} ordered by name, in one query."""
    out = {cid: [] for cid in circle_ids}
    if not out:
        return out
    rows = (db.session.query(CircleMember, User)
            .join(User, User.id == CircleMember.user_id)
            .filter(CircleMember.circle_id.in_(list(out)),
                    User.deleted_at.is_(None))
            .order_by(func.lower(User.username)).all())
    for member, user in rows:
        out[member.circle_id].append((member, user))
    return out


def _alive_shared_entry():
    return (Node.privacy_level == CIRCLES, Node.deleted_at.is_(None),
            Node.audience_root_id == Node.id)


def shared_entry_counts(circle_ids):
    """{circle_id: number of entries now shared with it}."""
    if not circle_ids:
        return {}
    return dict(
        db.session.query(NodeCircle.circle_id, func.count(NodeCircle.id))
        .join(Node, Node.id == NodeCircle.node_id)
        .filter(NodeCircle.circle_id.in_(list(circle_ids)),
                *_alive_shared_entry())
        .group_by(NodeCircle.circle_id).all())


def delete_circle(circle):
    """Delete a circle: its members and its shares go with it. An entry
    shared only with this circle is then read by nobody but its author."""
    NodeCircle.query.filter_by(circle_id=circle.id).delete(
        synchronize_session=False)
    CircleMember.query.filter_by(circle_id=circle.id).delete(
        synchronize_session=False)
    db.session.delete(circle)
    forget_circle_access()


# ── The audience of an entry ────────────────────────────────────────────

def is_shared_entry(node) -> bool:
    """*node* is an entry its author shared (it holds the audience)."""
    return (node is not None and node.privacy_level == CIRCLES
            and node.audience_root_id is not None
            and node.audience_root_id == node.id)


def shared_entry_of(node):
    """The shared entry whose audience reads *node*, while it is shared
    (alive and still "circles"); None for anything else."""
    if (node is None or node.privacy_level != CIRCLES
            or node.audience_root_id is None):
        return None
    entry = (node if node.audience_root_id == node.id
             else db.session.get(Node, node.audience_root_id))
    if (entry is None or entry.deleted_at is not None
            or not is_shared_entry(entry)):
        return None
    return entry


def entry_owner_id(entry):
    return entry.human_owner_id or entry.user_id


def reader_ids(entry_id):
    """Ids of the people who read the entry as its audience now: named
    for it, or members of a circle it is shared with (after their "Only
    new entries" cut-off). Not the author. Mutes are not applied: who
    hid whom is nobody else's to see."""
    named = select(NodeReader.user_id.label("uid")).where(
        NodeReader.node_id == entry_id)
    members = (
        select(CircleMember.user_id.label("uid"))
        .join(NodeCircle, NodeCircle.circle_id == CircleMember.circle_id)
        .where(NodeCircle.node_id == entry_id,
               or_(CircleMember.entries_from.is_(None),
                   NodeCircle.shared_at >= CircleMember.entries_from)))
    both = named.union(members).subquery()
    return {uid for (uid,) in db.session.execute(select(both.c.uid))}


def readers_of(entry):
    """The audience as accounts, ordered by name. Accounts that are
    deleted (or in their grace period) or without Circles read nothing
    and are not listed."""
    ids = reader_ids(entry.id) - {entry_owner_id(entry)}
    if not ids:
        return []
    users = (User.query.filter(User.id.in_(ids), User.deleted_at.is_(None))
             .order_by(func.lower(User.username)).all())
    return [u for u in users if circles_enabled(u)]


def audience_for_viewer(node, viewer_id):
    """What the banner, the icon's list and the reply dialog show for a
    shared node: the author and the readers by name. None when the node
    is not shared or the viewer is neither. For the author (of the entry
    itself) also the circles and names it is shared with, to seed the
    picker; nobody else gets a circle's id or name."""
    entry = shared_entry_of(node)
    if entry is None:
        return None
    owner_id = entry_owner_id(entry)
    readers = readers_of(entry)
    is_author = viewer_id == owner_id
    if not is_author and viewer_id not in {u.id for u in readers}:
        return None
    owner = db.session.get(User, owner_id)
    data = {
        "entry_id": entry.id,
        "author": owner.username if owner else None,
        "is_author": is_author,
        "readers": [u.username for u in readers],
        "is_entry": entry.id == node.id,
        # May a model read the entry (an AI reply in the thread)?
        "entry_ai_usage": entry.ai_usage,
    }
    if is_author and entry.id == node.id:
        data["circle_ids"] = [cid for (cid,) in db.session.query(
            NodeCircle.circle_id).filter_by(node_id=entry.id)
            .order_by(NodeCircle.circle_id)]
        data["usernames"] = [name for (name,) in db.session.query(
            User.username).join(NodeReader, NodeReader.user_id == User.id)
            .filter(NodeReader.node_id == entry.id)
            .order_by(func.lower(User.username))]
    return data


def resolve_audience(user, circle_ids, usernames):
    """(circles, people) the user asked to share with. Raises
    AudienceError for a circle that is not theirs, a name nobody has, or
    too many names."""
    circles = []
    for cid in circle_ids or []:
        circle = own_circle(cid, user.id)
        if circle is None:
            raise AudienceError("That circle no longer exists.",
                                code="circle_not_found")
        if circle.id not in {c.id for c in circles}:
            circles.append(circle)
    names = split_names(usernames)
    if len(names) > MAX_ONE_OFF_NAMES:
        raise AudienceError(
            f"You can name up to {MAX_ONE_OFF_NAMES} people for one entry. "
            "For more, make a circle.", code="circle_too_many_names")
    if names and lookup_blocked(user.id):
        raise AudienceError(
            "Too many names that nobody has. Try again in an hour.",
            code="circle_lookup_limit", status=429)
    people, unknown = resolve_names(names, user.id)
    if unknown:
        raise AudienceError(
            NO_SUCH_NAME + ": " + ", ".join("@" + n for n in unknown),
            code="circle_unknown_name", unknown=unknown)
    return circles, people


def preview_readers(circles, people, owner_id, entry=None):
    """Usernames of everyone who would read *entry* (None: a new entry)
    shared with *circles* and *people* now, ordered by name: exactly who
    reader_ids gives once it is saved. A circle the entry is already
    shared with keeps the time of that share (set_audience), so a member
    added to it since with "Only new entries" does not read the entry
    and is not named; a circle newly chosen is shared from now on."""
    ids = {u.id for u in people}
    if circles:
        cids = [c.id for c in circles]
        shared_at = {}
        if entry is not None and entry.id is not None:
            shared_at = dict(db.session.query(
                NodeCircle.circle_id, NodeCircle.shared_at).filter(
                NodeCircle.node_id == entry.id,
                NodeCircle.circle_id.in_(cids)))
        for cid, uid, entries_from in db.session.query(
                CircleMember.circle_id, CircleMember.user_id,
                CircleMember.entries_from).filter(
                CircleMember.circle_id.in_(cids)):
            since = shared_at.get(cid)
            if since is None or entries_from is None or since >= entries_from:
                ids.add(uid)
    ids.discard(owner_id)
    if not ids:
        return []
    users = (User.query.filter(User.id.in_(ids), User.deleted_at.is_(None))
             .order_by(func.lower(User.username)).all())
    return [u.username for u in users if circles_enabled(u)]


def parent_shared_entry(node):
    """The shared entry of the thread *node* was written into (its
    parent is in a shared thread), or None."""
    if node.parent_id is None:
        return None
    entry = shared_entry_of(db.session.get(Node, node.parent_id))
    # The discussion leading up to a shared entry may be shared with it
    # (share_thread_path): its parent then points at the entry itself,
    # which does not make the entry a reply in its own thread.
    if entry is not None and entry.id == node.id:
        return None
    return entry


# How far above an entry the discussion leading up to it is followed.
THREAD_PATH_HOPS = 500
# The most of the requester's archive a {user_export} in a prompt someone
# else shared reads, on every plan (Pro included): Peter, 2026-11-11,
# "100k ... should be fine given the explicit question & acknowledgement".
# A larger max_export_tokens in such a prompt is cut to it. INTRODUCED
# HEURISTIC.
OTHERS_PROMPT_EXPORT_TOKENS = 100_000
PATH_REPLY_IN_PROGRESS = (
    "An AI reply in the discussion above this entry is still being "
    "written. Share the discussion once it is done.")
# A node someone wrote into another person's shared thread is read by its
# people only while its writer reads the thread too.
THREAD_CLOSED = (
    "This thread is no longer shared with you, so what you wrote in it "
    "can't be changed or shared with its people. You can take it back to "
    "Only me.")
PROMPT_ALONE = (
    "A thread's prompt is shared together with the discussion below it. "
    "Share an entry of the thread and choose to share the discussion "
    "leading up to it.")


def thread_path(node, user_id):
    """The discussion leading up to *node*: the nodes above it that are
    *user_id*'s own (their entries, the thread's prompt, the AI replies
    they asked for), nearest first, up to the thread's start or the
    first node that is someone else's. Only the path, no side branch."""
    path = []
    current = node.parent if node is not None else None
    for _ in range(THREAD_PATH_HOPS):
        if (current is None
                or (current.human_owner_id or current.user_id) != user_id):
            break
        path.append(current)
        current = current.parent
    return path


def path_is_shared(node, path):
    """Every alive node of *path* is read by *node*'s audience."""
    alive = [n for n in path if n.deleted_at is None]
    return bool(alive) and is_shared_entry(node) and all(
        n.privacy_level == CIRCLES and n.audience_root_id == node.id
        for n in alive)


def share_thread_path(node, user, data):
    """``share_path`` of a sharing request for the user's own entry
    *node* ("Share also the discussion leading up to this node?";
    Peter, 2026-11-11). True: every node of the path from the thread's
    start down to *node* (the prompt the thread starts from included) is
    read by *node*'s audience: it points at *node*, like a reply below
    it. False: the path is taken back to Only me. Without the flag
    nothing above the entry changes: sharing an entry shares only it.
    A node of the path that is public stays public."""
    if "share_path" not in data or not is_shared_entry(node):
        return
    if not data.get("share_path"):
        release_thread_path(node)
        return
    from backend.utils.llm_nodes import reply_in_progress
    path = thread_path(node, user.id)
    # An AI reply is shared once it is written (Peter, 2026-11-12): the
    # discussion waits while one of its replies is still being written.
    if any(above.deleted_at is None and reply_in_progress(above)
           for above in path):
        raise AudienceError(PATH_REPLY_IN_PROGRESS, code="reply_in_progress",
                            status=409)
    for above in path:
        if above.deleted_at is not None:
            continue
        if above.privacy_level == PrivacyLevel.PUBLIC.value:
            continue
        if above.audience_root_id == above.id:
            clear_audience(above)
        if above.privacy_level != CIRCLES:
            above.set_privacy_level(CIRCLES)
        above.audience_root_id = node.id
    forget_circle_access()


def _back_to_author(node):
    """*node*, the author's own, is read by nobody but her again: Only
    me, no pointer (as before it was shared)."""
    if node.privacy_level == CIRCLES:
        node.set_privacy_level(PrivacyLevel.PRIVATE.value)
    node.audience_root_id = None


def release_thread_path(entry):
    """"Share also the discussion leading up to this node?" unticked:
    the discussion leading up to *entry* that was shared with it goes
    back to the entry's author alone. Each node of the path that points
    at *entry* becomes Only me and stops pointing at it, as before it
    was shared (deleted ones too). Nothing below *entry* changes."""
    if entry is None or entry.id is None:
        return
    released = False
    for above in thread_path(entry, entry_owner_id(entry)):
        if above.audience_root_id == entry.id:
            _back_to_author(above)
            released = True
    if released:
        forget_circle_access()


def release_deleted_entry(entry):
    """*entry*, a shared entry (or one that was shared), is deleted
    (node_deletion.soft_delete_node): every node of its author's that
    points at it (the discussion leading up to it when that was shared,
    her AI replies in the thread, her own replies) goes back to her
    alone, Only me with no pointer. They are her own writing: open to
    her and never closed or deleted with the entry, also after the
    entry's row is purged. Only what other people wrote changes nothing
    here: their AI replies go with the entry
    (node_cleanup.sweep_answers_of_deleted_entries), their typed
    replies stay theirs. One indexed query (audience_root_id)."""
    if entry is None or entry.id is None:
        return
    owner = entry_owner_id(entry)
    nodes = Node.query.filter(
        Node.audience_root_id == entry.id, Node.id != entry.id,
        func.coalesce(Node.human_owner_id, Node.user_id) == owner).all()
    for node in nodes:
        _back_to_author(node)
    if nodes:
        forget_circle_access()


# What each personal placeholder of a prompt reads, in the words the
# dialog before an AI reply uses ("Your AI reads your ...").
_PLACEHOLDER_NAMES = (
    ("{user_profile}", "profile"),
    ("{user_memory}", "memory"),
    ("{user_scratchpad}", "scratchpad"),
    ("{user_intentions}", "intentions"),
    ("{user_todo}", "to-do list"),
    ("{user_recent}", "summary of recent writing"),
    ("{user_recent_raw}", "recent writing, word for word"),
    ("{user_ai_preferences}", "AI preferences"),
    ("{user_artifacts_index}", "list of artifacts"),
    ("{user_export", "whole archive of writing"),
)
# Last in the list: "Your AI reads your profile, memory and anything
# else its tools look up (...) for this reply ...".
TOOLS_NAME = ("anything else its tools look up (your artifacts, to-do list "
              "and writing)")
# The same when the tools are all a prompt asks for (a public agentic
# prompt someone else wrote): "Your AI reads your artifacts, to-do list
# and writing through its tools for this reply ...".
TOOLS_ONLY_NAME = "artifacts, to-do list and writing through its tools"
AI_READS_MARKER = "_circle_ai_reads"
AI_READS_CHANGED_CODE = "circle_ai_reads_changed"
AI_READS_CHANGED = (
    "The prompt of this thread asks for more of your own data than you "
    "agreed to. Check what your AI would read, then ask again.")


def is_shared_prompt_for(node, user_id) -> bool:
    """*node* is a prompt someone else shared in a circle: in a reply
    *user_id* asks for in the thread, its personal placeholders are
    filled with *user_id*'s own data."""
    return (node.privacy_level == CIRCLES
            and node.audience_root_id is not None
            and (node.human_owner_id or node.user_id) != user_id
            and node.is_system_prompt)


def others_prompts(chain, user_id):
    """The prompt nodes of *chain* someone else wrote that act for
    *user_id* in a reply in a shared thread, however the user reads
    them:

    * a prompt shared with the user in a circle (is_shared_prompt_for):
      its personal placeholders are filled with the user's own data;
    * any agentic prompt someone else wrote, shared or public: its tools
      act on the user's account (their memory, their to-do list).

    Which of them need the user's agreement is decided from their text
    (agreement_prompts). *chain*: the nodes the reply is built from,
    root first, without the ones whose ai_usage keeps AI out
    (privacy.reply_context_nodes, then that filter). A deleted prompt
    acts in no way (privacy.acting_prompts, the rule the task's tools
    follow too)."""
    from backend.utils.privacy import acting_prompts
    from backend.utils.session_helpers import chain_has_agentic_prompt
    out = []
    for n in acting_prompts(chain, user_id, in_shared_thread=True):
        if (n.human_owner_id or n.user_id) == user_id:
            continue
        if is_shared_prompt_for(n, user_id) or chain_has_agentic_prompt([n]):
            out.append(n)
    return out


def _default_prompt_keys(prompt_key):
    """*prompt_key* and the keys whose default is the same file (Voice
    and Text mode share one): a default of one is a default of the
    other."""
    from backend.utils.prompts import PROMPT_DEFAULTS
    meta = PROMPT_DEFAULTS.get(prompt_key) or {}
    same = {k for k, m in PROMPT_DEFAULTS.items()
            if meta.get("file") and m.get("file") == meta.get("file")}
    return sorted(same | {prompt_key})


def is_default_prompt_text(text, prompt_key) -> bool:
    """*text* is, byte for byte, one of Loore's own default prompts for
    *prompt_key*, unedited (Peter, 2026-11-12): the current default (the
    file in backend/prompts), or an earlier default the database
    recorded. Decided from the text alone, never from what a client
    says.

    Earlier defaults: every prompt version saved since 2026-02-25 (the
    automatic upgrade to a new default, a user's save, a revert, a
    reset) records the SHA-256 of the file default current at that
    moment (UserPrompt.based_on_default_hash, written only by the server,
    from the file). A prompt node holds its version's text, so a node
    started under an earlier default matches that recorded hash. A
    default from before then is not recorded anywhere: such a text
    counts as edited. Whitespace counts: a default changed only in its
    whitespace counts as edited too.

    No content is decrypted here: the hashes are of Loore's files."""
    if not text or not prompt_key:
        return False
    import hashlib
    from backend.models import UserPrompt
    from backend.utils.prompts import default_prompt_hash
    digest = hashlib.sha256(text.encode()).hexdigest()
    keys = _default_prompt_keys(prompt_key)
    if any(default_prompt_hash(k) == digest for k in keys):
        return True
    return db.session.query(UserPrompt.id).filter(
        UserPrompt.prompt_key.in_(keys),
        UserPrompt.based_on_default_hash == digest).first() is not None


def uses_personal_placeholder(text) -> bool:
    """*text* names a placeholder that reads the requester's own data
    (the ones the dialog names, _PLACEHOLDER_NAMES)."""
    return any(placeholder in (text or "")
               for placeholder, _ in _PLACEHOLDER_NAMES)


def needs_agreement(node, text, user_id) -> bool:
    """A prompt of others_prompts, with *text* (the text the reply is
    built from), needs *user_id*'s agreement before a reply in a shared
    thread: it is not one of Loore's defaults unedited
    (is_default_prompt_text; Peter, 2026-11-12: "The dialog should be
    there if the sys prompt is edited, or created from scratch and is
    using any Loore placeholders"), and it fills the requester's data
    (a prompt shared with them that names a personal placeholder) or
    turns tools on (builder's addition: an edited agentic prompt acts on
    the requester's account through its tools, placeholders or not)."""
    from backend.utils.session_helpers import chain_has_agentic_prompt
    if is_default_prompt_text(text, node.get_prompt_key()):
        return False
    if chain_has_agentic_prompt([node]):
        return True
    return is_shared_prompt_for(node, user_id) and uses_personal_placeholder(
        text)


def agreement_prompts(chain, user_id, texts=None):
    """The prompts of others_prompts that a reply for *user_id* in a
    shared thread may run only with *user_id*'s agreement
    (needs_agreement). *texts* (``{node id: text}``): the text each is
    judged by, the one the reply is built from (the completion task
    holds it); without it each node's current text."""
    texts = texts or {}
    out = []
    for n in others_prompts(chain, user_id):
        text = texts[n.id] if n.id in texts else (n.get_content() or "")
        if needs_agreement(n, text, user_id):
            out.append(n)
    return out


def prompts_key(texts):
    """The key of what a set of prompt nodes asks of a requester, from
    ``{node id: text}``: it changes when a prompt is added to the chain
    or its text changes (and with the text, its placeholders and whether
    it turns tools on). "" for no prompt. The caller passes the very text
    the reply is built from (the task holds it, Node.hold_content), so
    the text agreed to is the text sent."""
    import hashlib
    parts = sorted(
        f"{nid}:{hashlib.sha256((text or '').encode()).hexdigest()}"
        for nid, text in texts.items())
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def chain_for_reply(node, user_id):
    """The nodes an AI reply in a shared thread under *node* for
    *user_id* is built from, root first, without the ones AI may not
    read: the completion task's walk (privacy.reply_context_nodes)."""
    from backend.utils.privacy import AI_ALLOWED, reply_context_nodes
    return [n for n in reply_context_nodes(node, user_id,
                                           in_shared_thread=True)
            if n.ai_usage in AI_ALLOWED]


def ai_reply_reads(node, user_id):
    """What an AI reply under *node* would read of *user_id*'s own data,
    for the dialog shown before it is generated in a shared thread:
    ``(names, key)``.

    *names*: the personal placeholders of every prompt node in the chain
    the reply is built from that resolves for the user (their own
    prompt, or one shared with them in a circle), by plain name, and the
    tools when a prompt someone else wrote turns them on.

    *key*: prompts_key of agreement_prompts (the prompts someone ELSE
    wrote that fill the user's data or turn tools on and are not one of
    Loore's defaults unedited). The request for the reply carries the
    key the user saw; the route and the task work it out again and
    refuse when it differs, so a prompt edited after the dialog fills
    nothing the user did not agree to. "" when nothing needs agreeing to:
    only the user's own prompts fill anything (own data, own prompt), or
    every prompt someone else wrote that acts here is a Loore default
    unedited (Peter, 2026-11-12). The clients show the dialog only for a
    key.

    The walk is the task's (chain_for_reply). Decrypts only prompt
    nodes, each once."""
    from backend.utils.privacy import acting_prompts
    from backend.utils.session_helpers import chain_has_agentic_prompt
    chain = chain_for_reply(node, user_id)
    texts = {}

    def text_of(n):
        if n.id not in texts:
            texts[n.id] = n.get_content() or ""
        return texts[n.id]

    found = []
    for n in acting_prompts(chain, user_id, in_shared_thread=True):
        mine = (n.human_owner_id or n.user_id) == user_id
        if not (mine or is_shared_prompt_for(n, user_id)):
            continue   # a public prompt of someone else's fills nothing
        text = text_of(n)
        for placeholder, name in _PLACEHOLDER_NAMES:
            if placeholder in text and name not in found:
                found.append(name)
    order = [name for _, name in _PLACEHOLDER_NAMES]
    names = sorted(found, key=order.index)
    others = others_prompts(chain, user_id)
    if others and chain_has_agentic_prompt(others):
        names.append(TOOLS_NAME if names else TOOLS_ONLY_NAME)
    theirs = agreement_prompts(
        chain, user_id, texts={n.id: text_of(n) for n in others})
    return names, prompts_key({n.id: texts[n.id] for n in theirs})


def ai_reads(node, user_id):
    return ai_reply_reads(node, user_id)[0]


class ThreadSource:
    """Where a node was written: the entry's id and the entry's owner,
    None when the entry no longer exists. A source nobody owns is
    nobody's to widen."""

    def __init__(self, entry_id, owner_id):
        self.entry_id = entry_id
        self.owner_id = owner_id

    def is_someone_elses(self, user_id) -> bool:
        return self.owner_id is None or self.owner_id != user_id


def thread_source(node):
    """The thread *node* was written into (a ThreadSource), whether or
    not its entry is shared now or still exists; None for a node that
    was not written into anyone's shared thread. A reply in a shared
    thread, typed or written by a model, keeps this for good
    (Node.audience_root_id is never cleared on it and has no foreign
    key, so it outlives the entry), and a node written under such a
    reply is found through its ancestors. The entry itself is not its
    own source.

    Who may widen a node is decided from this, not from what its direct
    parent is at the moment, and a record that no longer resolves stays
    a record: the node is then someone else's for everyone."""
    from backend.utils.thread_tree import ancestor_chain

    def of(entry_id):
        entry = db.session.get(Node, entry_id)
        return ThreadSource(
            entry_id, entry_owner_id(entry) if entry is not None else None)

    if node.audience_root_id is not None and node.audience_root_id != node.id:
        return of(node.audience_root_id)
    if node.id is None or node.parent_id is None:
        return None
    chain = ancestor_chain(
        node.id, Node.id, Node.parent_id, Node.audience_root_id)
    for row in chain[1:]:
        if row.audience_root_id is not None:
            return of(row.audience_root_id)
    return None


def clear_audience(node):
    """*node* stops being a shared entry (it was taken back to Only me,
    made public, or joined a thread). Replies that pointed at it keep
    pointing: with no shared entry behind them only their own authors
    and the entry's author read them."""
    if node.id is None:
        return
    NodeCircle.query.filter_by(node_id=node.id).delete(
        synchronize_session=False)
    NodeReader.query.filter_by(node_id=node.id).delete(
        synchronize_session=False)
    forget_circle_access()


def set_audience(node, user, circle_ids, usernames, now=None):
    """Make *node* a shared entry read by *circle_ids* (the user's own
    circles) and *usernames* (for this entry only), replacing what it
    was shared with. A circle it was already shared with keeps the time
    of that share, which a member's "Only new entries" cut-off is
    compared with. People who can read it now and could not before get
    their note. Raises AudienceError; refuses an audience nobody is in.
    The caller commits. Returns the readers' usernames."""
    now = now or datetime.utcnow()
    if node.is_system_prompt:
        # A prompt is shared only as part of the whole discussion that
        # leads up to a shared entry (share_thread_path), never alone.
        raise AudienceError(PROMPT_ALONE, code="circle_prompt_node")
    circles, people = resolve_audience(user, circle_ids, usernames)
    if not preview_readers(circles, people, user.id, entry=node):
        raise AudienceError(NO_READERS, code="circle_no_readers")

    was_shared = is_shared_entry(node)
    before = reader_ids(node.id) if was_shared else set()

    if node.privacy_level != CIRCLES:
        node.set_privacy_level(CIRCLES)
    node.audience_root_id = node.id

    have = {row.circle_id: row
            for row in NodeCircle.query.filter_by(node_id=node.id)}
    want = {c.id for c in circles}
    for cid, row in have.items():
        if cid not in want:
            db.session.delete(row)
    for cid in want - have.keys():
        db.session.add(NodeCircle(node_id=node.id, circle_id=cid,
                                  shared_at=now))
    named = {row.user_id: row
             for row in NodeReader.query.filter_by(node_id=node.id)}
    want_people = {u.id for u in people}
    for uid, row in named.items():
        if uid not in want_people:
            db.session.delete(row)
    for uid in want_people - named.keys():
        db.session.add(NodeReader(node_id=node.id, user_id=uid,
                                  created_at=now))
    db.session.flush()
    forget_circle_access()

    # A long entry is saved as a chain of parts: they are the entry, so
    # its readers read all of it.
    from backend.utils.node_split import split_chain_parts
    parts = split_chain_parts(node)
    for part in parts:
        join_thread_audience(part, node)
    # No AI reply joins the audience here: every AI reply is created
    # private and is shared only by its owner, once it is written (Peter,
    # 2026-11-12). An entry created "circles" gets no automatic reply
    # (llm_nodes.auto_generate_refusal), and the user's replies below an
    # older entry join only through "apply to my replies".

    after = reader_ids(node.id)
    note_shared(node, after - before)
    return [u.username for u in readers_of(node)]


def join_thread_audience(node, entry):
    """*node*, a reply in *entry*'s shared thread, is read by the
    thread's audience."""
    if node.privacy_level != CIRCLES:
        node.set_privacy_level(CIRCLES)
    if node.audience_root_id == node.id:
        clear_audience(node)
    node.audience_root_id = entry.id
    forget_circle_access()


def reply_thread_entry(parent, user_id):
    """The shared entry whose thread an AI reply under *parent* for
    *user_id* is written in, or None: *parent* is a node of a thread
    that is shared now (the entry itself, or a reply that points at it,
    whatever the reply's own level), *user_id* can read *parent*, and
    they read the thread as its author or as part of its audience."""
    from backend.utils.privacy import (
        can_user_access_node, in_audience, pointers_may_exist)
    if parent is None or not pointers_may_exist():
        return None
    pointer = parent.audience_root_id
    if pointer is None:
        # A member's own Only-me branch in someone else's shared thread:
        # what they wrote under their own Only-me node there carries no
        # pointer, and its thread is found through its ancestors
        # (thread_source). An AI reply asked for in that branch is a reply
        # in the thread like any other of theirs: Only me, and closed and
        # deleted with their access to the entry. The author's own
        # private branches stay as they are.
        if (parent.privacy_level != PrivacyLevel.PRIVATE.value
                or (parent.human_owner_id or parent.user_id) != user_id):
            return None
        source = thread_source(parent)
        if (source is None or source.entry_id is None
                or not source.is_someone_elses(user_id)):
            return None
        pointer = source.entry_id
    entry = (parent if pointer == parent.id
             else db.session.get(Node, pointer))
    if (entry is None or entry.deleted_at is not None
            or not is_shared_entry(entry)):
        return None
    owner_id = entry_owner_id(entry)
    if not circles_enabled_for(owner_id):
        return None
    if user_id != owner_id and not in_audience(entry.id, user_id):
        return None
    if not can_user_access_node(parent, user_id):
        return None
    return entry


def join_ai_reply(node, entry):
    """*node*, a new AI reply in *entry*'s shared thread, joins the
    thread: its pointer is set for good (where it was written). Every AI
    reply is created "private" (Peter, 2026-11-12), so it is its
    requester's alone until they share it with the thread through Edit
    (apply_requested_sharing). A level other than private, which no
    caller passes now, would be "circles". Never public."""
    if node.privacy_level != PrivacyLevel.PRIVATE.value:
        if node.id is None:
            node.privacy_level = CIRCLES     # no content to move yet
        elif node.privacy_level != CIRCLES:
            node.set_privacy_level(CIRCLES)
    node.audience_root_id = entry.id
    forget_circle_access()


def leave_audience(node):
    """*node* is no longer "circles". An entry of the author's own stops
    being shared: its audience rows go. Every pointer stays: it is read
    for access only while the node is "circles", an entry's pointer at
    itself is how its author keeps reading the replies written into it,
    and a reply's pointer is the record of where it came from
    (thread_source)."""
    if node.audience_root_id is not None and node.audience_root_id == node.id:
        clear_audience(node)
    forget_circle_access()


def thread_write_refusal(source, user):
    """Why *user* may not keep or put a node of theirs in the shared
    thread *source* (a ThreadSource of someone else's) where its people
    read it, as an AudienceError (403 ``circle_thread_closed``), or None.

    Its people read what is written there only while the entry is shared
    and the writer reads the thread as part of its audience (they were
    not removed, did not hide its author, and Circles is on for them).
    Otherwise the writer may take the node back to Only me but not change
    what the thread's people read. Nothing is refused while nobody but
    the writer reads the node: Circles off for the entry's author, or the
    entry gone for good (as before Circles)."""
    from backend.utils.privacy import in_audience
    entry = (db.session.get(Node, source.entry_id)
             if source is not None and source.entry_id is not None else None)
    if entry is None or not circles_enabled_for(entry_owner_id(entry)):
        return None
    if (entry.deleted_at is None and is_shared_entry(entry)
            and circles_enabled(user) and in_audience(entry.id, user.id)):
        return None
    return AudienceError(THREAD_CLOSED, code="circle_thread_closed",
                         status=403)


def apply_requested_sharing(node, user, data, *, old_privacy=None,
                            creating=False):
    """The Circles part of saving a node whose privacy_level has already
    been validated and set from *data*: called by the create and edit
    routes after the node has an id. Raises AudienceError before
    anything of the audience is written. Does nothing while Circles is
    off for the user (a "circles" node then reads as private, as before
    the feature).

    * Not "circles" (any more): the node stops being shared. A reply in
      someone else's shared thread cannot be made public by its writer.
    * "circles" on a reply in a shared thread: it joins the thread's
      audience. A different audience for it is refused.
    * "circles" on anything else: the audience in *data* (``circle_ids``,
      ``circle_usernames``) replaces the entry's. Without one, an entry
      that is already shared keeps its audience; one that would newly
      become "circles" on an edit is refused; a new node is left without
      an audience (private) for the client to share in a second step.
    """
    wants = "circle_ids" in data or "circle_usernames" in data
    names_an_audience = wants and bool(
        data.get("circle_ids") or split_names(data.get("circle_usernames")))
    # What was written into someone else's thread (a reply, a reply to a
    # reply, an AI reply the user asked for) is read by that thread's
    # audience or
    # by its writer alone, whatever its parent is now and whether or not
    # Circles is on for the writer: never public, never another audience.
    from backend.utils.privacy import pointers_may_exist
    source = thread_source(node) if pointers_may_exist() else None
    if source is not None and source.is_someone_elses(user.id):
        if node.privacy_level == PrivacyLevel.PUBLIC.value:
            raise AudienceError(
                "Only the author of the entry can make this thread public.",
                code="circle_thread_public", status=403)
        if node.privacy_level != CIRCLES:
            leave_audience(node)
            return None
        refused = thread_write_refusal(source, user)
        if refused is not None:
            raise refused
        if names_an_audience:
            raise AudienceError(REPLY_AUDIENCE, code="circle_reply_audience")
        _refuse_prompt_alone(node, source.entry_id, old_privacy)
        # A node of the writer's own Only-me branch (under a node the
        # thread's people do not read) stays Only me: it joins the
        # audience only where its parent is read by it. A node that is
        # "circles" already keeps it through an edit of its text.
        if old_privacy != CIRCLES:
            parent = (db.session.get(Node, node.parent_id)
                      if node.parent_id is not None else None)
            in_thread = shared_entry_of(parent)
            if in_thread is None or in_thread.id != source.entry_id:
                raise AudienceError(ONLY_ME_BRANCH,
                                    code="circle_only_me_branch")
        # Back in its thread (when the thread is still there to point at).
        if source.entry_id and node.audience_root_id != node.id:
            node.audience_root_id = source.entry_id
        forget_circle_access()
        return None
    if node.privacy_level != CIRCLES:
        # Also with Circles off for the user: an entry taken back stops
        # being shared.
        if old_privacy == CIRCLES or node.audience_root_id is not None:
            leave_audience(node)
        return None
    if not circles_enabled(user):
        return None
    thread_entry = parent_shared_entry(node)
    if thread_entry is not None:
        if wants and (data.get("circle_ids")
                      or split_names(data.get("circle_usernames"))):
            raise AudienceError(REPLY_AUDIENCE, code="circle_reply_audience")
        _refuse_prompt_alone(node, thread_entry.id, old_privacy)
        join_thread_audience(node, thread_entry)
        return None
    if not wants:
        # A text-only edit keeps what the entry is shared with; a new
        # node waits for its second step. Becoming "circles" on an edit
        # needs an audience.
        if creating or old_privacy == CIRCLES:
            return None
        raise AudienceError(NO_READERS, code="circle_no_readers")
    if (node.human_owner_id or node.user_id) != user.id:
        raise AudienceError(ONLY_AUTHOR, code="circle_not_author", status=403)
    readers = set_audience(node, user, data.get("circle_ids"),
                           data.get("circle_usernames"))
    share_thread_path(node, user, data)
    return readers


def _refuse_prompt_alone(node, entry_id, old_privacy):
    """A prompt node joins a thread's audience only with the whole
    discussion leading up to a shared entry (share_thread_path), never on
    its own: not as a reply in the thread (a Voice or Text-mode session
    started under a shared node keeps its prompt to its starter), and not
    as a prompt below its author's own shared entry. A prompt the path
    share already put in this audience keeps it (an edit of its text)."""
    if not node.is_system_prompt:
        return
    if old_privacy == CIRCLES and node.audience_root_id == entry_id:
        return
    raise AudienceError(PROMPT_ALONE, code="circle_prompt_node")


def join_reply(node, parent, user_id) -> bool:
    """The explicit act that puts a person's new node in a shared
    thread's audience: called by the paths on which a person replies
    into the thread (typed, recorded, uploaded), before the node is
    saved. *node* is "circles" and *parent* is a node of a shared thread
    that *user_id* can read; otherwise nothing happens and the node
    keeps what it was created with. An AI reply joins through
    join_ai_reply (llm_nodes.create_llm_placeholder). No other code path
    makes a new node readable by a thread's audience."""
    from backend.utils.privacy import can_user_access_node
    if (node is None or parent is None or node.privacy_level != CIRCLES
            or node.audience_root_id is not None):
        return False
    entry = shared_entry_of(parent)
    if entry is None or not can_user_access_node(parent, user_id):
        return False
    node.audience_root_id = entry.id
    forget_circle_access()
    return True


def reply_requires_circles(parent, privacy_level, user_id) -> bool:
    """A reply under a shared node must itself be "circles" (it is then
    read by the thread's audience), the rule public threads have. Only
    for a parent *user_id* can read: for any other parent the route's
    own answer for a parent that does not exist stands."""
    from backend.utils.privacy import can_user_access_node
    return (privacy_level != CIRCLES and parent is not None
            and can_user_access_node(parent, user_id)
            and shared_entry_of(parent) is not None)


def circle_reply_required_response():
    from flask import jsonify
    return jsonify({"error": REPLY_AUDIENCE,
                    "code": "circle_reply_required"}), 400


# ── Notes and mutes ─────────────────────────────────────────────────────

def muted_ids(user_id):
    """Ids of the people whose entries *user_id* chose not to see."""
    return {uid for (uid,) in db.session.query(
        CircleMute.muted_user_id).filter_by(user_id=user_id)}


def note_shared(entry, user_ids):
    """One note per reader for a newly shared entry (never a second one
    for the same entry, never to a reader who hid the author, never to
    the author). Adds rows; the caller commits. System text only: the
    author's name is added when the note is shown."""
    owner_id = entry_owner_id(entry)
    user_ids = {u for u in user_ids if u != owner_id}
    if not user_ids:
        return 0
    hid = {uid for (uid,) in db.session.query(CircleMute.user_id).filter(
        CircleMute.muted_user_id == owner_id,
        CircleMute.user_id.in_(user_ids))}
    noted = {uid for (uid,) in db.session.query(
        UserNotification.user_id).filter(
        UserNotification.type == NOTE_TYPE,
        UserNotification.node_id == entry.id,
        UserNotification.user_id.in_(user_ids))}
    added = 0
    for uid in sorted(user_ids - hid - noted):
        db.session.add(UserNotification(
            user_id=uid, type=NOTE_TYPE,
            title="An entry was shared with you.",
            body="You find it under Shared with you, whenever you want to.",
            link=f"/node/{entry.id}",
            node_id=entry.id, actor_user_id=owner_id))
        added += 1
    return added


def mute(user_id, sender):
    """"Don't show <name>'s entries": the sender's shared entries are
    closed to *user_id* and no note about them arrives, until undone.
    The notes already waiting go too. The sender is never told."""
    if CircleMute.query.filter_by(
            user_id=user_id, muted_user_id=sender.id).first() is None:
        db.session.add(CircleMute(user_id=user_id, muted_user_id=sender.id))
    UserNotification.query.filter_by(
        user_id=user_id, type=NOTE_TYPE, actor_user_id=sender.id,
        status="unread").update(
        {"status": "read", "read_at": datetime.utcnow()},
        synchronize_session=False)
    forget_circle_access()


def unmute(user_id, sender_id):
    CircleMute.query.filter_by(
        user_id=user_id, muted_user_id=sender_id).delete(
        synchronize_session=False)
    forget_circle_access()


# ── Deletion ────────────────────────────────────────────────────────────

def node_rows_filters(node_ids):
    """(model, condition) for the rows that hang off nodes: deleted with
    them by every purge."""
    return (
        (NodeCircle, NodeCircle.node_id.in_(node_ids)),
        (NodeReader, NodeReader.node_id.in_(node_ids)),
        (UserNotification, UserNotification.node_id.in_(node_ids)),
    )


def user_rows_filters(user_id, everything=True, before=None):
    """(model, condition, count key) for the rows of Circles that a purge
    of *user_id* deletes, in an order that respects the foreign keys.

    An account deletion (*everything*): all that belongs to or names the
    user: the shares and members of their circles, their memberships in
    other people's circles, the entries they were named for, the mutes
    by and of them, the notes about their entries, their circles.

    "Delete all my content" (not *everything*; Peter, 2026-11-11): only
    what is the user's own, made until the request (*before*; what the
    user makes afterwards stays, #590). The circles they made go, with
    their members and shares; the list of people they hid goes; the
    notes about their entries go with the entries. What other people
    keep about them stays: their place in other people's circles ("it's
    Anna's circle so Ben should stay in it"), the entries other people
    named them for, and other people's choice to hide them."""
    if everything:
        own_circles = select(Circle.id).where(Circle.user_id == user_id)
        return (
            (NodeCircle, NodeCircle.circle_id.in_(own_circles),
             "node_circle"),
            (CircleMember, or_(CircleMember.circle_id.in_(own_circles),
                               CircleMember.user_id == user_id),
             "circle_member"),
            (NodeReader, NodeReader.user_id == user_id, "node_reader"),
            (CircleMute, or_(CircleMute.user_id == user_id,
                             CircleMute.muted_user_id == user_id),
             "circle_mute"),
            (UserNotification, UserNotification.actor_user_id == user_id,
             "circle_note"),
            (Circle, Circle.user_id == user_id, "circle"),
        )

    def until(cond, column):
        return cond if before is None else and_(cond, column <= before)

    circles = until(Circle.user_id == user_id, Circle.created_at)
    own_circles = select(Circle.id).where(circles)
    return (
        (NodeCircle, NodeCircle.circle_id.in_(own_circles), "node_circle"),
        (CircleMember, CircleMember.circle_id.in_(own_circles),
         "circle_member"),
        (CircleMute, until(CircleMute.user_id == user_id,
                           CircleMute.created_at), "circle_mute"),
        (UserNotification,
         until(and_(UserNotification.actor_user_id == user_id,
                    UserNotification.type == NOTE_TYPE),
               UserNotification.created_at), "circle_note"),
        (Circle, circles, "circle"),
    )
