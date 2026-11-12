"""The size of the next AI reply's prompt in a thread, and the limit on
it (#648; Peter, 2026-11-12).

Every AI reply sends the whole thread above it, with the system prompt,
the user's context and the tool definitions. Nothing limited that, so a
very long thread, or one holding a very large document, made every reply
large and costly. Peter decided two steps: a notice from 90 % of a hard
limit, and the hard limit itself, at which an AI reply in the thread is
refused before any model call. Writing in the thread still works, and no
price is shown. The limit (THREAD_PROMPT_TOKEN_LIMIT, 250k tokens) and
the notice (THREAD_PROMPT_NOTICE_FRACTION of it) are settings.

How the size is known before the model is called, with no extra call:

- The thread's last measurement. Every conversation call records its
  full prompt size (APICostLog.input_tokens, counted by the provider)
  against the reply node it wrote (request_ref "node:<id>"). The newest
  AI turn on the path whose first call was recorded is the anchor: its
  prompt held everything above that turn. A Read reply (Glean) is never
  the anchor: its prompt held a day of tweets that later turns do not
  send again. Only the requesting user's own calls count.
- What was written since: the anchor turn's replies and every entry
  below them, in stored content units (Node.token_count: characters / 4
  of the node's text) plus a small per-message overhead, converted to
  tokens with the tokens per character the anchor call measured
  (input_tokens / prompt_chars). Without that figure: the user's latest
  conversation call on a model with the same tokenizer, then the ratio
  their profile builds measured, then the prior for the tokenizer
  (utils/tokens.TOKENS_PER_UNIT_PRIOR).
- No anchor (a thread's first reply, or an entry in the measured part was
  deleted since): the units of the whole path, converted the same way.
  This misses the system prompt and the text quotes pull in, so it is a
  lower bound. The completion task measures the assembled prompt itself
  before its first call (assembled_prompt_tokens) and refuses there; that
  refusal is recorded as a $0 cost row of type THREAD_LIMIT_REQUEST_TYPE
  against the node the reply was asked under, so the reply form and the
  next request know it without asking the task again.

Measuring changes nothing that is sent: a prompt under the limit goes
out byte for byte as before (the prompt cache, #187).
"""
import json
import logging
from collections import namedtuple

from flask import current_app

from backend.extensions import db
from backend.models import APICostLog
from backend.utils.tokens import (
    TOKENS_PER_UNIT_MAX, TOKENS_PER_UNIT_MIN, TOKENS_PER_UNIT_PRIOR,
    tokenizer_family,
)

logger = logging.getLogger(__name__)

THREAD_TOO_LONG_CODE = "thread_too_long"

# The refusal, wherever a reply is asked for (HTTP error, a skipped
# reply's llm_error, a failed reply's text). Plain: no price, no token
# numbers. "Including what it quotes": a thread with one large document
# in it reaches the limit after a few entries.
THREAD_TOO_LONG_MESSAGE = (
    "This thread is too long for another AI reply. Each reply reads the "
    "whole thread, including what it quotes, and this one has grown too "
    "large. You can still write here. To keep talking with AI, start a "
    "new thread.")

# A recording or upload whose reply was skipped for the limit (the
# VOICE_REPLY_SKIPPED_* / UPLOAD_REPLY_SKIPPED_* pattern).
RECORDING_REPLY_SKIPPED_MESSAGE = (
    "Your recording is saved. Loore didn't reply because this thread is "
    "too long for another AI reply. Start a new thread to keep talking.")
ENTRY_REPLY_SKIPPED_MESSAGE = (
    "Your entry is saved. Loore didn't reply because this thread is too "
    "long for another AI reply. Start a new thread to keep talking.")

# The cost row of a refusal measured by the completion task: $0, no call.
THREAD_LIMIT_REQUEST_TYPE = "thread_limit"
CONVERSATION_REQUEST_TYPE = "conversation"

# tool_calls_meta entry on a reply the completion task refused:
# {"name": "_refused", "code": THREAD_TOO_LONG_CODE}. llm-status gives the
# code to the reply's owner as ``error_code``; the clients hide "_" names.
REFUSED_MARKER = "_refused"

# The states the thread's API response carries (``thread_length``).
NOTICE = "notice"
LIMIT = "limit"

# Heuristic: what one message adds beyond its node's own text — the
# local time stamp and "author <name>:" the task puts in front of it
# (~40 characters), in units of 4 characters.
MESSAGE_OVERHEAD_UNITS = 10

