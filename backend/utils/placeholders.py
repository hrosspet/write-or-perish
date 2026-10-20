"""Helpers for {user_export} placeholder parsing, and the one-pass fill
of a prompt's placeholders (fill_placeholders).

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
# Only admins may change the window or scope (Peter, 2026-10-14): for
# anyone else every {ca_tweets} must be the default, {ca_tweets} or
# exactly days=1 and/or scope=all (check_ca_tweets_options). Checked
# before the reply's node exists and again in the completion task.
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
    """Who may run {ca_tweets} — the Glean rollout gate (#435): admins,
    every user while GLEAN_FOR_ALL is on, else the users listed in
    GLEAN_USER_IDS. A day of the archive is a paid request of a few cents
    per run (live gleans cost twice the batch price), so it must not be
    reachable from any account outside the gate. The user's own Glean
    switch (utils/glean.glean_enabled) sits on top of this; this is the
    one check for the placeholder and the completion task."""
    if user is None:
        return False
    if getattr(user, "is_admin", False):
        return True
    from flask import current_app
    if current_app.config.get("GLEAN_FOR_ALL", False):
        return True
    return getattr(user, "id", None) in (
        current_app.config.get("GLEAN_USER_IDS") or set())


def ca_tweets_denied_message():
    return "Glean is not available on your account yet."


def ca_tweets_text_allowed(user, *, from_read_prompt=False):
    """Who may have {ca_tweets} in a text that is sent: an admin anywhere
    (the PoC's own experiments: typed into an entry or a saved prompt);
    anyone else only when it comes from one of the read prompts a glean
    attaches, and only while they glean (the rollout gate and their own
    switch, utils/glean.glean_enabled). A typed placeholder would run a
    read outside Glean: on any days= value, on the chat model, with the
    switch off (#435 review)."""
    if user is None:
        return False
    if getattr(user, "is_admin", False):
        return True
    if not from_read_prompt:
        return False
    from backend.utils.glean import glean_enabled
    return glean_enabled(user)


def check_ca_tweets_access(text, user, *, from_read_prompt=False, log=None):
    """Refuse {ca_tweets} in `text` for users ca_tweets_text_allowed()
    rejects. *from_read_prompt*: the text is a read prompt a glean
    attached. Raises CaTweetsValidationError (→ 400 + toast at every call
    site), before any node exists. No-op without the placeholder."""
    if not text or not CA_TWEETS_PATTERN.search(text):
        return
    if not ca_tweets_text_allowed(user, from_read_prompt=from_read_prompt):
        log = log if log is not None else _default_logger
        log.warning("Refused {ca_tweets} for user_id=%s: not allowed",
                    getattr(user, "id", None))
        raise CaTweetsValidationError(ca_tweets_denied_message())


def ca_tweets_options_allowed(user):
    """Whether *user* may run {ca_tweets} with other options than the
    default (days=1, scope=all): only admins may change the window or
    scope (Peter, 2026-10-14)."""
    return user is not None and getattr(user, "is_admin", False) is True


def ca_tweets_options_denied_message():
    return ("Only admins may change the window or scope of {ca_tweets}. "
            "Use {ca_tweets} or {ca_tweets?days=1}.")


# The only values a non-admin's {ca_tweets} may carry.
CA_TWEETS_DEFAULT_PARAMS = {"days": str(CA_TWEETS_DEFAULT_DAYS),
                            "scope": "all"}


def ca_tweets_is_default(placeholder):
    """Whether a {ca_tweets...} placeholder is the default: no params, or
    exactly days=1 and/or scope=all, each once. Any other key, value,
    repeat or a key without a value is not."""
    if "?" not in placeholder:
        return True
    qs = placeholder.split("?", 1)[1].rstrip("}")
    for key, values in parse_qs(qs, keep_blank_values=True).items():
        expected = CA_TWEETS_DEFAULT_PARAMS.get(key.strip())
        if expected is None or len(values) != 1 \
                or values[0].strip() != expected:
            return False
    return True


def check_ca_tweets_options(text, user, *, log=None):
    """Refuse every {ca_tweets} placeholder in *text* that is not the
    default (ca_tweets_is_default) unless *user* is an admin: only
    admins may change the window or scope. Raises
    CaTweetsValidationError (a 400 at every HTTP call site, a failed
    reply in the completion task). No-op without the placeholder."""
    if not text or ca_tweets_options_allowed(user):
        return
    for match in CA_TWEETS_PATTERN.finditer(text):
        placeholder = match.group(0)
        if not ca_tweets_is_default(placeholder):
            log = log if log is not None else _default_logger
            log.warning("Refused {ca_tweets} options for user_id=%s: "
                        "placeholder=%r", getattr(user, "id", None),
                        placeholder)
            raise CaTweetsValidationError(ca_tweets_options_denied_message())


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


def fill_placeholders(text, values, patterns=(), seen=None):
    """*text* with its placeholders filled in ONE left-to-right pass.

    *values* maps each placeholder, as exact text, to its replacement: a
    string, or a callable taking the matched text for a value that
    depends on the occurrence (FirstThen). *patterns* are (compiled
    regex, callable) pairs for placeholders that take parameters
    ({ca_tweets?days=1}); each callable gets the matched text. *seen*, a
    set, collects every key of *values* that matched.

    A filled-in value is never searched again. Filling one placeholder
    after another rescanned the whole text each time, so a placeholder
    name inside an earlier value was filled too: the recent raw window
    carried a copy of an earlier session's prompt, and memory and
    intentions were filled into that copy as well (#518). Now a memory
    entry naming {user_intentions} stays as written.

    Values are inserted as they are: re's replacement-template escapes
    ("\\1", "\\g<0>", a Windows path's "\\U") mean nothing here, as with
    the str.replace this replaced.
    """
    if not values and not patterns:
        return text
    alternatives = [re.escape(key)
                    for key in sorted(values, key=len, reverse=True)]
    alternatives += [pattern.pattern for pattern, _fill in patterns]
    combined = re.compile("|".join(alternatives))

    def _replace(match):
        found = match.group(0)
        if found in values:
            if seen is not None:
                seen.add(found)
            value = values[found]
            return value(found) if callable(value) else value
        for pattern, fill in patterns:
            if pattern.fullmatch(found):
                return fill(found)
        return found

    return combined.sub(_replace, text)


class FirstThen:
    """A placeholder value that depends on the occurrence: *first* for
    the first one filled, *rest* for every later one, across all the
    texts it fills (the {user_export} archive goes into a prompt once,
    #139). Pass it as a value to fill_placeholders."""

    def __init__(self, first, rest):
        self.first = first
        self.rest = rest
        self.used = False

    def __call__(self, _found):
        if self.used:
            return self.rest
        self.used = True
        return self.first
