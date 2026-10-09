import json
"""Shared factory for creating LLM placeholder nodes."""

from flask import current_app

from backend.models import Node, User
from backend.extensions import db
from backend.utils.placeholders import (
    CA_TWEETS_PATTERN, check_ca_tweets_access, check_user_export_plan,
    validate_ca_tweets_placeholders, validate_user_export_placeholders,
)
from backend.utils.ca_feed import (
    FEED_AI_USAGE, READ_FURTHER_MARKER, READ_PROMPT_KEYS, ca_turn,
    read_reply_ids,
)
from backend.utils.privacy import AI_ALLOWED
from backend.utils.client_platform import CLIENT_MARKER, request_client


_MAX_ANCESTRY_HOPS = 1000


# ── No reply where AI may not read (Peter, 2026-10-01) ───────────────────
# A reply sends its thread to a model. It is generated only when every
# node it would send, and the reply itself, has an ai_usage that lets AI
# read it ('chat' / 'train'): the rule the web applies when it switches
# auto-generate off (frontend/src/utils/aiUsage.js contextAllowsAi).
AI_USAGE_NONE_CODE = "ai_usage_none"
REPLY_REFUSED_MESSAGE = (
    "AI usage is set to None here, so Loore keeps this thread away from AI "
    "and doesn't reply.")
VOICE_REFUSED_ACCOUNT_MESSAGE = (
    "Voice mode needs AI to listen and reply. Your Default AI usage is set "
    "to None, so Loore keeps your entries away from AI. You can change it "
    "in Account settings.")
VOICE_REFUSED_THREAD_MESSAGE = (
    "Voice mode needs AI to listen and reply. AI usage in this thread is "
    "set to None, so Loore keeps it away from AI. You can change it when "
    "you edit the thread's entries, and the default for new entries in "
    "Account settings.")


class AIUsageRefused(Exception):
    """A reply would send text to a model that its ai_usage keeps away
    from AI. ``scope`` says whose setting decided it: "thread" (a node
    the reply would read) or "account" (the account's Default AI usage,
    which a new entry would take). HTTP callers answer 403 with
    ``{"error", "code": "ai_usage_none", "scope"}``
    (ai_usage_refused_response; backend/__init__.py registers the same
    answer for any route that lets it escape)."""

    code = AI_USAGE_NONE_CODE

    def __init__(self, message=REPLY_REFUSED_MESSAGE, scope="thread"):
        super().__init__(message)
        self.message = message
        self.scope = scope


def ai_usage_refused_response(exc=None):
    """The 403 answer for a refused reply or voice turn."""
    from flask import jsonify
    exc = exc or AIUsageRefused()
    return jsonify({"error": exc.message, "code": exc.code,
                    "scope": exc.scope}), 403


# ── A read-only model never answers a chat turn (Peter, 2026-10-02) ──────
# A model with "chat": False runs reads only. A reply that is not a read
# and was asked for on one is refused, not moved to the chat default: the
# chat default can be another provider, and "Changing providers is however
# never acceptable", not even as a fallback (Peter, 2026-10-02).
READ_ONLY_MODEL_CODE = "model_read_only"


class ReadOnlyModelRefused(Exception):
    """A reply that is not a read was asked for on a read-only model.
    HTTP callers answer 400 ``{"error", "code": "model_read_only",
    "model"}`` (read_only_model_response; backend/__init__.py registers
    the same answer for any route that lets it escape). Raised before
    anything is written."""

    code = READ_ONLY_MODEL_CODE

    def __init__(self, model_id):
        cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id) or {}
        self.model_id = model_id
        self.model_name = cfg.get("display_name") or model_id
        self.message = (f"{self.model_name} is only for Read. Choose another "
                        "model for replies.")
        super().__init__(self.message)


def read_only_model_response(exc):
    """The 400 answer for a reply refused on a read-only model."""
    from flask import jsonify
    return jsonify({"error": exc.message, "code": exc.code,
                    "model": exc.model_id}), 400


