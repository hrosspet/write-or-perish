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

How the size is known before the model is called:

- The base: the thread's last measured prompt. Every conversation call
  records its full prompt size (APICostLog.input_tokens, counted by the
  provider) and its characters (prompt_chars) against the reply node it
  wrote (request_ref "node:<id>"). The newest AI turn on the path whose
  first call was recorded is the base: its prompt held everything above
  that turn. A Read reply (Glean) is never the base: its prompt held a
  day of tweets that later turns do not send again. Only the requesting
  user's own calls count. A refusal the completion task counted is a
  base too (THREAD_LIMIT_REQUEST_TYPE, below).
- The ratio: tokens per character. The base's own, when it was measured
  on the tokenizer of the model the reply will run on ("thread", the
  only ratio the estimate is *certain* with); else the user's latest
  call on that tokenizer, the ratio their profile builds measured, or the
  prior for the tokenizer (utils/tokens.TOKENS_PER_UNIT_PRIOR, an upper
  end). A figure measured on one tokenizer is never applied to another:
  a base from another tokenizer counts as its characters at this one's
  ratio.
- What was written since the base: the stored content units of those
  nodes (Node.token_count: characters / 4) and a small per-message
  overhead, at the ratio. No content is read and no provider is called
  (the reply form, the routes, Voice before a recording).
- Only a *certain* estimate refuses before the task runs. An uncertain
  one (no base, another tokenizer, a base from before prompt_chars) shows
  at most the notice, and the completion task decides: it measures the
  prompt it assembled (quotes and placeholders resolved, the base's part
  re-measured in characters so an edit or a deletion there counts both
  ways), and when that is uncertain and at or over the limit, it counts
  the prompt (LLMProvider.count_tokens: Anthropic's free count endpoint,
  tiktoken for OpenAI; no billed call) before refusing or fitting an
  archive. A refusal it decides is recorded as a $0 cost row under the
  node the reply was asked under, with the prompt's characters when the
  count was sure, so later estimates there are certain. A refusal row is
  dropped once the user changed or deleted an entry after it (what the
  thread quotes may have shrunk); the task then measures again.

Measuring changes nothing that is sent: a prompt under the limit goes
out byte for byte as it would with the limit off.
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

# Why the prompt is over the limit. LENGTH: the thread has grown long;
# a new thread is the way on. SIZE: an entry in it, or what it quotes, is
# most of the prompt; a new thread with the same text would be refused
# too, so the way on is a shorter text.
LENGTH = "length"
SIZE = "size"

# The refusal, wherever a reply is asked for (HTTP error, a skipped
# reply's llm_error, a failed reply's text, the thread page). Plain: no
# price, no numbers.
THREAD_TOO_LONG_MESSAGE = (
    "This thread is too long for another AI reply. Each reply reads the "
    "whole thread, including what it quotes, and this one has grown too "
    "large. You can still write here. To keep talking with AI, start a "
    "new thread.")
CONTENT_TOO_LARGE_MESSAGE = (
    "An entry in this thread, or what it quotes, is too large for an AI "
    "reply: each reply reads all of it. You can still write here. To talk "
    "about it with AI, shorten it, or split it and ask about one part at "
    "a time, each in its own thread.")

# A recording or upload whose reply was skipped for the limit (the
# VOICE_REPLY_SKIPPED_* / UPLOAD_REPLY_SKIPPED_* pattern).
RECORDING_REPLY_SKIPPED_MESSAGE = (
    "Your recording is saved. Loore didn't reply because this thread is "
    "too long for another AI reply. Start a new thread to keep talking.")
RECORDING_REPLY_SKIPPED_SIZE_MESSAGE = (
    "Your recording is saved. Loore didn't reply because an entry in this "
    "thread, or what it quotes, is too large for an AI reply. Start a new "
    "thread to keep talking.")
ENTRY_REPLY_SKIPPED_MESSAGE = (
    "Your entry is saved. Loore didn't reply because this thread is too "
    "long for another AI reply. Start a new thread to keep talking.")
ENTRY_REPLY_SKIPPED_SIZE_MESSAGE = (
    "Your entry is saved. Loore didn't reply because it, or what it "
    "quotes, is too large for an AI reply. Shorten it, or split it and "
    "ask about one part at a time, each in its own thread.")

