"""
Cost calculation utilities for API usage tracking.

All costs are in microdollars (1 USD = 1,000,000 microdollars) to avoid
floating-point precision issues while supporting sub-cent costs.
"""
from flask import current_app


def calculate_llm_cost_microdollars(model_id, input_tokens, output_tokens,
                                    batch=False):
    """
    Calculate LLM API cost in microdollars.

    Formula: input_tokens * input_price_per_mtok + output_tokens * output_price_per_mtok
    (the million factors in microdollars and per-million-token pricing cancel out)

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
    if threshold and input_tokens > threshold:
        input_price *= config.get("long_context_input_multiplier", 1)
        output_price *= config.get("long_context_output_multiplier", 1)
    cost = input_tokens * input_price + output_tokens * output_price
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
