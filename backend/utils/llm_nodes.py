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
    READ_FURTHER_MARKER, READ_PROMPT_KEYS, is_read_reply,
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


def _has_read_marker(node):
    try:
        meta = json.loads(node.tool_calls_meta or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return False
    return any(isinstance(m, dict) and m.get("name") == READ_FURTHER_MARKER
               for m in meta)


def is_read_llm_node(node):
    """An LLM node that is (or is about to be) a read: a finished read
    reply (ca_feed.is_read_reply), a pending "Read further" turn (its
    marker), or any reply directly under a read prompt (the first reply
    under the prompt is always the read, _ca_turn). Uses columns and
    relationships only, never the content: it runs on every hop of the
    model walks below."""
    if node is None or not (node.node_type == "llm" or node.llm_model):
        return False
    if _has_read_marker(node):
        return True
    parent = node.parent
    if parent is not None and parent.get_prompt_key() in READ_PROMPT_KEYS:
        return True
    return is_read_reply(node)


def _walk_llm_ancestors(node):
    """Yield the alive LLM nodes from *node* up, nearest first.
    Cycle-safe, at most ``_MAX_ANCESTRY_HOPS`` parents. Skips tombstones:
    a soft-deleted ancestor's ``llm_model`` is still set (only ``content``
    gets wiped at +30d) but the node is meant to be invisible."""
    current = node
    visited = set()
    for _ in range(_MAX_ANCESTRY_HOPS):
        if current is None or current.id in visited:
            return
        visited.add(current.id)
        if (current.deleted_at is None and current.node_type == "llm"
                and current.llm_model):
            yield current
        current = (
            Node.query.get(current.parent_id)
            if current.parent_id else None
        )


def resolve_chat_model(parent_node, user):
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
    for node in _walk_llm_ancestors(parent_node):
        if is_read_llm_node(node):
            continue
        if is_active_model(node.llm_model):
            return node.llm_model, "predecessor"
        if node.llm_model in supported or node.llm_model == "gpt-4.5-preview":
            break

    if user is not None:
        pref = getattr(user, "preferred_model", None)
        if pref and is_active_model(pref):
            return pref, "user_preference"

    return (current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6"),
            "default")


def resolve_read_model(anchor_node):
    """The model for a read under *anchor_node* (None for a fresh thread)
    when the request named none: the closest earlier read's model while it
    is still a read model ("predecessor"), else READ_DEFAULT_MODEL
    ("default"). Chat replies in between are skipped, so a conversation
    held on Opus never carries into the next read. There is no per-user
    read preference yet (#355)."""
    for node in _walk_llm_ancestors(anchor_node):
        if not is_read_llm_node(node):
            continue
        if is_read_model(node.llm_model):
            return node.llm_model, "predecessor"
        break
    return current_app.config["READ_DEFAULT_MODEL"], "default"


def pick_model_for_generation(parent_node, user):
    """Pick the LLM model for an auto-generated (chat) response when the
    caller has not supplied an explicit model. See resolve_chat_model."""
    return resolve_chat_model(parent_node, user)[0]


def reply_is_read(parent, meta=None):
    """Whether a reply created under *parent* will be a read turn in the
    completion task (_ca_turn): a "Read further" request (its marker in
    *meta*), a reply directly under a read reply, or the first reply
    under a read prompt — possibly below notes the user typed under the
    prompt first. The walk passes only user nodes, by prompt key (no
    decryption), and stops at the first LLM node: past a read reply a
    user message makes the turn a chat. A PoC-era prompt (2026-09-13)
    that carries {ca_tweets} in its text is recognised only as the
    direct parent (the caller has its content decrypted already)."""
    if any(isinstance(m, dict) and m.get("name") == READ_FURTHER_MARKER
           for m in (meta or ())):
        return True
    if parent is None:
        return False
    if parent.node_type == "llm" or parent.llm_model:
        return is_read_llm_node(parent)
    current = parent
    visited = set()
    for _ in range(_MAX_ANCESTRY_HOPS):
        if current is None or current.id in visited:
            return False
        visited.add(current.id)
        if current.deleted_at is None:
            if current.node_type == "llm" or current.llm_model:
                return False
            if current.get_prompt_key() in READ_PROMPT_KEYS:
                return True
        current = current.parent
    return False


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

    # Reads run only on read models (#355). The read routes validate the
    # model they are given; this catches a read reached another way — a
    # reply asked for under a read prompt or read reply from a picker that
    # offers every model, or an auto-generated reply under a note typed
    # below the prompt — and gives it the model the Read button would.
    if not is_read_model(model_id) and (
            reply_is_read(parent, meta)
            or CA_TWEETS_PATTERN.search(parent_content or "")):
        read_model_id = resolve_read_model(parent)[0]
        current_app.logger.info(
            "Reply under node %s is a read: model %s -> %s",
            parent.id, model_id, read_model_id)
        model_id = read_model_id

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
