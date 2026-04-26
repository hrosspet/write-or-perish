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
    """Parse URL-style params from a placeholder like {user_export?keep=oldest}."""
    if '?' in match_str:
        qs = match_str.split('?', 1)[1].rstrip('}')
        return {k: v[0] for k, v in parse_qs(qs).items()}
    return {}


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