_MESSAGES = {LENGTH: THREAD_TOO_LONG_MESSAGE,
             SIZE: CONTENT_TOO_LARGE_MESSAGE}
_RECORDING_MESSAGES = {LENGTH: RECORDING_REPLY_SKIPPED_MESSAGE,
                       SIZE: RECORDING_REPLY_SKIPPED_SIZE_MESSAGE}
_ENTRY_MESSAGES = {LENGTH: ENTRY_REPLY_SKIPPED_MESSAGE,
                   SIZE: ENTRY_REPLY_SKIPPED_SIZE_MESSAGE}

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

# Heuristic: the prompt is over the limit because of what the thread
# holds (SIZE), not its length, when one entry, or text no entry holds
# (what the entries quote, an archive), is more than this share of it.
SIZE_SHARE = 0.5

# Characters per stored unit (Node.token_count is len(text) // 4).
CHARS_PER_UNIT = 4


class ThreadTooLong(Exception):
    """A reply's prompt would be over THREAD_PROMPT_TOKEN_LIMIT. Raised
    by create_llm_placeholder before anything is written. HTTP callers
    answer 409 ``{"error", "code": "thread_too_long"}``
    (thread_too_long_response; backend/__init__.py registers the same
    answer for a route that lets it escape), or keep the entry they wrote
    and report the skipped reply as ``llm_error`` / ``llm_error_code``.
    *reason*: LENGTH or SIZE, which picks the text."""

    code = THREAD_TOO_LONG_CODE

    def __init__(self, tokens=None, limit=None, reason=LENGTH):
        self.reason = reason if reason in _MESSAGES else LENGTH
        self.message = _MESSAGES[self.reason]
        super().__init__(self.message)
        self.tokens = tokens
        self.limit = limit

    @property
    def recording_message(self):
        """The text a Voice recording whose reply was skipped carries."""
        return _RECORDING_MESSAGES[self.reason]

    @property
    def entry_message(self):
        """The text an upload whose reply was skipped carries."""
        return _ENTRY_MESSAGES[self.reason]


