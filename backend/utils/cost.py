"""
Cost calculation utilities for API usage tracking.

All costs are in microdollars (1 USD = 1,000,000 microdollars) to avoid
floating-point precision issues while supporting sub-cent costs.
"""
from flask import current_app


# Anthropic prompt-caching multipliers on the input price (#187):
# cache reads bill at 0.1x, cache writes (5-min TTL) at 1.25x.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def calculate_llm_cost_microdollars(model_id, input_tokens, output_tokens,
                                    batch=False, cache_read_tokens=0,
                                    cache_write_tokens=0):
    """
    Calculate LLM API cost in microdollars.

    Formula: input_tokens * input_price_per_mtok + output_tokens * output_price_per_mtok
    (the million factors in microdollars and per-million-token pricing cancel out)

    input_tokens must be the UNCACHED portion (what the provider reports in
    usage.input_tokens); cache reads/writes are passed separately and billed
    at their multipliers (#187).

    batch=True applies the Batch API discount (~50% of synchronous pricing,
    issue #173). Long-context multipliers still apply to the per-call input
    before the discount.
    """
    config = current_app.config["SUPPORTED_MODELS"].get(model_id)
    if not config:
        return 0
    input_price = config.get("input_price_per_mtok", 0)
    output_price = config.get("output_price_per_mtok", 0)
    threshold = config.get("long_context_threshold")
    total_prompt = input_tokens + cache_read_tokens + cache_write_tokens
    if threshold and total_prompt > threshold:
        input_price *= config.get("long_context_input_multiplier", 1)
        output_price *= config.get("long_context_output_multiplier", 1)
    cost = (input_tokens * input_price
            + cache_read_tokens * input_price * CACHE_READ_MULTIPLIER
            + cache_write_tokens * input_price * CACHE_WRITE_MULTIPLIER
            + output_tokens * output_price)
    if batch:
        cost *= 0.5
    return round(cost)


def calculate_audio_cost_microdollars(model_id, duration_seconds):
    """
    Calculate audio API cost in microdollars from duration.

    Uses per-minute pricing since TTS/STT APIs don't return token counts.
    """
    pricing = current_app.config.get("AUDIO_PRICING", {}).get(model_id)
    if not pricing:
        return 0
    price_per_min = pricing["price_per_minute_usd"]
    return round(duration_seconds / 60 * price_per_min * 1_000_000)
