"""
API key utilities for Write or Perish.

This module provides functions to select the appropriate API keys based on
privacy/ai_usage settings. Separated from tasks to avoid circular imports.
"""
from backend.utils.privacy import AIUsage


def determine_api_key_type(node_chain: list, logger=None) -> str:
    """
    Determine which API key type to use based on ai_usage settings of nodes.

    The most restrictive setting wins:
    - If ANY user node has ai_usage='chat', use 'chat' keys
    - If ALL user nodes have ai_usage='train', use 'train' keys

    Args:
        node_chain: List of nodes in the conversation chain
        logger: Optional logger for warnings

    Returns:
        'chat' or 'train' indicating which key type to use
    """
    # Only consider user nodes (not LLM responses) for determining key type
    user_nodes = [n for n in node_chain if n.node_type != "llm"]

    if not user_nodes:
        # No user content, default to chat (more restrictive)
        return 'chat'

    # Check if any node has ai_usage='chat' (but not 'train')
    # If so, we must use chat keys
    for node in user_nodes:
        ai_usage = getattr(node, 'ai_usage', AIUsage.NONE)
        if ai_usage == AIUsage.CHAT:
            return 'chat'

    # If we get here, all nodes either have 'train' or 'none'
    # 'none' shouldn't reach LLM calls (handled in frontend), but default to 'chat' if it does
    for node in user_nodes:
        ai_usage = getattr(node, 'ai_usage', AIUsage.NONE)
        if ai_usage == AIUsage.NONE:
            if logger:
                logger.warning(f"Node {node.id} has ai_usage='none' but is in LLM context - using chat keys")
            return 'chat'

    # All nodes have ai_usage='train', safe to use train keys
    return 'train'


def get_api_keys_for_usage(config, key_type: str) -> dict:
    """
    Get the appropriate API keys based on the key type.

    Falls back to legacy single keys if separated keys are not configured.

    Args:
        config: Flask app config
        key_type: 'chat' or 'train'

    Returns:
        Dict with 'openai' and 'anthropic' keys
    """
    if key_type == 'train':
        openai_key = config.get("OPENAI_API_KEY_TRAIN") or config.get("OPENAI_API_KEY")
        anthropic_key = config.get("ANTHROPIC_API_KEY_TRAIN") or config.get("ANTHROPIC_API_KEY")
    else:  # 'chat' or any other case
        openai_key = config.get("OPENAI_API_KEY_CHAT") or config.get("OPENAI_API_KEY")
        anthropic_key = config.get("ANTHROPIC_API_KEY_CHAT") or config.get("ANTHROPIC_API_KEY")

    return {
        "openai": openai_key,
        "anthropic": anthropic_key
    }


def get_openai_chat_key(config) -> str:
    """
    Get the OpenAI API key for chat/audio operations.

    Audio operations (transcription, TTS) always use the CHAT key since they
    don't involve training data - they're purely for user interaction.

    Falls back to legacy single key if CHAT key is not configured.

    Args:
        config: Flask app config

    Returns:
        OpenAI API key string
    """
    return config.get("OPENAI_API_KEY_CHAT") or config.get("OPENAI_API_KEY")
