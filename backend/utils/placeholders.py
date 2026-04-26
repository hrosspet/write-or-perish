"""Helpers for {user_export} placeholder parsing.

Lives in `utils/` so tests can import it without pulling in the full
Celery + LLM-providers chain that comes with `backend.tasks.llm_completion`.
"""

import logging
import re
from urllib.parse import parse_qs

# Pattern for detecting {user_export} with optional URL-style params.
#
# Syntax: {user_export} or {user_export?param=value&param2=value2}
#
# Scope: {user_export} always uses the "engaged_threads" topology — every
# node where the user is author or human_owner_id is an anchor; ancestors
# are climbed up to the root or first inaccessible node, and descendants
# are climbed down. Foreign public threads the user replied to are
# included (climb-up reaches the foreign root). Foreign sibling branches
# the user did not engage with are excluded.
#
# Supported params:
#   keep=oldest             - When truncating to fit the budget, keep the
#                             oldest threads instead of the newest (default).
#                             Useful for tasks that need early/foundational
#                             writing.
#   keep=newest             - Explicit default: keep the newest threads.
#   max_export_tokens=<int> - Initial token budget for the export. If the
#                             prompt still overflows the LLM context, the
#                             retry loop will shrink further from this
#                             ceiling. Non-numeric or negative values are
#                             logged and ignored (treated as no cap). 0
#                             disables the export entirely.
#
USER_EXPORT_PATTERN = re.compile(r"\{user_export(\?[^}]*)?\}")

_default_logger = logging.getLogger(__name__)


def parse_placeholder_params(match_str):
    """Parse URL-style params from a placeholder like {user_export?keep=oldest}.

    Whitespace around keys and values is stripped so a typo like
    `{user_export?keep=newest& max_export_tokens=10000}` (note the space
    after `&`) recovers as `{"keep": "newest", "max_export_tokens":
    "10000"}` instead of producing a key of " max_export_tokens" that
    callers silently miss.
    """
    if '?' in match_str:
        qs = match_str.split('?', 1)[1].rstrip('}')
        return {
            k.strip(): v[0].strip()
            for k, v in parse_qs(qs).items()
        }
    return {}


# Recognized parameter keys for the {user_export} placeholder. Used by
# the handler to log a warning when an unrecognized key appears
# (catches typos like `max-export-tokens` that whitespace stripping
# alone wouldn't fix).
#
# Future param-bearing placeholders: the silent-fallback bug class
# (typoed key → ignored → default behavior, which once shipped 1M+
# tokens to the LLM at $5.71/request) is general, not specific to
# {user_export}. When you add a new parameterized placeholder, grow a
# sibling pattern: a separate KNOWN_KEYS frozenset + a validator
# function that mirrors validate_user_export_placeholders, called from
# create_llm_placeholder. Don't just append the new placeholder's keys
# to this set — that would also let `{user_export?your_key=...}`
# validate as "known", reintroducing the bug for the original
# placeholder. Today's bare-string placeholders ({user_profile},
# {user_todo}, {user_recent}, etc.) don't take params and aren't
# vulnerable, but the next param-bearing one will be.
USER_EXPORT_KNOWN_KEYS = frozenset({"keep", "max_export_tokens"})


def warn_unknown_user_export_keys(params, *, user_id=None, placeholder=None,
                                  log=None):
    """Emit a structured warning if `params` contains keys not recognized
    by the {user_export} placeholder. Returns the set of unknown keys
    (empty when all keys are recognized). Helps surface typos that would
    otherwise silently fall back to default behavior."""
    unknown = set(params) - USER_EXPORT_KNOWN_KEYS
    if unknown:
        log = log if log is not None else _default_logger
        log.warning(
            "Unrecognized {user_export} param key(s) for user_id=%s: "
            "%s placeholder=%r — these are ignored. Known keys: %s",
            user_id, sorted(unknown), placeholder,
            sorted(USER_EXPORT_KNOWN_KEYS),
        )
    return unknown


class UserExportValidationError(ValueError):
    """Raised when a {user_export} placeholder has an unrecognized param
    key. Caught upstream of LLM-node creation to abort the request before
    any cost is incurred (silent typo fallbacks once shipped 1M+ tokens
    to the LLM at $5.71 per request)."""


def _format_unknown_keys_error(unknown_keys, placeholder=None):
    parts = [
        f"{{user_export}} has unrecognized param key(s) "
        f"{sorted(unknown_keys)}.",
        f"Known keys: {sorted(USER_EXPORT_KNOWN_KEYS)}.",
        "Fix the typo (check for stray whitespace) and resend.",
    ]
    if placeholder:
        parts.insert(1, f"Got: {placeholder}.")
    return " ".join(parts)


def validate_user_export_placeholders(text, *, user_id=None, log=None):
    """Scan `text` for {user_export?...} placeholders and validate each.

    Raises UserExportValidationError on the first invalid placeholder
    so callers can abort BEFORE creating LLM placeholder nodes or
    dispatching tasks. The error message is user-facing (used in HTTP
    400 responses and as toast text).

    No-op when `text` is empty or contains no {user_export} placeholders.
    """
    if not text:
        return
    log = log if log is not None else _default_logger
    for match in USER_EXPORT_PATTERN.finditer(text):
        placeholder = match.group(0)
        params = parse_placeholder_params(placeholder)
        unknown = set(params) - USER_EXPORT_KNOWN_KEYS
        if unknown:
            log.warning(
                "Refused {user_export} placeholder for user_id=%s: "
                "unknown key(s) %s placeholder=%r",
                user_id, sorted(unknown), placeholder,
            )
            raise UserExportValidationError(
                _format_unknown_keys_error(unknown, placeholder=placeholder)
            )


def parse_max_export_tokens(raw, *, user_id=None, placeholder=None,
                            log=None):
    """Parse the `max_export_tokens` value from a {user_export} placeholder.

    Returns the parsed int when valid (>= 0), or None when absent /
    non-numeric / negative. Non-numeric and negative values emit a
    structured warning so user typos surface in logs rather than silently
    producing a different budget than intended.
    """
    if raw is None:
        return None
    log = log if log is not None else _default_logger
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "Ignoring non-numeric max_export_tokens for user_id=%s: "
            "raw=%r placeholder=%r",
            user_id, raw, placeholder,
        )
        return None
    if parsed < 0:
        log.warning(
            "Ignoring negative max_export_tokens for user_id=%s: "
            "raw=%r placeholder=%r",
            user_id, raw, placeholder,
        )
        return None
    return parsed
