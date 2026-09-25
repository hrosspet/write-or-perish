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


_MAX_ANCESTRY_HOPS = 1000


def is_active_model(model_id):
    """A SUPPORTED_MODELS key that is not deprecated."""
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id)
    return cfg is not None and not cfg.get("deprecated")


def is_read_model(model_id):
    """An active model a read may run on (the "read" flag, #355)."""
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id)
    return cfg is not None and not cfg.get("deprecated") and bool(cfg.get("read"))


def effective_preferred_model(user):
    """The user's saved model while it is still offered, else None. A
    preference for a model deprecated since has no effect anywhere: the
    reply routes, the background tasks and the Account page all fall back
    to DEFAULT_LLM_MODEL (#355)."""
    pref = getattr(user, "preferred_model", None) if user is not None else None
    return pref if pref and is_active_model(pref) else None


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
            Node.ai_usage, max_depth=_MAX_ANCESTRY_HOPS)
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
      2. ("user_preference") ``user.preferred_model`` (active).
      3. ("default") ``DEFAULT_LLM_MODEL`` from the Flask config / env.

    If the closest non-read LLM ancestor is recognized but no longer
    usable (deprecated, or the historical ``gpt-4.5-preview`` legacy id),
    the walk stops there and falls through to ``user.preferred_model``.
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
        if is_active_model(node.llm_model):
            return node.llm_model, "predecessor"
        if node.llm_model in supported or node.llm_model == "gpt-4.5-preview":
            break

    pref = effective_preferred_model(user)
    if pref:
        return pref, "user_preference"
    return (current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6"),
            "default")


def resolve_read_model(anchor_node, chain=None):
    """The model for a read under *anchor_node* (None for a fresh thread)
    when the request named none: the closest earlier read's model while it
    is still a read model ("predecessor"), else READ_DEFAULT_MODEL
    ("default"). Chat replies in between are skipped, so a conversation
    held on Opus never carries into the next read. There is no per-user
    read preference yet (#355)."""
    chain = chain or _Chain(anchor_node)
    for node in chain.llm_replies():
        if node.id not in chain.reads:
            continue
        if is_read_model(node.llm_model):
            return node.llm_model, "predecessor"
        break
    return current_app.config["READ_DEFAULT_MODEL"], "default"


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


def reply_ai_usage(parent_node, user, chain=None, parent_content=None):
    """The ai_usage a new reply under *parent_node* starts with when the
    request names none (#362): the parent's, as everywhere, except that a
    read is looked through. A read's nodes carry 'chat' because they quote
    other people's tweets (ca_feed.FEED_AI_USAGE); that constrains the
    read, not what the user writes under it, and the tweets are kept off
    the training key per request anyway (llm_completion forces chat keys
    on every turn of a read thread). The walk goes up from the parent and
    skips:

      - the read prompts (by key; a PoC-era prompt that carries
        {ca_tweets} in its own text only as the direct parent, whose
        *parent_content* the caller has decrypted anyway, as in
        reply_read_turn);
      - every LLM reply below a read prompt: the read replies, and the
        chat turns about the picks, which are stored as 'chat' for the
        tweets in their context (create_llm_placeholder), not by the
        user's choice;
      - any other read reply the chain knows (a render, picks, a batch).

    The first node left decides. None left (the read is the thread's
    root): the user's default_ai_usage. The reply form (GET /nodes/<id>),
    Voice, Text mode and the streaming path all ask here, so they cannot
    drift apart.
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
    top = max(prompts, default=-1)
    for i, n in enumerate(nodes):
        if i in prompts or n.id in chain.reads:
            continue
        if i < top and _is_llm(n):
            continue
        return n.ai_usage or default
    return default


def create_llm_placeholder(parent_node_id, model_id, human_owner_id,
                           privacy_level="private", ai_usage="chat",
                           placeholder_text="[LLM response generation pending...]",
                           enqueue=True, source_mode=None, meta=None):
    """Create an LLM placeholder node, optionally enqueue generation task.

    Returns (llm_node, task_id) -- task_id is None if enqueue=False.
    *meta* seeds the node's tool_calls_meta (a list of entries) in the
    same commit that creates it, so a marker the task reads (the read
    thread's "_read", routes/read.py) is there before the task can start.

    Raises UserExportValidationError if the parent node's content
    contains a {user_export} placeholder with unrecognized param keys.
    Validation runs BEFORE any DB writes so a misconfigured placeholder
    never produces an orphan LLM node and never incurs LLM API spend.
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
    check_ca_tweets_access(parent_content, owner)

    # The model the reply will actually run on (#355). A read runs only on
    # a read model: the read routes validate the one they are given, and
    # this catches a read reached another way (a reply asked for under a
    # read prompt or read reply from a picker that offers every model, an
    # auto-generated reply under a note typed below the prompt) by the
    # task's own rule (ca_feed.ca_turn). Any other reply never runs on a
    # deprecated model: one sent explicitly (a preference saved before the
    # deprecation, an old tab) is replaced by the chat default.
    chain = _Chain(parent)
    turn = reply_read_turn(parent, meta, parent_content, chain=chain)
    if turn in ("read", "read_again"):
        if not is_read_model(model_id):
            new_model_id = resolve_read_model(parent, chain=chain)[0]
            current_app.logger.info(
                "Reply under node %s is a read: model %s -> %s",
                parent.id, model_id, new_model_id)
            model_id = new_model_id
    elif not is_active_model(model_id):
        new_model_id = resolve_chat_model(parent, owner, chain=chain)[0]
        current_app.logger.info(
            "Reply under node %s: model %s is not offered -> %s",
            parent.id, model_id, new_model_id)
        model_id = new_model_id
    # Every turn of a read thread has the day's tweets or the picks'
    # quotes in its context (llm_completion forces chat keys on it), so
    # the reply is stored as what it is built from, whatever its parent
    # says (#362). 'none' is left as the caller sent it.
    if turn is not None and ai_usage == "train":
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
    if meta:
        llm_node.tool_calls_meta = json.dumps(list(meta))
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
