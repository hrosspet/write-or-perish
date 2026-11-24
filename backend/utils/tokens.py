"""
Token estimation utilities for LLM context management.
"""
import logging
import os
import pathlib
import threading

from flask import current_app


# Tokenizer families: which BPE a model bills with (config
# ``tokenizer_family``). Chunk balance never depends on the family; it
# only selects the prior for the real-token cap check and tags the user's
# measured ratio, so a figure measured on one tokenizer is never applied
# to another. (Moved here from tasks/exports for the thread size check,
# #648; exports re-exports them.)
TOKENIZER_FAMILIES = ("claude_old", "claude_new", "o200k")
_FAMILY_BY_PROVIDER = {"openai": "o200k", "anthropic": "claude_new"}

# Prior billed-input tokens per stored unit, by content class and family,
# used for the cap check until the user's first chunk on that family has
# been measured. These are the UPPER ends of the ranges measured
# 2026-09-03 (Loore threads: hrosspet's prod chain; tweets: the exgenesis
# and majamediaco compact exports), so an unmeasured user errs toward one
# extra split, never toward the pricing tier. The claude_new tweet figure
# is estimated from the o200k counts (no local tokenizer).
TOKENS_PER_UNIT_PRIOR = {
    "threads": {"o200k": 1.15, "claude_old": 1.55, "claude_new": 2.2},
    "tweets": {"o200k": 2.4, "claude_old": 2.6, "claude_new": 3.1},
}
# Sanity bounds on a measured ratio, so one degenerate measurement (an
# empty or truncated prompt) cannot collapse the cap check for every
# later chunk. Real corpora measured 0.85–2.4 tokens per unit.
TOKENS_PER_UNIT_MIN = 0.25
TOKENS_PER_UNIT_MAX = 8.0


def tokenizer_family(model_id):
    """The model's tokenizer family (config ``tokenizer_family``), by
    provider when the config does not say — Anthropic defaulting to the
    denser new family, the conservative guess for the cap check."""
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id) or {}
    family = cfg.get("tokenizer_family")
    if family in TOKENIZER_FAMILIES:
        return family
    return _FAMILY_BY_PROVIDER.get(cfg.get("provider"), "claude_new")


# The o200k_base encoding, loaded once per process at its first use
# (local_token_counter): "enc" once loaded; "failed" when that load did
# not finish in time, after which the process estimates instead and never
# tries again; "error" the failure's type, for the one log line.
_O200K = {}
_O200K_LOCK = threading.Lock()

# Heuristic (#717): how long the first count in a process waits for
# tiktoken to load o200k_base. From its cache folder that takes well under
# a second; the first use on a machine downloads the file (about 3.6 MB)
# into the folder, and tiktoken's download has no timeout of its own.
O200K_LOAD_TIMEOUT_SECONDS = 10


def tiktoken_cache_dir(config=None):
    """Where tiktoken keeps its encoding files: TIKTOKEN_CACHE_DIR (the
    variable tiktoken reads itself), else ``<data>/tiktoken`` beside the
    audio, import and Community Archive folders (the
    tasks.imports.snapshot_dir_for pattern). tiktoken's own default is the
    temp folder, which a restart or a clean-up may empty, and then the next
    count downloads the file again."""
    cfg = config if config is not None else current_app.config
    configured = cfg.get("TIKTOKEN_CACHE_DIR")
    if configured:
        return pathlib.Path(configured)
    from backend.utils.twitter_archive import STASH_ROOT
    return STASH_ROOT.parent / "tiktoken"


def _load_o200k(timeout):
    """o200k_base, or None when it did not load within *timeout* seconds
    (tiktoken missing, the file not cached and its host not reachable, a
    download that hangs). The load runs in its own thread, so the count
    waits at most *timeout*; a load that finishes later still leaves the
    encoding for the process's next count."""
    try:
        folder = tiktoken_cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(folder))
    except Exception as e:  # noqa: BLE001 - tiktoken then uses its own default
        _O200K["error"] = f"cache folder: {type(e).__name__}"
    done = threading.Event()

    def load():
        try:
            import tiktoken
            _O200K["enc"] = tiktoken.get_encoding("o200k_base")
        except Exception as e:  # noqa: BLE001 - reported by the caller
            _O200K["error"] = type(e).__name__
        finally:
            done.set()

    threading.Thread(target=load, name="tiktoken-o200k", daemon=True).start()
    if not done.wait(timeout):
        _O200K.setdefault("error", f"not loaded within {timeout} s")
    return _O200K.get("enc")


def _o200k_encoding():
    """The process's o200k_base, loaded at the first call (_load_o200k),
    or None for good after a load that failed: one WARNING line per
    process, and no new attempt on every Glean."""
    enc = _O200K.get("enc")
    if enc is not None or _O200K.get("failed"):
        return enc
    with _O200K_LOCK:
        enc = _O200K.get("enc")
        if enc is None and not _O200K.get("failed"):
            enc = _load_o200k(O200K_LOAD_TIMEOUT_SECONDS)
            if enc is None:
                _O200K["failed"] = True
                logging.getLogger(__name__).warning(
                    "tiktoken's o200k_base is not available (%s); this "
                    "process fits Glean lists and the references digest "
                    "by the estimate",
                    _O200K.get("error") or "unknown")
    return enc


def local_token_counter(model_id):
    """A function text -> tokens that counts on this machine, for a model
    whose tokenizer is o200k (OpenAI's GPT-5.x and later, which bill
    with it: tiktoken's o200k_base, no request), or None: another
    tokenizer family (Anthropic counts only through its endpoint), or
    the encoding not available in this process (_o200k_encoding). Used to
    fit a Glean's tweets to its prompt (#717)."""
    if tokenizer_family(model_id) != "o200k":
        return None
    return o200k_counter()


def o200k_counter():
    """A function text -> o200k_base tokens, counted on this machine, or
    None when the encoding is not available in this process
    (_o200k_encoding). For a limit set by OpenAI's prices whatever the
    model (the Saved References Digest's cap, external_digest)."""
    enc = _o200k_encoding()
    if enc is None:
        return None

    def count(text):
        return len(enc.encode(text or "", disallowed_special=()))
    return count


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
