"""OpenAI Prompt Cache Diagnostics for conversation turns (#348).

OpenAI reports why a Responses API call missed the prompt cache when the
request names an earlier response to compare against
(prompt_cache_options.comparison_response_id; GPT-5.6 and later, no
charge). This module picks that baseline for each conversation call and
turns the verdict into APICostLog columns. It only collects the data;
nothing here changes what is sent to the model.

Baselines:
- tool_round: within one turn, the previous round's response (held in
  memory by ConversationCacheDiagnostics).
- prev_turn: across turns, the last OpenAI conversation call on this
  node's ancestor path, found by the "node:<id>" request_ref its cost row
  carries. The last call in the thread would be wrong: prompt_cache_key is
  per thread root, so a sibling branch would report input_changed for a
  fork that is expected.
- None on a thread's first OpenAI call: no option is sent.
"""
import hashlib
import logging
from collections import namedtuple
from datetime import datetime

from backend.extensions import db
from backend.models import APICostLog

logger = logging.getLogger(__name__)

# Miss reasons our code should never cause: we set none of these request
# parameters, and the tool list is config-only since #329. Each one is
# logged as a warning so it shows up without a query.
UNEXPECTED_MISS_REASONS = frozenset({
    "reasoning_effort_changed", "verbosity_changed",
    "service_tier_changed", "context_compacted",
})

Baseline = namedtuple("Baseline", "response_id kind at")


def system_prefix_hash(messages, system_msg_index):
    """First 16 hex characters of the sha256 of the system prompt's text,
    or None when the prompt has no system message. Content never leaves
    this function; two calls with different hashes had different system
    prompts."""
    if system_msg_index is None or system_msg_index >= len(messages):
        return None
    content = messages[system_msg_index].get("content")
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict))
    else:
        text = str(content or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def find_prev_turn_baseline(user_id, node_chain):
    """The last OpenAI conversation call written to a node on this path,
    as a Baseline, or None."""
    refs = [f"node:{node.id}" for node in node_chain if node is not None]
    if not refs:
        return None
    row = (APICostLog.query
           .filter(APICostLog.user_id == user_id,
                   APICostLog.request_type == "conversation",
                   APICostLog.request_ref.in_(refs),
                   APICostLog.provider_response_id.isnot(None))
           .order_by(APICostLog.id.desc())
           .first())
    if row is None:
        return None
    return Baseline(row.provider_response_id, "prev_turn", row.created_at)


class ConversationCacheDiagnostics:
    """Per-turn baseline bookkeeping. `enabled` is False for Anthropic and
    for OpenAI models without "cache_diagnostics" in their config; the
    calls then go out unchanged and only the response id is recorded."""

    def __init__(self, enabled, user_id, node_chain, system_hash=None):
        self.enabled = enabled
        self.system_hash = system_hash
        self._baseline = None
        if enabled:
            # Analytics never cost a turn. A failed SELECT would abort the
            # whole Postgres transaction the task goes on to use, so the
            # lookup runs in a savepoint that is rolled back on failure.
            try:
                with db.session.no_autoflush:
                    with db.session.begin_nested():
                        self._baseline = find_prev_turn_baseline(
                            user_id, node_chain)
            except Exception:
                logger.warning("Cache diagnostics baseline lookup failed",
                               exc_info=True)

    def call(self, completion_fn):
        """Run one provider call. `completion_fn(comparison_response_id)`
        makes it; the response dict comes back annotated with the baseline
        that was sent, and becomes the baseline for the next round."""
        sent = self._baseline if self.enabled else None
        # The gap is measured to when the request went out, so a slow
        # reply does not look like cache expiry.
        sent_at = datetime.utcnow()
        resp = completion_fn(sent.response_id if sent else None)
        if sent is not None and resp.get("cache_comparison_sent"):
            resp["cache_diag_baseline"] = sent.kind
            if sent.at is not None:
                resp["cache_diag_gap_s"] = max(
                    0, int((sent_at - sent.at).total_seconds()))
        if self.enabled and resp.get("response_id"):
            self._baseline = Baseline(
                resp["response_id"], "tool_round", datetime.utcnow())
        return resp

    def log_fields(self, resp, node_id):
        """The APICostLog columns for one conversation call: the node it
        wrote, the response id, the verdict, and the system prompt hash."""
        fields = {
            "request_ref": f"node:{node_id}" if node_id else None,
            "provider_response_id": _text(resp.get("response_id"), 128),
            "system_prefix_hash": self.system_hash,
            "cache_diag_baseline": resp.get("cache_diag_baseline"),
            "cache_diag_gap_s": resp.get("cache_diag_gap_s"),
        }
        diag = resp.get("cache_diagnostics") or {}
        if diag:
            # The verdict is an untyped field from OpenAI: anything that
            # would not fit its column is dropped rather than failing the
            # commit that also completes the node.
            fields.update(
                cache_diag_type=_text(diag.get("type"), 40),
                cache_diag_reason=_text(diag.get("reason"), 40),
                cache_diag_reusable_tokens=_count(
                    diag.get("comparison_reusable_tokens")),
                cache_diag_missed_tokens=_count(
                    diag.get("cache_missed_tokens")),
            )
            if diag.get("reason") in UNEXPECTED_MISS_REASONS:
                logger.warning(
                    "OpenAI cache miss for a reason our code should not "
                    "cause: %s (node %s, baseline %s)", diag.get("reason"),
                    node_id, resp.get("cache_diag_baseline"))
        return fields


def _text(value, width):
    """*value* if it is a string, cut to the column width; else None."""
    return value[:width] if isinstance(value, str) else None


def _count(value):
    """*value* as an int that fits a Postgres integer column, else None."""
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if -2**31 <= n < 2**31 else None
