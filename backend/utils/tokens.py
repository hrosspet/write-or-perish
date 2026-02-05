"""
Token estimation utilities for LLM context management.
"""
from flask import current_app


def approximate_token_count(text: str) -> int:
    """
    Approximate token count for a text string.
    Uses a simple heuristic: ~4 characters per token.

    This is a rough estimate that works reasonably well for most LLM tokenizers.
    """
    if not text:
        return 0
    return len(text) // 4


def get_model_context_window(model_id: str) -> int:
    """
    Get the context window size for a given model.

    Args:
        model_id: Model identifier (e.g., "gpt-5", "claude-sonnet-4.5")

    Returns:
        Context window size in tokens
    """
    model_config = current_app.config["SUPPORTED_MODELS"].get(model_id)
    if model_config:
        return model_config.get("context_window", 200000)
    return 200000


def calculate_max_export_tokens(
    model_id: str,
    reserved_tokens: int = 0,
    buffer_percent: float = None
) -> int:
    """
    Calculate the maximum tokens available for user export content.

    This accounts for:
    - Model's context window limit
    - Reserved tokens (for conversation, prompts, etc.)
    - Safety buffer (percentage of context window)

    Args:
        model_id: Model identifier
        reserved_tokens: Tokens already used or reserved (conversation, prompt template, etc.)
        buffer_percent: Override for buffer percentage (default from config)

    Returns:
        Maximum tokens available for export content
    """
    context_window = get_model_context_window(model_id)

    if buffer_percent is None:
        buffer_percent = current_app.config.get("PROFILE_CONTEXT_BUFFER_PERCENT", 0.07)

    buffer_tokens = int(context_window * buffer_percent)
    max_tokens = context_window - reserved_tokens - buffer_tokens

    return max(0, max_tokens)