# Characters per stored unit (Node.token_count is len(text) // 4).
CHARS_PER_UNIT = 4


class ThreadTooLong(Exception):
    """A reply's prompt would be over THREAD_PROMPT_TOKEN_LIMIT. Raised
    by create_llm_placeholder before anything is written. HTTP callers
    answer 409 ``{"error", "code": "thread_too_long"}``
    (thread_too_long_response; backend/__init__.py registers the same
    answer for a route that lets it escape), or keep the entry they wrote
    and report the skipped reply as ``llm_error`` / ``llm_error_code``."""

    code = THREAD_TOO_LONG_CODE

    def __init__(self, tokens=None, limit=None,
                 message=THREAD_TOO_LONG_MESSAGE):
        super().__init__(message)
        self.message = message
        self.tokens = tokens
        self.limit = limit


def thread_too_long_response(exc=None):
    """The 409 answer for a refused reply."""
    from flask import jsonify
    exc = exc or ThreadTooLong()
    return jsonify({"error": exc.message, "code": exc.code}), 409


def reply_skipped_fields(exc=None):
    """For a 2xx that saved the user's entry but skipped its reply."""
    exc = exc or ThreadTooLong()
    return {"llm_error": exc.message, "llm_error_code": exc.code}


def skipped_reply_code(warning):
    """The code a Voice recording's warning carries for the clients
    (``warning_code`` beside ``warning``): THREAD_TOO_LONG_CODE for a
    reply skipped for the thread's length, which the Voice screen says
    aloud; None for any other warning."""
    if warning == RECORDING_REPLY_SKIPPED_MESSAGE:
        return THREAD_TOO_LONG_CODE
    return None


# ── Settings ─────────────────────────────────────────────────────────────

def prompt_limit(config=None):
    """THREAD_PROMPT_TOKEN_LIMIT, or 0 when the limit is off."""
    cfg = config if config is not None else current_app.config
    try:
        return max(0, int(cfg.get("THREAD_PROMPT_TOKEN_LIMIT") or 0))
    except (TypeError, ValueError):
        return 0


def notice_from(config=None):
    """The prompt size from which the reply form shows the notice: the
    limit times THREAD_PROMPT_NOTICE_FRACTION (kept within (0, 1])."""
    cfg = config if config is not None else current_app.config
    limit = prompt_limit(cfg)
    try:
        fraction = float(cfg.get("THREAD_PROMPT_NOTICE_FRACTION", 0.9))
    except (TypeError, ValueError):
        fraction = 0.9
    if not 0 < fraction <= 1:
        fraction = 0.9
    return int(limit * fraction)


def length_state(tokens, config=None):
    """None under the notice size, NOTICE from it, LIMIT at the limit."""
    limit = prompt_limit(config)
    if not limit or tokens is None:
        return None
    if tokens >= limit:
        return LIMIT
    if tokens >= notice_from(config):
        return NOTICE
    return None


# ── Measuring ────────────────────────────────────────────────────────────

# One measured prompt on the path. *through*: the index, in the
# nearest-first chain, from which the measured prompt held every node
# (the nodes before it, nearer the tip, were written since). *chars* is
# None on rows from before APICostLog.prompt_chars.
Measurement = namedtuple(
    "Measurement", "tokens chars model_id at through kind")


def _is_llm(node):
    return node.node_type == "llm" or bool(node.llm_model)


def _turn_start(chain, i):
    """The index of the first node of the AI turn chain[i] belongs to:
    up through the interim steps (a tool round) whose continuation it
    is. Each round's call was recorded on its own node, and only the
    first call's prompt held nothing but the thread."""
    while i + 1 < len(chain):
        older = chain[i + 1]
        if (_is_llm(older) and older.continuation_node_id is not None
                and older.continuation_node_id == chain[i].id):
            i += 1
        else:
            break
    return i


