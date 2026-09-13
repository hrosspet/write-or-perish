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
#   days=<int>              - Time window: only include content created in
#                             the last N days (counted back from the node
#                             carrying the placeholder). Useful for
#                             period reviews, e.g. {user_export?days=92}
#                             for a quarterly writeup. Non-numeric or
#                             values < 1 are logged and ignored (full
#                             archive). Combines with max_export_tokens
#                             (budget applies within the window).
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
USER_EXPORT_KNOWN_KEYS = frozenset({"keep", "max_export_tokens", "days"})


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


# Plan gate for the archive export. A {user_export} with no token
# budget (or a budget above this cap) loads the whole archive into the
# prompt on every reply — $4-5 per request on a large archive — so it is
# a Pro-plan feature. Everyone else must cap it. The cap is the value
# Peter set on 2026-09-09; it is not derived from anything.
FREE_USER_EXPORT_TOKEN_CAP = 100_000

USER_EXPORT_EXAMPLE = (
    "{user_export?max_export_tokens=" + str(FREE_USER_EXPORT_TOKEN_CAP) + "}"
)


def unrestricted_export_message():
    """User-facing text for a refused unrestricted {user_export}. Used
    as the HTTP 400 body / toast and as the failed node's error."""
    return (
        "{user_export} without a token limit loads your whole archive "
        "into every reply and is available on the Pro plan. On your "
        "plan, add a limit of up to "
        f"{FREE_USER_EXPORT_TOKEN_CAP:,} tokens, e.g. {USER_EXPORT_EXAMPLE}"
    )


def export_budget_allowed(max_export_tokens, *, unrestricted_allowed):
    """Whether an export with this parsed budget may run.

    `max_export_tokens` is the output of parse_max_export_tokens: None
    means no (valid) cap, i.e. the full archive. Entitled users may run
    anything; others need a cap within FREE_USER_EXPORT_TOKEN_CAP.
    0 (export disabled) is always fine.
    """
    if unrestricted_allowed:
        return True
    if max_export_tokens is None:
        return False
    return max_export_tokens <= FREE_USER_EXPORT_TOKEN_CAP


def check_user_export_plan(text, *, unrestricted_allowed, user_id=None,
                           log=None):
    """Refuse {user_export} placeholders in `text` that this user's plan
    may not run (see export_budget_allowed). Raises
    UserExportValidationError with the user-facing message, so callers
    treat it exactly like a malformed placeholder: abort before any LLM
    node is created or any spend happens.

    No-op for entitled users or text without the placeholder. Every
    placeholder is checked, not just the first: the task resolves the
    first one in chain order, and which node comes first is not known
    at the single-node call sites.
    """
    if unrestricted_allowed or not text:
        return
    log = log if log is not None else _default_logger
    for match in USER_EXPORT_PATTERN.finditer(text):
        placeholder = match.group(0)
        params = parse_placeholder_params(placeholder)
        budget = parse_max_export_tokens(
            params.get("max_export_tokens"),
            user_id=user_id, placeholder=placeholder, log=log,
        )
        if not export_budget_allowed(
                budget, unrestricted_allowed=unrestricted_allowed):
            log.warning(
                "Refused unrestricted {user_export} for user_id=%s: "
                "placeholder=%r budget=%r cap=%s",
                user_id, placeholder, budget, FREE_USER_EXPORT_TOKEN_CAP,
            )
            raise UserExportValidationError(unrestricted_export_message())


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


# ── {ca_tweets} — Community Archive feed (PoC, 2026-09-13) ─────────────
#
# Syntax: {ca_tweets} or {ca_tweets?days=N}
#
# Renders the last N days of the Community Archive corpus (the nightly
# parquet snapshot cached under COMMUNITY_ARCHIVE_SNAPSHOT_DIR) in the
# compact tweet format, so a prompt can ask "is there anything in the
# last day's tweets this person would benefit from reading?". The window
# ends at the NEWEST TWEET IN THE SNAPSHOT, not at the node's timestamp:
# the export lags a few hours and undercounts the newest day, so
# anchoring on the node would silently shrink days=1 to a partial day.
#
# A prompt carrying this placeholder is sent through the provider's
# Batch API (Anthropic only for now) rather than a synchronous call: a
# day of the corpus is ~250k tokens, the answer is not latency-bound,
# and batch is half price. See generate_llm_response.
#
# Supported params:
#   days=<int>     - window length, default 1, at most CA_TWEETS_MAX_DAYS
#                    (a day of the whole archive is ~170k tokens; three is
#                    the most a 1M-context model takes, and the most one
#                    request should cost). Non-numeric, < 1 or over the cap
#                    is refused (not silently defaulted — see the note
#                    above USER_EXPORT_KNOWN_KEYS on silent-fallback cost
#                    bugs).
#   scope=all      - the whole archive (default).
#   scope=follows  - only the accounts the user follows on X, from the
#                    following list saved for them under the snapshot
#                    dir (following/<username>.json); refused at run
#                    time when no list is saved.
#
CA_TWEETS_PATTERN = re.compile(r"\{ca_tweets(\?[^}]*)?\}")
CA_TWEETS_KNOWN_KEYS = frozenset({"days", "scope"})
CA_TWEETS_DEFAULT_DAYS = 1
CA_TWEETS_MAX_DAYS = 3
CA_TWEETS_SCOPES = ("all", "follows")


