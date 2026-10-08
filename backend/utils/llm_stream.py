"""A reply shown (and in voice, spoken) while the model writes it (#367).

ReplyStream is the listener one model call gets (the StreamListener
interface in backend/llm_providers.py, by duck typing). It keeps the text
so far, writes it to ``Node.streaming_content`` for the browser
(PartialTextWriter), and in voice passes it to the node's speech stream
(backend/utils/tts_stream.py). The node's ``content`` is untouched until
the reply is final.
"""
import logging
import re
import time

from sqlalchemy import update

from backend.extensions import db
from backend.models import Node
from backend.utils.encryption import encrypt_with_dek, new_dek
from backend.utils.timefmt import _LEADING_STAMPS_RE, _TRAILING_STAMPS_RE
from backend.utils.tts_stream_text import _is_marker_prefix, _is_stamp_prefix

logger = logging.getLogger(__name__)

# How often the partial text is written to the node. Each write is one
# small UPDATE; the browser's stream polls at the same pace.
# INTRODUCED CONSTANT (#367): not tuned; 0.5 s reads as live without
# writing on every token.
PARTIAL_WRITE_INTERVAL_SECS = 0.5

# Appended to a reply the provider stopped sending part-way (an error
# after text had arrived): the node keeps what was written and says so,
# in text and in voice, instead of failing as if nothing had been said.
CUT_OFF_NOTE = ("\n\n*(The reply was cut off here: the model provider "
                "failed while writing it. Ask again to get the rest.)*")

_TAIL_BRACKET_RE = re.compile(r"\[[^\]\n]*$")
_TAIL_BRACE_RE = re.compile(r"\{[^}\n]*$")


def partial_display_text(raw, canonicalize=None):
    """The partial text as the browser may show it. The same rules the
    final text gets, applied to a text that may still grow: quote labels
    are made absolute (*canonicalize*), timestamps at the edges are
    dropped, and a quote marker or timestamp cut off at the end is held
    back so it never renders half-written."""
    text = canonicalize(raw) if canonicalize else raw
    text = _LEADING_STAMPS_RE.sub("", text)
    stripped = text.lstrip()
    if stripped.startswith("[") and "\n" not in stripped \
            and _is_stamp_prefix(stripped):
        return ""
    text = _TRAILING_STAMPS_RE.sub("", text)
    m = _TAIL_BRACKET_RE.search(text)
    if m and _is_stamp_prefix(m.group(0)):
        text = text[:m.start()]
    m = _TAIL_BRACE_RE.search(text)
    if m and _is_marker_prefix(m.group(0)):
        text = text[:m.start()]
    return text


class PartialTextWriter:
    """Keeps ``Node.streaming_content`` current while a reply streams:
    throttled, encrypted with one DEK for the whole generation (one KMS
    wrap, not one per write), and written without bumping ``updated_at``
    (the embedding sweep keys on it and must not pick up a node
    mid-generation). Runs in the task's thread, on its session."""

    def __init__(self, node, canonicalize=None, clock=time.monotonic):
        self._node_id = node.id
        self._public = node.privacy_level == "public"
        self._canonicalize = canonicalize
        self._clock = clock
        self._dek = None
        self._raw = []
        self._written = ""
        self._last_write = None

    def feed(self, text):
        self._raw.append(text)
        now = self._clock()
        if (self._last_write is None
                or now - self._last_write >= PARTIAL_WRITE_INTERVAL_SECS):
            self.flush()

    def reset(self):
        self._raw = []
        self.flush()

    def flush(self):
        self._last_write = self._clock()
        text = partial_display_text("".join(self._raw), self._canonicalize)
        if text == self._written:
            return
        if text and not self._public and self._dek is None:
            self._dek = new_dek()
        value = (text if self._public else encrypt_with_dek(text, self._dek))
        db.session.execute(
            update(Node).where(Node.id == self._node_id).values(
                streaming_content=value or None,
                updated_at=Node.updated_at))
        db.session.commit()
        self._written = text


class ReplyStream:
    """The listener for one model call writing *node*'s reply.

    ``speech`` is the node's NodeSpeech in a voice turn (None otherwise):
    it gets the same text, and a restart is refused once any of it has
    been cut for speaking. ``show`` writes the partial text for the
    browser.

    Both are best-effort: a failure there (a KMS wrap, the mid-stream
    UPDATE, a bug in the speech side) is logged and switches that side
    off. It must never propagate into the provider's stream loop, which
    would abort the model call itself."""

    def __init__(self, node, canonicalize=None, speech=None, show=True):
        self._node_id = node.id
        self._parts = []
        self._writer = (PartialTextWriter(node, canonicalize)
                        if show else None)
        self._speech = speech

    @property
    def text(self):
        return "".join(self._parts)

    def _writer_call(self, method, *args):
        if self._writer is None:
            return
        try:
            getattr(self._writer, method)(*args)
        except Exception:
            logger.exception("Partial text for node %s: %s failed; not "
                             "showing it live any more", self._node_id,
                             method)
            self._writer = None
            try:
                db.session.rollback()
            except Exception:
                logger.exception("Rollback after a partial-text failure")

    def _speech_call(self, method, *args):
        if self._speech is None:
            return None
        try:
            return getattr(self._speech, method)(*args)
        except Exception:
            logger.exception("Live speech for node %s: %s failed; not "
                             "speaking it while written any more",
                             self._node_id, method)
            self._speech = None
            return None

    def on_text(self, text):
        if not text:
            return
        self._parts.append(text)
        self._writer_call("feed", text)
        self._speech_call("feed", text)

    def on_tool_call(self, name):
        # The text before a tool call is the round's whole text: show it
        # all and let the speech finish it now, not after the tool input
        # has streamed too. A round with no text is closed later, with
        # the fallback line it speaks instead.
        self._writer_call("flush")
        if self._parts:
            self._speech_call("close")

    def on_restart(self):
        if self._speech is not None and self._speech_call("restart") is False:
            return False
        self._parts = []
        self._writer_call("reset")
        return True

    def cut_off(self, note=CUT_OFF_NOTE):
        """The reply as it stood when the provider failed, with the *note*
        that says so (also spoken), as a response dict for _finalize. No
        tool calls: the round never completed."""
        self.on_text(note)
        return {
            "content": self.text,
            "tool_calls": [],
            "truncated": False,
            "cut_off": True,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
