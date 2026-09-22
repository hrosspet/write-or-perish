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


class PayloadLicence:
    """The key decision for one turn, kept current as the payload grows.

    ``determine_api_key_type`` reads the nodes IN the chain. What those
    nodes resolve to is pulled in afterwards — a ``{quote:ID}`` of a node
    marked 'chat', a ``{quote_ext:ID}`` saved reference (someone else's
    writing, which Loore has no licence to train on), what a read_full or
    a semantic_search pulls in mid-turn — and none of it reached that
    decision (#325). Every resolution reports what it added here, and the
    caller reads ``key_type`` right before each provider call: a payload
    that has taken on unlicensed content goes out on the chat key,
    whichever call of the turn it is. The calls before the pull carried
    only licensed content and stay as sent.

    'train' means everything in the payload is licensed for training. One
    piece that is not drops the turn to 'chat', and it never goes back.
    """

    def __init__(self, key_type: str, logger=None, label: str = "payload"):
        self._key_type = key_type
        self._logger = logger
        self._label = label

    @property
    def key_type(self) -> str:
        return self._key_type

    def to_chat(self, reason: str) -> None:
        """Drop to the chat key for the rest of the turn."""
        if self._key_type == AIUsage.CHAT.value:
            return
        if self._logger:
            self._logger.info(
                "%s: %s; chat keys from here on (the chain said %r)",
                self._label, reason, self._key_type)
        self._key_type = AIUsage.CHAT.value

    def note_usage(self, ai_usage, what: str) -> None:
        """One row resolved into the payload, judged by its own setting:
        an artifact, the todo list, a search preview's node."""
        if ai_usage != AIUsage.TRAIN.value:
            self.to_chat(f"{what} is {ai_usage!r}")

    def note_nodes(self, node_ids, what: str = "quoted node") -> None:
        """Nodes resolved into the payload by id — a ``{quote:ID}``, a
        read_full — whoever wrote them. Each node's own ai_usage decides,
        the same rule the chain's nodes are held to."""
        if self._key_type == AIUsage.CHAT.value or not node_ids:
            return
        from backend.models import Node
        row = Node.query.with_entities(Node.id, Node.ai_usage).filter(
            Node.id.in_(set(node_ids)),
            Node.ai_usage != AIUsage.TRAIN.value,
        ).first()
        if row is not None:
            self.to_chat(f"{what} {row[0]} is {row[1]!r}")

    def note_external(self, item_ids, what: str = "saved reference") -> None:
        """Saved references resolved into the payload — other people's
        writing, never licensed for training, whoever saved them."""
        ids = list(item_ids or ())
        if ids:
            self.to_chat(f"{what} {ids[0]} is other people's writing")
