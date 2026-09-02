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
