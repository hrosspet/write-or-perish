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


def _format_token_count(tokens):
    """Format a token count for display: 1500000 → '1.5M', 30000 → '30K'."""
    if not tokens:
        return None
    if tokens >= 1_000_000:
        s = f"{tokens / 1_000_000:.1f}".rstrip('0').rstrip('.')
        return f"{s}M tokens"
    if tokens >= 1_000:
        s = f"{tokens / 1_000:.1f}".rstrip('0').rstrip('.')
        return f"{s}K tokens"
    return f"{tokens} tokens"


def format_date_metadata(covers_start=None, covers_end=None, tokens=None):
    """Return a bracketed date-range line, or empty string if no dates.

    Examples:
        [Covers data through 2026-03-10 (1.5M tokens).]
        [Covers 2026-03-10 to 2026-03-28 (30K tokens).]
    """
    fmt = lambda dt: dt.strftime('%Y-%m-%d')  # noqa: E731
    tok = _format_token_count(tokens)
    tok_part = f" ({tok})" if tok else ""
    if covers_start and covers_end:
        return f"[Covers {fmt(covers_start)} to {fmt(covers_end)}{tok_part}]\n"
    elif covers_end:
        return f"[Covers data through {fmt(covers_end)}{tok_part}]\n"
    return ""


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
