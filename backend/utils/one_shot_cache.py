"""No prompt cache for one-shot OpenAI requests (#803).

A one-shot request is built once, sent once, and never followed by a
request that starts with the same prefix: the external digest, recent
context, intentions, profile windows and merges, poll drafts, the pre-fill
merge. On GPT-5.6 and later OpenAI writes every prompt to its prompt cache
unless the request says otherwise, and bills the written tokens at 1.25x
the input price, in Batch too. Nothing reads what a one-shot request
writes, so it asks for no cache: ``prompt_cache_options`` in explicit mode
with no ``prompt_cache_breakpoint`` in the input. OpenAI's prompt caching
guide: "When no explicit breakpoints are placed, the request does not use
prompt caching or create cache writes." Checked on the live API on
2026-11-24 (GPT-6.2 Sol, Responses and Chat Completions): 0 tokens
written with it, all of the prompt without it.

Each call site says it is one-shot (``one_shot=True`` on
LLMProvider.get_completion, ``"one_shot": True`` on a batch request
dict); the field goes out only to a model that takes it. Conversation
turns, the voice pre-warm, Glean and the todo merge send nothing new:
their next request reads what this one writes.

Anthropic needs nothing: its caching is opt-in (``cache_control``), and
no one-shot request sets it.
"""

# The whole option a one-shot request sends. Explicit mode places no
# breakpoint of its own, and Loore places none.
ONE_SHOT_CACHE_OPTIONS = {"mode": "explicit"}


def takes_prompt_cache_options(model_cfg):
    """Whether OpenAI accepts ``prompt_cache_options`` for this
    SUPPORTED_MODELS entry: GPT-5.6 and later ("Supported for gpt-5.6 and
    later models", Responses and Chat Completions references), the
    entries flagged ``cache_diagnostics``. Older OpenAI models bill no
    cache write and must not get the field; Anthropic never does."""
    return bool(model_cfg) and model_cfg.get("provider") == "openai" \
        and bool(model_cfg.get("cache_diagnostics"))


def _supported_models():
    """SUPPORTED_MODELS from the Flask config inside an app context, else
    the config's default (as utils/safety_identifier reads SECRET_KEY)."""
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            return current_app.config.get("SUPPORTED_MODELS") or {}
    except Exception:  # pragma: no cover — Flask missing or not set up
        pass
    from backend.config import Config
    return Config.SUPPORTED_MODELS


def one_shot_cache_options(model_id, api_model):
    """The ``prompt_cache_options`` of a one-shot batch request, or None.

    ``model_id`` is the request's SUPPORTED_MODELS key and ``api_model``
    the model the request calls. The field goes out only when that entry
    takes it and is the model called: a request whose ``api_model`` is
    not its entry's own gets none."""
    cfg = _supported_models().get(model_id)
    if not takes_prompt_cache_options(cfg) \
            or cfg.get("api_model") != api_model:
        return None
    return dict(ONE_SHOT_CACHE_OPTIONS)