def latest_measurement(chain, user_id, reads=frozenset()):
    """The newest valid Measurement of a reply's prompt on *chain* (the
    parent of the next reply and its ancestors, nearest first), or None.
    One query. A measurement is dropped when a node it held was deleted
    after it was taken: the prompt is smaller now, and the lower bound
    (with the task's own measurement) takes over."""
    if not chain or user_id is None:
        return None
    refs = {f"node:{n.id}": i for i, n in enumerate(chain)}
    rows = (db.session.query(
        APICostLog.id, APICostLog.request_ref, APICostLog.request_type,
        APICostLog.input_tokens, APICostLog.prompt_chars,
        APICostLog.model_id, APICostLog.created_at)
        .filter(APICostLog.user_id == user_id,
                APICostLog.request_type.in_(
                    (CONVERSATION_REQUEST_TYPE, THREAD_LIMIT_REQUEST_TYPE)),
                APICostLog.request_ref.in_(list(refs)))
        .all())
    first_call = {}
    refusals = {}
    for row in rows:
        i = refs.get(row.request_ref)
        if i is None or not row.input_tokens:
            continue
        if row.request_type == CONVERSATION_REQUEST_TYPE:
            if not _is_llm(chain[i]) or chain[i].id in reads:
                continue
            seen = first_call.get(i)
            if seen is None or row.id < seen.id:
                first_call[i] = row
        else:
            seen = refusals.get(i)
            if seen is None or row.id > seen.id:
                refusals[i] = row

    best = None
    for i in sorted(first_call):
        start = _turn_start(chain, i)
        if chain[start].id in reads:
            continue
        at = start if start in first_call else i
        row = first_call[at]
        best = Measurement(row.input_tokens, row.prompt_chars, row.model_id,
                           row.created_at, at + 1, CONVERSATION_REQUEST_TYPE)
        break
    for i, row in refusals.items():
        found = Measurement(row.input_tokens, row.prompt_chars, row.model_id,
                            row.created_at, i, THREAD_LIMIT_REQUEST_TYPE)
        if (best is None or found.through < best.through
                or (found.through == best.through and found.at
                    and best.at and found.at > best.at)):
            best = found
    if best is None:
        return None
    for node in chain[best.through:]:
        deleted = getattr(node, "deleted_at", None)
        if deleted is not None and best.at is not None and deleted > best.at:
            return None
    return best


def _family(model_id):
    return tokenizer_family(model_id)


def _models_of_family(family):
    return [m for m in current_app.config.get("SUPPORTED_MODELS", {})
            if tokenizer_family(m) == family]


def _bounded(per_unit):
    return min(max(per_unit, TOKENS_PER_UNIT_MIN), TOKENS_PER_UNIT_MAX)


def tokens_per_unit(user_id, model_id, measurement=None, user=None):
    """Tokens per stored unit (4 characters) for *model_id*'s tokenizer,
    and where the figure came from:

    - "thread": the measurement's own call (input_tokens / prompt_chars),
      when it was on the same tokenizer;
    - "user": the user's latest conversation call with a size on a model
      of that tokenizer (one query);
    - "profile": the ratio the user's profile builds measured on it
      (User.profile_token_ratio);
    - "prior": TOKENS_PER_UNIT_PRIOR["threads"] for the tokenizer, the
      upper end of what was measured (2026-09-03)."""
    family = _family(model_id)
    if (measurement is not None and measurement.chars
            and measurement.tokens and measurement.kind
            == CONVERSATION_REQUEST_TYPE
            and _family(measurement.model_id) == family):
        return _bounded(measurement.tokens * CHARS_PER_UNIT
                        / measurement.chars), "thread"
    if user_id is not None:
        row = (db.session.query(APICostLog.input_tokens,
                                APICostLog.prompt_chars)
               .filter(APICostLog.user_id == user_id,
                       APICostLog.request_type == CONVERSATION_REQUEST_TYPE,
                       APICostLog.prompt_chars > 0,
                       APICostLog.input_tokens > 0,
                       APICostLog.model_id.in_(_models_of_family(family)))
               .order_by(APICostLog.id.desc())
               .first())
        if row is not None:
            return _bounded(row.input_tokens * CHARS_PER_UNIT
                            / row.prompt_chars), "user"
    if user is None and user_id is not None:
        from backend.models import User
        user = db.session.get(User, user_id)
    measured = getattr(user, "profile_token_ratio", None)
    if measured and getattr(user, "profile_token_ratio_family", None) == family:
        return _bounded(float(measured)), "profile"
    return TOKENS_PER_UNIT_PRIOR["threads"][family], "prior"


def _node_units(node):
    if getattr(node, "deleted_at", None) is not None:
        return MESSAGE_OVERHEAD_UNITS
    return (getattr(node, "token_count", None) or 0) + MESSAGE_OVERHEAD_UNITS


def _default_model(user_id, user=None):
    from backend.utils.llm_nodes import default_model_for
    if user is None and user_id is not None:
        from backend.models import User
        user = db.session.get(User, user_id)
    return default_model_for(user)


