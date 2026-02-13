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


def reduce_export_tokens(max_export_tokens, actual_tokens, max_tokens,
                         export_content=None):
    """
    Calculate a reduced max_export_tokens after a PromptTooLongError.

    On first attempt (max_export_tokens is None), estimates from the actual
    export content. On subsequent retries, scales down the existing budget.

    Args:
        max_export_tokens: Current budget (None on first attempt)
        actual_tokens: Actual token count reported by the API
        max_tokens: Maximum tokens allowed by the API
        export_content: The export text (required when max_export_tokens is None)

    Returns:
        Reduced max_export_tokens value
    """
    safety_factor = current_app.config.get("RETRY_SAFETY_FACTOR", 0.99)
    reduction = max_tokens / actual_tokens * safety_factor
    if max_export_tokens is None:
        return int(approximate_token_count(export_content) * reduction)
    return int(max_export_tokens * reduction)
