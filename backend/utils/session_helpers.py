"""Shared helpers for voice/conversation session routes."""

from flask_login import current_user
from backend.models import Node
from backend.utils.llm_nodes import create_llm_placeholder

# Keys that share the unified agentic.txt template. Any of these means "an
# agentic session": tools, proposal parsing and mode notes switch on. Read
# through Node.get_prompt_key(), which survives a per-thread prompt edit
# detaching the prompt reference.
AGENTIC_PROMPT_KEYS = ('voice', 'textmode')


def chain_has_agentic_prompt(node_chain):
    """True when any node in *node_chain* was started under an agentic
    prompt key (see AGENTIC_PROMPT_KEYS)."""
    for node in node_chain:
        key = node.get_prompt_key() if hasattr(node, 'get_prompt_key') else None
        if key in AGENTIC_PROMPT_KEYS:
            return True
    return False


def strip_agentic_prompts(node_chain, keep=None):
    """*node_chain* without its agentic prompt nodes (voice / textmode),
    wherever they sit: the root, or one attached mid-thread. The
    messages around them stay, so a reader of the chain still sees the
    sharing that came before an agentic session started under it. A
    node passed as *keep* stays even if agentic. Returns (chain,
    dropped)."""
    dropped = [n for n in node_chain
               if n is not keep
               and (n.get_prompt_key() if hasattr(n, 'get_prompt_key')
                    else None) in AGENTIC_PROMPT_KEYS]
    if not dropped:
        return list(node_chain), []
    return [n for n in node_chain if n not in dropped], dropped


def ancestors_have_prompt(node, user_id, prompt_key):
    """Walk up ancestors and check if any node carries a prompt key that
    matches — the key stamped on the node (kept after a per-thread edit
    detached the prompt reference) or its linked UserPrompt's key.
    `prompt_key` may be a single string or an iterable of strings — any
    match returns True.

    Passing multiple keys lets callers treat different prompt keys that
    share the same template (e.g. 'voice' + 'textmode' both pointing at
    agentic.txt) as equivalent for ancestry purposes, so mode switches
    within an existing agentic thread don't re-attach a fresh prompt
    node.
    """
    if isinstance(prompt_key, str):
        keys = {prompt_key}
    else:
        keys = set(prompt_key)
    current = node
    while current:
        if current.get_prompt_key() in keys:
            return True
        if current.parent_id:
            current = Node.query.get(current.parent_id)
        else:
            break
    return False


def is_llm_node(node):
    return node.node_type == 'llm' or bool(node.llm_model)


def prompt_entry_privacy(user_id, prompt_key, message_privacy):
    """The level a new prompt entry is stored at. A prompt entry shows
    its owner's prompt text and carries pinned versions of their profile
    and other documents.

    A Text-mode one is Public only when its owner's default level on the
    Account page is Public AND the message it starts with
    (*message_privacy*) is created Public; in every other case (default
    Private, a Private or Circles message, an unknown value) it is
    Private (Peter, 2026-11-24: a public message alone does not share
    the prompt, and a private message under a Public default says the
    prompt is meant to be private too; the owner may share it on the
    entry itself). Every other kind (Voice, Glean) is Private, as
    before."""
    if prompt_key != "textmode" or message_privacy != "public":
        return "private"
    from backend.models import User
    user = User.query.get(user_id)
    default = getattr(user, "default_privacy_level", None)
    return "public" if default == "public" else "private"


def attach_agentic_prompt_under(node, user_id, prompt_key, message_privacy,
                                ai_usage):
    """The agentic prompt node a Text / Voice session continuing under
    *node* needs, or None when one already sits above it (any key in
    AGENTIC_PROMPT_KEYS: a mode switch inside an agentic thread reuses
    the prompt that is there). The node is created empty and linked to
    the user's prompt record through attach_context_artifacts, which
    also pins the artifact snapshots its placeholders name; the caller
    hangs the message (created at *message_privacy*) under it and
    commits. The prompt node's level is prompt_entry_privacy's.

    A session starts only under a node of the user's own (for an AI
    reply: one they asked for). Under someone else's node the answer is
    None as well: the message is a plain reply there, directly under
    that node, as one sent to POST /nodes/ is."""
    from backend.utils.privacy import is_node_owner
    if not is_node_owner(node, user_id):
        return None
    if ancestors_have_prompt(node, user_id, AGENTIC_PROMPT_KEYS):
        return None
    from backend.extensions import db
    from backend.utils.prompts import get_user_prompt_record
    from backend.utils.context_artifacts import attach_context_artifacts
    prompt_record = get_user_prompt_record(user_id, prompt_key)
    # Circles: a prompt node is never shared (it carries its writer's
    # prompt and guidance). Under a shared node it is its writer's alone:
    # prompt_entry_privacy gives a "circles" message a private prompt.
    prompt_node = Node(
        user_id=user_id,
        human_owner_id=user_id,
        parent_id=node.id,
        node_type="user",
        privacy_level=prompt_entry_privacy(
            user_id, prompt_key, message_privacy),
        ai_usage=ai_usage,
    )
    db.session.add(prompt_node)
    db.session.flush()
    attach_context_artifacts(
        prompt_node.id, user_id, prompt_record=prompt_record,
    )
    return prompt_node


def create_agentic_root(user_id, prompt_key, message_privacy, ai_usage):
    """The system node that starts a new agentic thread: empty, top-level,
    linked to the user's *prompt_key* prompt record (and the artifact
    snapshots its placeholders name) through attach_context_artifacts.
    Same shape /textmode/start and save-as-node build. The caller hangs
    the first message (created at *message_privacy*) under it and
    commits. The root's level is prompt_entry_privacy's. The public
    listings show the thread by its first public message
    (utils/public_threads)."""
    from backend.extensions import db
    from backend.utils.prompts import get_user_prompt_record
    from backend.utils.context_artifacts import attach_context_artifacts
    prompt_record = get_user_prompt_record(user_id, prompt_key)
    root = Node(
        user_id=user_id,
        human_owner_id=user_id,
        parent_id=None,
        node_type="user",
        privacy_level=prompt_entry_privacy(
            user_id, prompt_key, message_privacy),
        ai_usage=ai_usage,
    )
    db.session.add(root)
    db.session.flush()
    attach_context_artifacts(root.id, user_id, prompt_record=prompt_record)
    return root


def create_llm_placeholder_node(parent_node_id, model_id, requesting_user_id,
                                ai_usage=None, source_mode=None):
    """Create an LLM placeholder node and enqueue the generation task."""
    if ai_usage is None:
        ai_usage = current_user.default_ai_usage
    llm_node, _ = create_llm_placeholder(
        parent_node_id, model_id, requesting_user_id,
        ai_usage=ai_usage,
        source_mode=source_mode,
    )
    return llm_node