def is_active_model(model_id):
    """A SUPPORTED_MODELS key that is not deprecated."""
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id)
    return cfg is not None and not cfg.get("deprecated")


def is_read_model(model_id):
    """An active model a read may run on (the "read" flag, #355)."""
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id)
    return cfg is not None and not cfg.get("deprecated") and bool(cfg.get("read"))


def is_chat_model(model_id):
    """An active model that anything other than a read may run on: a
    reply, a Voice / Text mode turn, the account preference and the
    background work it drives. ``"chat": False`` makes a model read only
    (Peter, 2026-10-02); absent means True."""
    if not is_active_model(model_id):
        return False
    cfg = current_app.config["SUPPORTED_MODELS"][model_id]
    return bool(cfg.get("chat", True))


def effective_preferred_model(user):
    """The user's saved model while it is still offered for chat, else
    None. A preference for a model deprecated since, or read only, has no
    effect anywhere: the reply routes, the background tasks and the
    Account page all fall back to DEFAULT_LLM_MODEL (#355)."""
    pref = getattr(user, "preferred_model", None) if user is not None else None
    return pref if pref and is_chat_model(pref) else None


def default_model_for(user):
    """The model a user's background work (profile, digest, recent
    context) runs on: their active preference, else DEFAULT_LLM_MODEL."""
    return (effective_preferred_model(user)
            or current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6"))


def _is_llm(node):
    return node.node_type == "llm" or bool(node.llm_model)


def _has_read_marker(node):
    try:
        meta = json.loads(node.tool_calls_meta or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return False
    return any(isinstance(m, dict) and m.get("name") == READ_FURTHER_MARKER
               for m in meta)


class _Chain:
    """*node*'s ancestor chain, nearest first, with what the model rules
    read loaded in bulk: two queries for the chain
    (thread_tree.ancestor_chain), at most one for prompt keys held only
    by a linked prompt, two for the read replies (ca_feed.read_reply_ids)
    — the same count on a 400-node thread as on a 4-node one. A per-level
    walk lazy-loaded the parent, its prompt link and each reply's render
    and picks (~1,000 queries on a 400-node thread, #356 review).

    ``rendered`` are the read replies the completion task counts (a
    render, picks or a batch). ``reads`` adds the reads it has not
    rendered yet — a pending or failed "Read further" (its marker) and
    any reply directly under a read prompt — for the default-model walks,
    where a failed read on Luna must not become the chat default either.
    """

    def __init__(self, node):
        from backend.models import NodeContextArtifact, UserPrompt
        from backend.utils.thread_tree import ancestor_chain
        self.nodes = [] if node is None else ancestor_chain(
            node.id, Node.id, Node.parent_id, Node.node_type, Node.llm_model,
            Node.deleted_at, Node.prompt_key, Node.tool_calls_meta,
            Node.ai_usage, Node.user_id, Node.human_owner_id,
            Node.privacy_level, max_depth=_MAX_ANCESTRY_HOPS)
        self.keys = {n.id: n.prompt_key for n in self.nodes if n.prompt_key}
        linked = [n.id for n in self.nodes if not n.prompt_key]
        if linked:
            self.keys.update(
                db.session.query(NodeContextArtifact.node_id,
                                 UserPrompt.prompt_key)
                .join(UserPrompt, UserPrompt.id == NodeContextArtifact.artifact_id)
                .filter(NodeContextArtifact.node_id.in_(linked),
                        NodeContextArtifact.artifact_type == "prompt")
                .all())
        self.rendered = read_reply_ids(self.nodes)
        self.reads = set()
        for i, n in enumerate(self.nodes):
            if not _is_llm(n):
                continue
            parent = self.nodes[i + 1] if i + 1 < len(self.nodes) else None
            if (n.id in self.rendered or _has_read_marker(n)
                    or (parent is not None and parent.deleted_at is None
                        and self.keys.get(parent.id) in READ_PROMPT_KEYS)):
                self.reads.add(n.id)

    def unreadable(self, user_id):
        """The nearest node a reply for *user_id* would send to the model
        whose ai_usage keeps AI out, else None. The walk is the completion
        task's (_load_node_chain): up from the node while *user_id* can
        see it or its tombstone. A deleted node is passed over: the task
        sends a notice in its place, never its text."""
        from backend.utils.privacy import can_user_see_node_or_tombstone
        for n in self.nodes:
            if not can_user_see_node_or_tombstone(n, user_id):
                return None
            if n.deleted_at is None and n.ai_usage not in AI_ALLOWED:
                return n
        return None

    def llm_replies(self):
        """Alive LLM replies, nearest first. Tombstones are skipped: a
        soft-deleted reply's ``llm_model`` is still set (only ``content``
        gets wiped at +30d) but the node is meant to be invisible."""
        return [n for n in self.nodes
                if n.deleted_at is None and n.node_type == "llm" and n.llm_model]


def resolve_chat_model(parent_node, user, chain=None):
    """The model for a (non-read) reply under *parent_node* when the
    caller supplied none, and where it came from:

      1. ("predecessor") the closest ancestor LLM reply's ``llm_model``,
         skipping reads: a read runs on a read model (Luna), and that
         must not become the default for the conversation around it.
      2. ("user_preference") ``user.preferred_model`` (active, chat).
      3. ("default") ``DEFAULT_LLM_MODEL`` from the Flask config / env.

    If the closest non-read LLM ancestor is recognized but not usable for
    chat (deprecated, read only, or the historical ``gpt-4.5-preview``
    legacy id), the walk stops there and falls through to
    ``user.preferred_model``.
    Walking past it to find an even older active model would silently
    override the user's current account preference. Truly-unknown
    ancestors keep walking — they're typically placeholder rows from data
    migrations.
    """
    supported = current_app.config.get("SUPPORTED_MODELS", {})
    chain = chain or _Chain(parent_node)
    for node in chain.llm_replies():
        if node.id in chain.reads:
            continue
        if is_chat_model(node.llm_model):
            return node.llm_model, "predecessor"
        if node.llm_model in supported or node.llm_model == "gpt-4.5-preview":
            break

    pref = effective_preferred_model(user)
    if pref:
        return pref, "user_preference"
    return (current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6"),
            "default")


def glean_provider(anchor_node, user, chain=None):
    """The provider a glean under *anchor_node* for *user* must stay on:
    the provider of the user's own conversation there — the closest chat
    reply in the thread that was made FOR this user (their own AI reply:
    human owner == user), else the user's preference, else the default.
    Replies made for someone else (another user's AI reply higher up a
    public thread) never count. A user's provider is never switched, not
    even for a read (Peter, 2026-10-02 and 2026-10-09)."""
    from backend.utils.glean import model_provider
    supported = current_app.config.get("SUPPORTED_MODELS", {})
    chain = chain or _Chain(anchor_node)
    user_id = getattr(user, "id", None)
    for node in chain.llm_replies():
        if node.id in chain.reads or user_id is None:
            continue
        if (node.human_owner_id or node.user_id) != user_id:
            continue
        if is_chat_model(node.llm_model):
            return model_provider(node.llm_model)
        if node.llm_model in supported:
            break  # the user's own reply on a model no longer offered
    return model_provider(
        effective_preferred_model(user)
        or current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6"))


def resolve_read_model(anchor_node, chain=None, user=None):
    """The model for a read under *anchor_node* (None for a fresh thread)
    when the request named none: the closest earlier read's model while it
    is still a read model ("predecessor"), else the default. Chat replies
    in between are skipped, so a conversation held on Opus never carries
    into the next read. There is no per-user read preference (#355).

    With *user* (every glean, #435) the read stays on the provider of the
    user's chat model (glean_provider) and the server chooses: for a
    non-admin always that provider's glean model (GLEAN_MODEL_ANTHROPIC /
    GLEAN_MODEL_OPENAI, "provider_default"), whatever an earlier read ran
    on; for an admin an earlier read of the same provider first. Raises
    GleanModelUnavailable when the provider can't be resolved or has no
    glean model: never another provider's, never READ_DEFAULT_MODEL.
    Without a user, READ_DEFAULT_MODEL ("default")."""
    from backend.utils.glean import (
        GleanModelUnavailable, glean_model_for_provider, model_provider,
    )
    chain = chain or _Chain(anchor_node)
    if user is None:
        for node in chain.llm_replies():
            if node.id not in chain.reads:
                continue
            if is_read_model(node.llm_model):
                return node.llm_model, "predecessor"
            break
        return current_app.config["READ_DEFAULT_MODEL"], "default"
    provider = glean_provider(anchor_node, user, chain=chain)
    if not provider:
        raise GleanModelUnavailable(provider)
    if getattr(user, "is_admin", False) is True:
        for node in chain.llm_replies():
            if node.id not in chain.reads:
                continue
            if (is_read_model(node.llm_model)
                    and model_provider(node.llm_model) == provider):
                return node.llm_model, "predecessor"
            break
    return glean_model_for_provider(provider), "provider_default"


def pick_model_for_generation(parent_node, user):
    """Pick the LLM model for an auto-generated (chat) response when the
    caller has not supplied an explicit model. See resolve_chat_model."""
    return resolve_chat_model(parent_node, user)[0]


def reply_read_turn(parent, meta=None, parent_content=None, chain=None):
    """The turn a reply under *parent* will be in the completion task —
    "read", "read_again" or "chat" (ca_feed.ca_turn, the rule the task
    applies) — or None outside a read thread. *meta* is the new reply's
    tool_calls_meta ("Read further" marks its request there). The read
    prompt is found by key; a PoC-era prompt that carries {ca_tweets} in
    its own text (2026-09-13) only as the direct parent, whose
    *parent_content* the caller has decrypted anyway."""
    if parent is None:
        return None
    chain = chain or _Chain(parent)
    if not chain.nodes:
        return None
    ca_node = next((n for n in chain.nodes
                    if n.deleted_at is None
                    and chain.keys.get(n.id) in READ_PROMPT_KEYS), None)
    if ca_node is None and CA_TWEETS_PATTERN.search(parent_content or ""):
        ca_node = chain.nodes[0]
    if ca_node is None:
        return None
    requested = any(isinstance(m, dict) and m.get("name") == READ_FURTHER_MARKER
                    for m in (meta or ()))
    return ca_turn(list(reversed(chain.nodes)), ca_node, chain.nodes[0],
                   chain.rendered, requested=requested)


def read_only_model_refusal(model_id, parent_node=None, new_entry=False):
    """Why a reply may not run on *model_id* (a ReadOnlyModelRefused), or
    None: the check create_llm_placeholder makes, for the routes that
    write the user's entry before they ask for the placeholder, so they
    can refuse before anything is written. *parent_node* is where the
    route writes (None: a new thread). With *new_entry* the reply answers
    a new entry the route adds under *parent_node*: still a read while no
    read reply has answered the prompt, a chat turn after one. A model
    that is not active at all is left to create_llm_placeholder."""
    if is_chat_model(model_id) or not is_active_model(model_id):
        return None
    turn = reply_read_turn(parent_node)
    if new_entry and turn == "read_again":
        turn = "chat"
    if turn in ("read", "read_again"):
        return None
    return ReadOnlyModelRefused(model_id)


def reply_ai_usage(parent_node, user, chain=None, parent_content=None):
    """The ai_usage a new reply under *parent_node* starts with when the
    request names none (#362): the parent's, as everywhere, except that
    the Read's own nodes are looked through. Those are 'chat' by
    construction because they quote other people's tweets
    (ca_feed.FEED_AI_USAGE); that describes them, not what is written
    under them, and the tweets are kept off the training key per request
    anyway (llm_completion). The walk goes up from the parent and skips:

      - the read prompts (by key; a PoC-era prompt that carries
        {ca_tweets} in its own text only as the direct parent, whose
        *parent_content* the caller has decrypted anyway, as in
        reply_read_turn);
      - the read replies: the recommendation nodes that present the
        picks (_Chain.reads — a render, picks or a batch, a "Read
        further" marker, or an LLM reply directly under a read prompt).

    The first node left decides, whatever its value, whether the user
    wrote it or it is an LLM reply after the recommendation (those carry
    the thread's setting like any other reply, 2026-09-29). None left
    (the read is the thread's root): the user's default_ai_usage. The
    reply form (GET /nodes/<id>), Voice, Text mode and the streaming path
    all ask here, so they cannot drift apart.
    """
    default = getattr(user, "default_ai_usage", None) or "none"
    if parent_node is None:
        return default
    chain = chain or _Chain(parent_node)
    nodes = chain.nodes or [parent_node]
    prompts = {i for i, n in enumerate(nodes)
               if chain.keys.get(n.id) in READ_PROMPT_KEYS}
    if not prompts and CA_TWEETS_PATTERN.search(parent_content or ""):
        prompts = {0}
    for i, n in enumerate(nodes):
        if i in prompts or n.id in chain.reads:
            continue
        return n.ai_usage or default
    return default


def reply_refusal(parent_node, user_id, ai_usage=None, chain=None):
    """Why no reply may be generated under *parent_node* for *user_id*
    (an AIUsageRefused), or None when it may: a node the reply would send
    to the model is not AI-readable, or *ai_usage*, the setting the reply
    (or the entry a turn adds above it) would carry, is not. Every path
    that starts a reply asks here or goes through create_llm_placeholder,
    which does."""
    if ai_usage is not None and ai_usage not in AI_ALLOWED:
        return AIUsageRefused()
    if parent_node is None:
        return None
    chain = chain or _Chain(parent_node)
    if chain.unreadable(user_id) is not None:
        return AIUsageRefused()
    return None


def voice_turn_refusal(user, parent_node=None, ai_usage=None):
    """Why a Voice turn may not start or get its reply (an AIUsageRefused
    carrying the Voice screen's message), or None. Voice mode exists to
    send what is said to a model, so a turn that could not get a reply is
    not started (Peter, 2026-10-01).

    - A fresh thread (no *parent_node*): the account's Default AI usage
      and the *ai_usage* the recording carries must both let AI read it
      ("account").
    - Continuing a thread: the reply rule under *parent_node*, with the
      ai_usage the turn's entry takes there (reply_ai_usage): "thread"
      when a node above is not AI-readable, "account" when only the new
      entry would not be (a thread whose only nodes are a read takes the
      account's default)."""
    if parent_node is None:
        if (getattr(user, "default_ai_usage", None) not in AI_ALLOWED
                or (ai_usage is not None and ai_usage not in AI_ALLOWED)):
            return AIUsageRefused(VOICE_REFUSED_ACCOUNT_MESSAGE, "account")
        return None
    chain = _Chain(parent_node)
    if chain.unreadable(user.id) is not None:
        return AIUsageRefused(VOICE_REFUSED_THREAD_MESSAGE, "thread")
    if reply_ai_usage(parent_node, user, chain=chain) not in AI_ALLOWED:
        return AIUsageRefused(VOICE_REFUSED_ACCOUNT_MESSAGE, "account")
    return None


def _read_turn_model(parent, owner, model_id, chain):
    """The model a read turn under *parent* runs on (#435): an admin's
    named read model as named (their own evaluations, any provider);
    for everyone else the server's choice, resolve_read_model for the
    owner — a read model of their own provider, whatever model the
    request carried."""
    from backend.utils.glean import model_provider
    # The admin's choice holds only for a reply of their own under a node
    # of their own: an admin never picks a model for someone else.
    owns_parent = owner is not None and (
        (parent.human_owner_id or parent.user_id) == owner.id)
    if (getattr(owner, "is_admin", False) is True and owns_parent
            and is_read_model(model_id)):
        return model_id
    new_model_id = resolve_read_model(parent, chain=chain, user=owner)[0]
    if new_model_id != model_id:
        current_app.logger.info(
            "Reply under node %s is a read: model %s -> %s (provider %s)",
            parent.id, model_id, new_model_id, model_provider(new_model_id))
    return new_model_id


def _with_live_marker(meta, live):
    """*meta* as a list, with READ_LIVE_MARKER added when *live* (a glean:
    always a live call, #435)."""
    from backend.utils.glean import READ_LIVE_MARKER
    meta = list(meta or [])
    if live and not any(isinstance(m, dict) and m.get("name") == READ_LIVE_MARKER
                        for m in meta):
        meta.append({"name": READ_LIVE_MARKER})
    return meta


def create_llm_placeholder(parent_node_id, model_id, human_owner_id,
                           privacy_level="private", ai_usage="chat",
                           placeholder_text="[LLM response generation pending...]",
                           enqueue=True, source_mode=None, meta=None,
                           client=None, read_live=True):
    """Create an LLM placeholder node, optionally enqueue generation task.

    Returns (llm_node, task_id) -- task_id is None if enqueue=False.
    *meta* seeds the node's tool_calls_meta (a list of entries) in the
    same commit that creates it, so a marker the task reads (the read
    thread's "_read", routes/read.py) is there before the task can start.

    A reply that is a read (a glean, #435) is a live call: it gets the
    READ_LIVE_MARKER, which the task reads, unless *read_live* is False
    (the admin's /read/start experiments, which go through the Batch
    API). It runs on the read model the server chooses for the owner, of
    their own provider (an admin's named read model excepted,
    _read_turn_model), never another provider's.

    *client* ('ios' / 'web', utils/client_platform) is the app the user
    is talking from. It defaults to the current request's; a caller with
    no request (the Voice finalize task) passes the one its request
    recorded. A known client is stamped on the node (CLIENT_MARKER): a
    GitHub issue the turn files gets it as its platform label.

    Raises UserExportValidationError if the parent node's content
    contains a {user_export} placeholder with unrecognized param keys.
    Validation runs BEFORE any DB writes so a misconfigured placeholder
    never produces an orphan LLM node and never incurs LLM API spend.

    Raises AIUsageRefused, also before any write, when a node the reply
    would send to the model, or the reply's own *ai_usage*, is not
    AI-readable (reply_refusal).

    Raises ReadOnlyModelRefused, also before any write, when a reply that
    is not a read asks for a read-only model ("chat": False).
    """
    # Spend-cap guard: a blocked user must never get an LLM placeholder node
    # (and never incur generation spend). Raised before any DB write so no
    # orphan node is created; HTTP callers surface it as a 402 → banner via
    # the SpendCapExceeded error handler. This is the single chokepoint for
    # every placeholder-creating path (textmode, converse, voice, replies).
    from backend.utils.spend import SpendCapExceeded, user_is_capped
    if user_is_capped(human_owner_id):
        raise SpendCapExceeded()

    # Race A guard: lock the parent row and reject if soft-deleted. The
    # locking is what closes the create-vs-soft-delete race under READ
    # COMMITTED — a plain SELECT-then-INSERT can't see the concurrent
    # deleted_at UPDATE in time. See backend/utils/node_deletion.py.
    from backend.utils.node_deletion import ParentDeletedError, lock_node
    parent = lock_node(parent_node_id)
    if parent is None:
        raise ParentDeletedError("Parent node not found")
    if parent.deleted_at is not None:
        raise ParentDeletedError("Parent node has been deleted")

    # No reply where AI may not read: the thread the task would send (the
    # parent and what is above it) and the reply itself. Before anything
    # is decrypted or written.
    chain = _Chain(parent)
    refused = reply_refusal(parent, human_owner_id, ai_usage, chain=chain)
    if refused is not None:
        raise refused

    # Pre-flight: validate any {user_export} placeholders in the parent's
    # content. Misconfigured placeholders previously fell back silently
    # to "no token cap" and cost real $$$ on a single request.
    parent_content = parent.get_content()
    validate_user_export_placeholders(
        parent_content, user_id=human_owner_id,
    )
    validate_ca_tweets_placeholders(
        parent_content, user_id=human_owner_id,
    )
    # Plan gate: an uncapped export in the parent entry is refused here
    # for non-Pro users, before any node exists. A placeholder inherited
    # from an older message or the thread's system prompt is caught by
    # the same rule inside generate_llm_response, where the chain is
    # already decrypted (re-decrypting every ancestor here would add a
    # KMS unwrap per node per turn).
    owner = User.query.get(human_owner_id)
    check_user_export_plan(
        parent_content,
        unrestricted_allowed=bool(owner and owner.has_unrestricted_export),
        user_id=human_owner_id,
    )
    # {ca_tweets} in the parent: a glean's own read prompt for a user who
    # gleans, anything for an admin (#435).
    from backend.utils.glean import is_glean_prompt_node
    check_ca_tweets_access(
        parent_content, owner,
        from_read_prompt=is_glean_prompt_node(parent, owner))

    # The model the reply will actually run on (#355). A read runs only on
    # a read model: the read routes validate the one they are given, and
    # this catches a read reached another way (a reply asked for under a
    # read prompt or read reply from a picker that offers every model, an
    # auto-generated reply under a note typed below the prompt) by the
    # task's own rule (ca_feed.ca_turn). Any other reply never runs on a
    # deprecated or read-only model. A read-only one (an old tab, a client
    # whose picker does not filter on "chat", a direct call) is refused
    # with ReadOnlyModelRefused, before any write: moving the reply to the
    # chat default could move it to another provider, which is never
    # acceptable (Peter, 2026-10-02). A deprecated one (a preference saved
    # before the deprecation) is still replaced by the chat default (#355).
    # This is the one place that knows the turn, so the routes that only
    # check that the model exists stay right for a read turn they carry;
    # routes that write the user's entry first ask read_only_model_refusal
    # before writing.
    turn = reply_read_turn(parent, meta, parent_content, chain=chain)
    is_read_turn = turn in ("read", "read_again")
    if is_read_turn:
        model_id = _read_turn_model(parent, owner, model_id, chain)
    elif is_active_model(model_id) and not is_chat_model(model_id):
        raise ReadOnlyModelRefused(model_id)
    elif not is_chat_model(model_id):
        new_model_id = resolve_chat_model(parent, owner, chain=chain)[0]
        current_app.logger.info(
            "Reply under node %s: model %s is not offered -> %s",
            parent.id, model_id, new_model_id)
        model_id = new_model_id
    # The Read's own recommendation reply (a read or a read further,
    # whichever way it was asked for) presents the picks, quoting the
    # tweets verbatim: it is 'chat' by construction, like the read
    # routes stamp it. A chat turn after it takes the thread's setting
    # like any other reply (#362, 2026-09-29): the tweets it re-sends
    # are kept off the training key per call (llm_completion). 'none'
    # never gets here (reply_refusal above).
    if turn in ("read", "read_again") and ai_usage == "train":
        ai_usage = FEED_AI_USAGE

    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.flush()

    from backend.utils.tokens import approximate_token_count

    llm_node = Node(
        user_id=llm_user.id,
        parent_id=parent_node_id,
        human_owner_id=human_owner_id,
        node_type="llm",
        llm_model=model_id,
        llm_task_status="pending",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        token_count=approximate_token_count(placeholder_text),
    )
    llm_node.set_content(placeholder_text)
    meta = _with_live_marker(meta, is_read_turn and read_live)
    client = client or request_client()
    if client:
        meta.append({"name": CLIENT_MARKER, "client": client})
    if meta:
        llm_node.tool_calls_meta = json.dumps(meta)
    db.session.add(llm_node)
    db.session.commit()

    task_id = None
    if enqueue:
        from backend.tasks.llm_completion import generate_llm_response
        task = generate_llm_response.delay(
            parent_node_id, llm_node.id, model_id, human_owner_id,
            source_mode=source_mode,
        )
        llm_node.llm_task_id = task.id
        db.session.commit()
        task_id = task.id

    return llm_node, task_id