def refusal_message(reason=LENGTH):
    """The refusal's text for *reason*."""
    return _MESSAGES.get(reason, THREAD_TOO_LONG_MESSAGE)


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
    reply skipped for the thread's size, which the Voice screen says
    aloud; None for any other warning."""
    if warning in _RECORDING_MESSAGES.values():
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


def length_state(tokens, config=None, certain=True):
    """None under the notice size, NOTICE from it, LIMIT at the limit.
    An uncertain estimate never says LIMIT: at most NOTICE, and the
    completion task decides with a count."""
    limit = prompt_limit(config)
    if not limit or tokens is None:
        return None
    if tokens >= limit and certain:
        return LIMIT
    if tokens >= notice_from(config):
        return NOTICE
    return None


# ── Measuring ────────────────────────────────────────────────────────────

# One measured prompt on the path. *through*: the index, in the
# nearest-first chain, from which the measured prompt held every node
# (the nodes before it, nearer the tip, were written since). *chars* is
# None on rows from before APICostLog.prompt_chars, and on a refusal the
# task could not count.
Measurement = namedtuple(
    "Measurement", "tokens chars model_id at through kind")

# An estimate: *tokens*; *certain* when its ratio is the thread's own on
# this tokenizer (or a count); *reason* LENGTH or SIZE; *chars* the
# prompt's characters when the task measured them.
Size = namedtuple("Size", "tokens certain reason chars")


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


def _changed_since(user_id, when):
    """Whether the user changed or deleted one of their entries after
    *when* (a refusal: what the thread quotes may have shrunk). One
    query."""
    from sqlalchemy import or_
    from backend.models import Node
    if when is None:
        return True
    return db.session.query(
        db.session.query(Node.id).filter(
            Node.user_id == user_id,
            or_(Node.deleted_at > when,
                (Node.updated_at > when) & (Node.created_at <= when)),
        ).exists()).scalar()


def latest_measurement(chain, user_id, reads=frozenset()):
    """The newest valid Measurement of a reply's prompt on *chain* (the
    parent of the next reply and its ancestors, nearest first), or None.
    One query, two with a refusal row. A measurement is dropped when a
    node it held was deleted after it was taken (the prompt is smaller
    now); a refusal row also when the user changed or deleted any entry
    since, or when the task could not count it."""
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
        elif row.prompt_chars:
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
            if not _changed_since(user_id, found.at):
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

    - "thread": the measurement's own prompt (input_tokens /
      prompt_chars), when it was measured on the same tokenizer — the
      only certain ratio;
    - "user": the user's latest conversation call with a size on a model
      of that tokenizer (one query);
    - "profile": the ratio the user's profile builds measured on it
      (User.profile_token_ratio);
    - "prior": TOKENS_PER_UNIT_PRIOR["threads"] for the tokenizer, the
      upper end of what was measured (2026-09-03)."""
    family = _family(model_id)
    if (measurement is not None and measurement.chars
            and measurement.tokens
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


def _resolve(chain, user_id, model_id, reads, user):
    """``(measurement, base_tokens, per_unit, certain, model_id)``: the
    base the estimate starts from, in this model's tokens. A base on
    another tokenizer, or a refusal row, counts as its characters at
    today's ratio for this tokenizer; a base with neither the same
    tokenizer nor characters is no base."""
    found = latest_measurement(chain, user_id, reads)
    if model_id is None:
        model_id = (found.model_id if found is not None
                    else _default_model(user_id, user))
    per_unit, source = tokens_per_unit(user_id, model_id, found, user=user)
    base = None
    if found is not None:
        same = _family(found.model_id) == _family(model_id)
        if same and found.kind == CONVERSATION_REQUEST_TYPE:
            base = found.tokens
        elif found.chars:
            base = found.chars * per_unit / CHARS_PER_UNIT
        else:
            found = None
    return found, base, per_unit, source == "thread", model_id


def size_reason(tokens, chain, per_unit):
    """SIZE when one entry, or text no entry on the path holds (quotes,
    an archive, the system prompt's context), is more than SIZE_SHARE of
    *tokens*; else LENGTH. Stored sizes only."""
    if not tokens:
        return LENGTH
    node_tokens = [(_node_units(n) * per_unit) for n in chain]
    biggest = max(node_tokens, default=0)
    held = tokens - sum(node_tokens)
    if biggest > SIZE_SHARE * tokens or held > SIZE_SHARE * tokens:
        return SIZE
    return LENGTH


def estimate_reply_size(chain, user_id, model_id=None, reads=frozenset(),
                        user=None):
    """The estimated prompt of the next AI reply under chain[0] (*chain*:
    that node and its ancestors, nearest first, each with id, node_type,
    llm_model, token_count, continuation_node_id and deleted_at), as a
    Size. No content is read and no provider is called: one query for
    the measurement, at most two more. *reads*: the ids of the chain's
    Read replies. *model_id*: the model the reply would run on; None
    takes the base's, else the user's default."""
    if not chain:
        return Size(0, True, LENGTH, None)
    found, base, per_unit, certain, _model = _resolve(
        chain, user_id, model_id, reads, user)
    through = found.through if found is not None else len(chain)
    units = sum(_node_units(n) for n in chain[:through])
    tokens = int((base or 0) + units * per_unit)
    certain = certain and found is not None
    return Size(tokens, certain, size_reason(tokens, chain, per_unit), None)


def estimate_reply_tokens(chain, user_id, model_id=None, reads=frozenset(),
                          user=None):
    """estimate_reply_size's tokens."""
    return estimate_reply_size(chain, user_id, model_id, reads, user).tokens


# A path already loaded by the caller (the thread page's ancestors):
# *nodes* nearest first, *reads* the ids of its Read replies.
PathChain = namedtuple("PathChain", "nodes reads")


def chain_for(node):
    """*node* and its ancestors, nearest first, with what the estimate
    and the base rules read (llm_nodes._Chain: two queries for the
    chain, two for the Read replies)."""
    from backend.utils.llm_nodes import _Chain
    return _Chain(node)


def thread_size(node, user_id, model_id=None, chain=None, user=None):
    """``(Size, state)`` for a reply under *node*: the estimate and
    None / NOTICE / LIMIT. ``(None, None)`` when the limit is off."""
    if node is None or not prompt_limit():
        return None, None
    chain = chain or chain_for(node)
    size = estimate_reply_size(chain.nodes, user_id, model_id,
                               reads=chain.reads, user=user)
    return size, length_state(size.tokens, certain=size.certain)


def thread_length(node, user_id, model_id=None, chain=None, user=None):
    """``(tokens, state)`` for a reply under *node* (thread_size)."""
    size, state = thread_size(node, user_id, model_id, chain=chain,
                              user=user)
    return (size.tokens if size is not None else None), state


def thread_length_fields(node, user_id, chain=None, user=None):
    """``thread_length`` and, at the limit, ``thread_length_message`` (the
    refusal's text for its reason), for the clients."""
    size, state = thread_size(node, user_id, chain=chain, user=user)
    fields = {"thread_length": state}
    if state == LIMIT:
        fields["thread_length_message"] = refusal_message(size.reason)
    return fields


def thread_length_refusal(node, user_id, model_id=None, chain=None,
                          user=None):
    """A ThreadTooLong when a reply under *node* would be at or over the
    limit and the estimate is certain, else None. Every path that asks
    for a reply goes through create_llm_placeholder, which asks here; the
    Voice routes ask it before a recording starts."""
    size, state = thread_size(node, user_id, model_id, chain=chain,
                              user=user)
    if state != LIMIT:
        return None
    logger.info("Reply under node %s refused: prompt ~%d tokens >= %d (%s)",
                node.id, size.tokens, prompt_limit(), size.reason)
    return ThreadTooLong(size.tokens, prompt_limit(), size.reason)


# ── In the completion task ───────────────────────────────────────────────

def prompt_chars(messages, tools=None):
    """Characters of a prompt: every block's text (llm_providers
    .message_text) and the tool definitions as JSON."""
    from backend.llm_providers import message_text
    chars = sum(len(message_text(m)) for m in messages or ())
    if tools:
        chars += len(json.dumps(tools, sort_keys=True, default=str))
    return chars


def assembled_prompt_size(chain, user_id, model_id, messages, msg_start,
                          nodes_end, tools=None, reads=frozenset(),
                          user=None, whole=False, dense_chars=0):
    """The completion task's measure of the prompt it has assembled,
    before the first call, as a Size (with the prompt's characters).

    *chain*: the task's node chain, nearest first; *msg_start*: {node id:
    index of its first message}; *nodes_end*: the index after the last
    node's messages (the notes follow).

    With a base: the base's part is measured again in the characters it
    has now (an entry edited, a quoted entry grown or deleted since: the
    difference counts both ways), and what was written since in its
    characters (quotes and placeholders resolved), all at the ratio.
    *whole* (a Read turn, whose day of tweets sits in the base's part as
    a stub) or no base: the whole prompt at the ratio. *dense_chars*:
    characters of tweets in the prompt, counted at the tweets prior."""
    chars = prompt_chars(messages, tools)
    found, base, per_unit, certain, model_id = _resolve(
        chain, user_id, model_id, reads, user)
    per_char = per_unit / CHARS_PER_UNIT
    dense_per_char = (TOKENS_PER_UNIT_PRIOR["tweets"][_family(model_id)]
                      / CHARS_PER_UNIT)
    dense_chars = min(dense_chars, chars)
    start = None
    if found is not None and not whole:
        start = (nodes_end if found.through == 0
                 else msg_start.get(chain[found.through - 1].id))
    if start is None:
        tokens = ((chars - dense_chars) * per_char
                  + dense_chars * dense_per_char)
        certain = certain and found is not None and not dense_chars
    else:
        if found.chars:
            # The base's part as it is now (with the base's own ratio on
            # its tokenizer this is found.tokens plus the characters it
            # gained or lost since, at that ratio).
            base = prompt_chars(messages[:start], tools) * per_char
        # else: a base from before prompt_chars, on this tokenizer: its
        # tokens as measured.
        since = prompt_chars(messages[start:])
        tokens = base + since * per_char
    tokens = int(max(tokens, 0))
    return Size(tokens, certain, size_reason(tokens, chain, per_unit), chars)


def record_refusal(user_id, model_id, node_id, tokens, chars=None):
    """The $0 cost row of a refusal the completion task measured: no
    call was made. *node_id* is the node the reply was asked under, so
    the next estimate on the path finds it (latest_measurement). *chars*
    only when the size was certain (a count, or the thread's own ratio):
    a row without them is never a base. The caller commits."""
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