def estimate_reply_tokens(chain, user_id, model_id=None, reads=frozenset(),
                          user=None):
    """The estimated prompt size, in tokens, of the next AI reply under
    chain[0] (*chain*: that node and its ancestors, nearest first, each
    with id, node_type, llm_model, token_count, continuation_node_id and
    deleted_at). No content is read and no provider is called: one query
    for the measurement, at most one for the ratio. *reads*: the ids of
    the chain's Read replies. *model_id*: the model the reply would run
    on; None takes the anchor's, else the user's default."""
    if not chain:
        return 0
    found = latest_measurement(chain, user_id, reads)
    if model_id is None:
        model_id = (found.model_id if found is not None
                    else _default_model(user_id, user))
    per_unit, _source = tokens_per_unit(user_id, model_id, found, user=user)
    through = found.through if found is not None else len(chain)
    units = sum(_node_units(n) for n in chain[:through])
    base = found.tokens if found is not None else 0
    return int(base + units * per_unit)


# A path already loaded by the caller (the thread page's ancestors):
# *nodes* nearest first, *reads* the ids of its Read replies.
PathChain = namedtuple("PathChain", "nodes reads")


def chain_for(node):
    """*node* and its ancestors, nearest first, with what the estimate
    and the anchor rules read (llm_nodes._Chain: two queries for the
    chain, two for the Read replies)."""
    from backend.utils.llm_nodes import _Chain
    return _Chain(node)


def thread_length(node, user_id, model_id=None, chain=None, user=None):
    """``(tokens, state)`` for a reply under *node*: the estimate and
    None / NOTICE / LIMIT. ``(None, None)`` when the limit is off."""
    if node is None or not prompt_limit():
        return None, None
    chain = chain or chain_for(node)
    tokens = estimate_reply_tokens(chain.nodes, user_id, model_id,
                                   reads=chain.reads, user=user)
    return tokens, length_state(tokens)


def thread_length_refusal(node, user_id, model_id=None, chain=None,
                          user=None):
    """A ThreadTooLong when a reply under *node* would be at or over the
    limit, else None. Every path that asks for a reply goes through
    create_llm_placeholder, which asks here; the Voice routes ask it
    before a recording starts."""
    tokens, state = thread_length(node, user_id, model_id, chain=chain,
                                  user=user)
    if state != LIMIT:
        return None
    logger.info("Reply under node %s refused: prompt ~%d tokens >= %d",
                node.id, tokens, prompt_limit())
    return ThreadTooLong(tokens, prompt_limit())


# ── In the completion task ───────────────────────────────────────────────

def prompt_chars(messages, tools=None):
    """Characters of a prompt: every block's text (llm_providers
    .message_text) and the tool definitions as JSON."""
    from backend.llm_providers import message_text
    chars = sum(len(message_text(m)) for m in messages or ())
    if tools:
        chars += len(json.dumps(tools, sort_keys=True, default=str))
    return chars


def assembled_prompt_tokens(chain, user_id, model_id, messages, msg_start,
                            tools=None, reads=frozenset(), user=None):
    """The completion task's measure of the prompt it has assembled,
    before the first call, as ``(tokens, chars)``. *chain*: the task's
    node chain, nearest first; *msg_start*: {node id: index of its first
    message}. With a measurement on the path: its tokens plus what was
    written since, now in the characters the prompt actually has (quotes
    and placeholders resolved), at the thread's tokens per character.
    Without one: the whole prompt's characters (system prompt and tools
    included) at the user's tokens per character."""
    chars = prompt_chars(messages, tools)
    found = latest_measurement(chain, user_id, reads)
    per_unit, source = tokens_per_unit(user_id, model_id, found, user=user)
    per_char = per_unit / CHARS_PER_UNIT
    if found is not None:
        if found.through == 0:
            return found.tokens, chars
        start = msg_start.get(chain[found.through - 1].id)
        if start is not None:
            since = prompt_chars(messages[start:])
            return int(found.tokens + since * per_char), chars
    logger.debug("Prompt measured from %d chars at %.3f tokens/char (%s)",
                 chars, per_char, source)
    return int(chars * per_char), chars


def record_refusal(user_id, model_id, node_id, tokens, chars=None):
    """The $0 cost row of a refusal the completion task measured: no
    call was made. *node_id* is the node the reply was asked under, so
    the next estimate on the path finds it (latest_measurement). The
    caller commits."""
    db.session.add(APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type=THREAD_LIMIT_REQUEST_TYPE,
        request_ref=f"node:{node_id}",
        input_tokens=int(tokens),
        output_tokens=0,
        prompt_chars=chars,
        cost_microdollars=0,
    ))
