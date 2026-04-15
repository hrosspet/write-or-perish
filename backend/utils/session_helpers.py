"""Shared helpers for voice/conversation session routes."""

from flask_login import current_user
from backend.models import Node
from backend.utils.llm_nodes import create_llm_placeholder


def ancestors_have_prompt(node, user_id, prompt_key):
    """Walk up ancestors and check if any node links to a UserPrompt with this key."""
    current = node
    while current:
        prompt = current.get_artifact("prompt")
        if prompt is not None and prompt.prompt_key == prompt_key:
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