class CaTweetsValidationError(UserExportValidationError):
    """A malformed {ca_tweets} placeholder. Subclasses the export error so
    every HTTP call site that already turns a refused {user_export} into
    a 400 + toast handles this one identically."""


def parse_ca_tweets_days(params, *, placeholder=None):
    """`days` from a parsed {ca_tweets} param dict → int >= 1 (default
    CA_TWEETS_DEFAULT_DAYS). Raises CaTweetsValidationError on junk."""
    raw = params.get("days")
    if raw is None:
        return CA_TWEETS_DEFAULT_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 0
    if days < 1 or days > CA_TWEETS_MAX_DAYS:
        raise CaTweetsValidationError(
            f"{{ca_tweets}} needs days=<whole number 1..{CA_TWEETS_MAX_DAYS}>; "
            f"got {placeholder or raw!r}.")
    return days


def ca_tweets_allowed(user):
    """Who may run {ca_tweets}: admins, while it is a PoC. A day of the
    archive is a ~$0.02–$0.50 batch request per run with no plan gate
    behind it, so it must not be reachable from any account that can
    type the placeholder."""
    return bool(user is not None and getattr(user, "is_admin", False))


def ca_tweets_denied_message():
    return ("{ca_tweets} (the Community Archive feed) is not available on "
            "your account yet.")


def check_ca_tweets_access(text, user, *, log=None):
    """Refuse {ca_tweets} in `text` for users ca_tweets_allowed() rejects.
    Raises CaTweetsValidationError (→ 400 + toast at every call site),
    before any node exists. No-op without the placeholder."""
    if not text or not CA_TWEETS_PATTERN.search(text):
        return
    if not ca_tweets_allowed(user):
        log = log if log is not None else _default_logger
        log.warning("Refused {ca_tweets} for user_id=%s: not allowed",
                    getattr(user, "id", None))
        raise CaTweetsValidationError(ca_tweets_denied_message())


def parse_ca_tweets_scope(params, *, placeholder=None):
    """`scope` from a parsed {ca_tweets} param dict → 'all' | 'follows'."""
    raw = (params.get("scope") or "all").strip().lower()
    if raw not in CA_TWEETS_SCOPES:
        raise CaTweetsValidationError(
            f"{{ca_tweets}} scope must be one of {list(CA_TWEETS_SCOPES)}; "
            f"got {placeholder or raw!r}.")
    return raw


def validate_ca_tweets_placeholders(text, *, user_id=None, log=None):
    """Refuse a {ca_tweets?...} placeholder with unknown keys or a bad
    `days` BEFORE any LLM node exists or any spend happens (mirrors
    validate_user_export_placeholders). No-op without the placeholder."""
    if not text:
        return
    log = log if log is not None else _default_logger
    for match in CA_TWEETS_PATTERN.finditer(text):
        placeholder = match.group(0)
        params = parse_placeholder_params(placeholder)
        unknown = set(params) - CA_TWEETS_KNOWN_KEYS
        if unknown:
            log.warning(
                "Refused {ca_tweets} placeholder for user_id=%s: unknown "
                "key(s) %s placeholder=%r", user_id, sorted(unknown),
                placeholder)
            raise CaTweetsValidationError(
                f"{{ca_tweets}} has unrecognized param key(s) "
                f"{sorted(unknown)}. Got: {placeholder}. Known keys: "
                f"{sorted(CA_TWEETS_KNOWN_KEYS)}.")
        parse_ca_tweets_days(params, placeholder=placeholder)
        parse_ca_tweets_scope(params, placeholder=placeholder)


def parse_days(raw, *, user_id=None, placeholder=None, log=None):
    """Parse the `days` value from a {user_export} placeholder.

    Returns the parsed int when valid (>= 1), or None when absent /
    non-numeric / < 1. Invalid values emit a structured warning so user
    typos surface in logs rather than silently exporting the full
    archive (there is no meaningful zero-day window, so 0 is treated as
    invalid rather than as "disable" — max_export_tokens=0 already
    covers disabling the export).
    """
    if raw is None:
        return None
    log = log if log is not None else _default_logger
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "Ignoring non-numeric days for user_id=%s: "
            "raw=%r placeholder=%r",
            user_id, raw, placeholder,
        )
        return None
    if parsed < 1:
        log.warning(
            "Ignoring days < 1 for user_id=%s: raw=%r placeholder=%r",
            user_id, raw, placeholder,
        )
        return None
    return parsed
