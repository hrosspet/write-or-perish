"""Text side of speaking a voice reply while it is written (#367 part 4).

Two pure pieces; the thread that turns chunks into audio is
backend/utils/tts_stream.py.

SpokenTextProjector is the streaming version of the text preparation the
batch TTS task does on a finished reply (``_strip_quote_markers``,
``strip_edge_timestamps``, ``_strip_heading_sections``). It takes the raw
text deltas and releases a character only once no later text can scrub or
restructure it: an ambiguous tail — the start of a quote marker, of a
timestamp, of a heading or a share fence — is held until it is decided.
Its output is a list of events: ("text", str) and ("heading", title) for
an h1/h2 chapter heading.

ChunkPlanner is the streaming version of ``section_aware_chunk_text``: it
keeps the released text per section and cuts chunks that stay inside one
section, only at sentence boundaries that released text has confirmed, so
whether a chunk ends its section is known when it is cut. It does not
decide chunk sizes; the TTS thread asks it for a chunk of at most N chars
whenever it is free (see tts_stream.py for the size rule).
"""
import re
from collections import deque

from backend.utils.audio_processing import (
    MIN_FIRST_CHUNK_CHARS, TTS_AUDIO_SECS_PER_CHAR, TTS_CHUNK_OVERHEAD_SECS,
    TTS_GEN_CHARS_PER_SEC, TTS_MAX_CHARS, _split_at_sentence, _split_at_word)
from backend.utils.timefmt import _EDGE_STAMP_RE

_STAMP_RE = re.compile(_EDGE_STAMP_RE)
_STAMP_TEMPLATE = "[dddd-dd-dd dd:dd "
_UNKNOWN_STAMP = "[unknown time]"
_STAMP_ZONE_RE = re.compile(r"[A-Za-z+\-0-9:]{0,6}")

# Any quote marker: labels ({quote:A}, canonicalized only at finalize) and
# absolute ids ({quote:123}, {quote_ext:9}). Batch TTS strips the absolute
# form; a streamed reply still carries labels.
_MARKER_RE = re.compile(r"\{quote(?:_ext)?:[A-Za-z0-9]+\}")
_MARKER_PREFIX_RE = re.compile(r"\{quote(?:_ext)?:[A-Za-z0-9]*")
_MAX_MARKER_LEN = 40

_FENCE_OPEN_RE = re.compile(r":::share(?:[ \t]+\w+)?[ \t]*", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r":::[ \t]*")
_HEADING_LINE_RE = re.compile(r"(#{1,6})[ \t]+(.*)")

# ### headings the proposal detectors in llm_completion.py key on
# (_detect_todo/github_issue/feedback/share_proposal). A block they start
# is shown as a card, not spoken.
_TODO_KEYWORDS = ("completed", "new task", "priority")
_VALUE_HEADINGS = ("category", "feedback category", "share type")


def _is_structural_heading(h):
    return (any(k in h for k in _TODO_KEYWORDS)
            or "issue title" in h or h == "title" or "description" in h
            or h in ("feedback", "share"))


def _is_stamp_prefix(s):
    """Could *s* (starting with '[') still become an edge timestamp?"""
    if _UNKNOWN_STAMP.startswith(s):
        return True
    for i, ch in enumerate(s):
        if i >= len(_STAMP_TEMPLATE):
            return bool(_STAMP_ZONE_RE.fullmatch(s[len(_STAMP_TEMPLATE):]))
        want = _STAMP_TEMPLATE[i]
        if want == "d":
            if not ("0" <= ch <= "9"):
                return False
        elif ch != want:
            return False
    return True


def _is_marker_prefix(s):
    """Could *s* (starting with '{') still become a quote marker?"""
    if len(s) > _MAX_MARKER_LEN:
        return False
    return ("{quote_ext:".startswith(s) or "{quote:".startswith(s)
            or bool(_MARKER_PREFIX_RE.fullmatch(s)))


class SpokenTextProjector:
    """Raw reply deltas in, spoken-text events out (see module docstring).

    Modes, for the proposal blocks (only with ``proposals=True``, i.e. an
    agentic reply): NORMAL speaks; STRUCT is a proposal's structured part
    (suppressed); VALUE suppresses the one-line value under a
    ``### Category``-style heading and then speaks the TRAILING commentary
    until the next ``###``; NOTE speaks the ``### Note`` body. FENCE
    suppresses a ``:::share`` block (in every mode) until its closing
    ``:::`` line."""

    NORMAL, STRUCT, VALUE, TRAILING, NOTE, FENCE = range(6)
    _SPEAKING = (NORMAL, TRAILING, NOTE)

    def __init__(self, proposals=True):
        self.proposals = proposals
        self._buf = ""
        self._mode = self.NORMAL
        self._fence_return = self.NORMAL
        self._reply_start = True
        self._line_start = True
        # Complete timestamps (and the whitespace around them) that may be
        # the reply's trailing edge: released when more text follows,
        # dropped if the reply ends with them.
        self._held = ""
        self._events = []

    # ── public ──────────────────────────────────────────────────────────
    def feed(self, text):
        self._buf += text
        self._process(final=False)
        return self._take()

    def close(self):
        self._process(final=True)
        self._held = ""
        return self._take()

    # ── output ──────────────────────────────────────────────────────────
    def _take(self):
        events, self._events = self._events, []
        return events

    def _emit_text(self, text):
        if not text:
            return
        if self._events and self._events[-1][0] == "text":
            self._events[-1] = ("text", self._events[-1][1] + text)
        else:
            self._events.append(("text", text))

    def _release_held(self):
        if self._held:
            held, self._held = self._held, ""
            self._emit_text(held)

    # ── the loop ────────────────────────────────────────────────────────
    def _process(self, final):
        while self._buf:
            if self._reply_start:
                if not self._leading_edge(final):
                    return
                continue
            if self._line_start:
                if not self._line(final):
                    return
                continue
            if not self._inline(final):
                return

    def _leading_edge(self, final):
        """Drop timestamps at the very start (strip_edge_timestamps)."""
        stripped = self._buf.lstrip()
        if not stripped:
            if final:
                self._buf = ""
            return False
        if stripped[0] == "[":
            m = _STAMP_RE.match(stripped)
            if m:
                self._buf = stripped[m.end():]
                return True
            if not final and _is_stamp_prefix(stripped):
                return False
        # Batch TTS strips the whole text, so the reply starts at its
        # first non-space character (a heading there is a heading).
        self._buf = stripped
        self._reply_start = False
        return True

    def _line(self, final):
        """Classify the line starting at the buffer's head. Returns False
        to wait for more text."""
        nl = self._buf.find("\n")
        line = self._buf if nl < 0 else self._buf[:nl]
        complete = nl >= 0 or final
        kind = self._line_kind(line, complete)
        if kind == "wait":
            return False
        if kind == "inline":
            if line.strip():
                self._release_held()
            self._line_start = False
            return True
        if not complete:
            return False
        self._buf = self._buf[nl + 1:] if nl >= 0 else ""
        self._whole_line(line.rstrip("\r"))
        return True

    def _line_kind(self, line, complete):
        """'whole' — handle once the line is complete; 'inline' — an
        ordinary line, released as it arrives; 'wait' — can't tell yet."""
        if self._mode not in self._SPEAKING:
            return "whole"
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            if hashes == len(line):
                return "inline" if complete else "wait"
            special = hashes <= 2 or (hashes == 3 and self.proposals)
            return "whole" if special and line[hashes] in " \t" else "inline"
        if line.startswith(":"):
            low = line.lower()
            if ":::share".startswith(low) and not complete:
                return "wait"
            if low.startswith(":::share"):
                return "whole"
        return "inline"

    def _whole_line(self, line):
        mode = self._mode
        if mode == self.FENCE:
            if _FENCE_CLOSE_RE.fullmatch(line):
                self._mode = self._fence_return
            return
        if line.strip():
            # Text follows the held timestamps: they weren't the edge.
            self._release_held()
        if _FENCE_OPEN_RE.fullmatch(line):
            self._fence_return = mode
            self._mode = self.FENCE
            return
        m = _HEADING_LINE_RE.fullmatch(line)
        level = len(m.group(1)) if m else 0
        if level in (1, 2):
            # A chapter heading also ends a proposal block.
            self._mode = self.NORMAL
            self._events.append(("heading", m.group(2).strip()))
            return
        if level == 3 and self.proposals:
            h = m.group(2).strip().lower()
            if h in _VALUE_HEADINGS:
                self._mode = self.VALUE
                return
            if h == "note" and mode != self.NORMAL:
                self._mode = self.NOTE
                return
            if _is_structural_heading(h):
                self._mode = self.STRUCT
                return
            if mode == self.TRAILING:
                # The trailing commentary ends at the next ### heading.
                self._mode = self.STRUCT
                return
        if mode == self.VALUE:
            if line.strip():
                self._mode = self.TRAILING
            return
        if mode == self.STRUCT:
            return
        # A heading line spoken as-is (h3 in plain text, as batch does).
        self._emit_text(_MARKER_RE.sub("", line) + "\n")

    def _inline(self, final):
        """Release the current line's text up to its newline. Returns
        True once the line has ended, False when the rest of the buffer
        must wait for more text (or the buffer is used up)."""
        s = self._buf
        out = []
        i = 0
        ended = False
        while i < len(s):
            ch = s[i]
            if ch == "\n":
                if self._held:
                    self._held += ch
                else:
                    out.append(ch)
                i += 1
                self._line_start = ended = True
                break
            if ch == "{":
                m = _MARKER_RE.match(s, i)
                if m:
                    i = m.end()
                    continue
                if not final and _is_marker_prefix(s[i:]):
                    break
            elif ch == "[":
                m = _STAMP_RE.match(s, i)
                if m:
                    if not self._held:
                        self._emit_text("".join(out))
                        out = []
                    self._held += m.group(0)
                    i = m.end()
                    continue
                if not final and _is_stamp_prefix(s[i:]):
                    break
            if self._held:
                if ch in " \t\r":
                    self._held += ch
                    i += 1
                    continue
                self._emit_text("".join(out))
                out = []
                self._release_held()
            out.append(ch)
            i += 1
        self._emit_text("".join(out))
        self._buf = s[i:]
        return ended


class Chunk:
    __slots__ = ("text", "section_title", "section_index", "section_end")

    def __init__(self, text, section_title, section_index, section_end):
        self.text = text
        self.section_title = section_title
        self.section_index = section_index
        self.section_end = section_end

    def __repr__(self):
        return (f"Chunk({self.text!r}, {self.section_title!r}, "
                f"{self.section_index}, end={self.section_end})")


def _last_confirmed_boundary(text):
    """Index just past the last sentence end (.!? + whitespace, as
    _split_at_sentence) that released text follows, or 0."""
    for i in range(len(text) - 2, -1, -1):
        if text[i] in ".!?" and text[i + 1] in " \n\r\t":
            end = i + 1
            while end < len(text) and text[end] in " \n\r\t":
                end += 1
            if text[end:].strip():
                return end
    return 0


class ChunkPlanner:
    """Section-bounded chunks from the projector's events (see module
    docstring). Section indices count sections in arrival order the way
    ``split_sections`` enumerates them: a non-empty preamble is 0, each
    heading takes the next index, and a heading with no body before the
    next heading (or the end) produces no chunk."""

    def __init__(self):
        self._next_section = 0
        self._section = None          # (index, title) receiving text
        self._heading = None          # (index, title) seen, no body yet
        self._pending = ""
        # Ended sections not fully cut yet: [text, title, index, end].
        self._sealed = deque()
        self.closed = False
        self.cut = 0                  # chunks handed out so far

    @property
    def pending_chars(self):
        return len(self._pending) + sum(len(s[0]) for s in self._sealed)

    @property
    def done(self):
        return self.closed and not self._sealed and not self._pending

    def add(self, events):
        for kind, value in events:
            if kind == "heading":
                self._seal(section_end=True)
                self._heading = (self._next_section, value)
                self._next_section += 1
            else:
                self._add_text(value)

    def close(self):
        self._seal(section_end=False)
        if self._heading is not None and self._sealed \
                and self._sealed[-1][3]:
            # The reply ended on a heading with no body: the section
            # before it was the last one after all.
            self._sealed[-1][3] = False
        self.closed = True

    def _add_text(self, text):
        if self._section is None:
            if not text.strip():
                return
            text = text.lstrip()
            if self._heading is not None:
                index, title = self._heading
                self._heading = None
            else:
                index, title = self._next_section, None
                self._next_section += 1
            prefix = ""
            if title:
                spoken = title if title[-1] in ".!?" else f"{title}."
                prefix = f"{spoken}\n\n"
            self._section = (index, title)
            self._pending = prefix + text
        else:
            self._pending += text

    def _seal(self, section_end):
        if self._section is not None and self._pending.strip():
            index, title = self._section
            self._sealed.append(
                [self._pending.strip(), title, index, section_end])
        self._section = None
        self._pending = ""

    def _split(self, text, limit):
        split = _split_at_sentence(text, limit)
        if self.cut == 0 and split < MIN_FIRST_CHUNK_CHARS:
            split = _split_at_word(text, limit)
        return split

    def take(self, limit, jit=False):
        """The next chunk of at most *limit* chars, or None when there
        isn't one yet. Ended sections are cut first, whatever their size;
        the open section only once it holds more than *limit* chars (the
        cut then falls at a sentence boundary within the limit), or, with
        *jit* (text is arriving slowly and the queue is about to run
        out), at its last confirmed sentence boundary."""
        chunk = self._take(limit, jit)
        if chunk is not None:
            self.cut += 1
        return chunk

    def _take(self, limit, jit):
        if self._sealed:
            item = self._sealed[0]
            text, title, index, end = item
            if len(text) <= limit:
                self._sealed.popleft()
                return Chunk(text, title, index, end)
            split = self._split(text, limit)
            head, rest = text[:split].strip(), text[split:].strip()
            if not rest:
                self._sealed.popleft()
                return Chunk(head, title, index, end)
            item[0] = rest
            return Chunk(head, title, index, False)
        if self._section is None or not self._pending.strip():
            return None
        text = self._pending
        if len(text) > limit:
            split = self._split(text, limit)
            if not text[split:].strip():
                return None   # the boundary isn't confirmed yet
        elif jit:
            split = _last_confirmed_boundary(text)
            if not split:
                return None
        else:
            return None
        head = text[:split].strip()
        self._pending = text[split:].lstrip()
        index, title = self._section
        return Chunk(head, title, index, False)


# ── When to cut, and how big ────────────────────────────────────────────
# Same timing model as section_aware_chunk_text (#140 calibration): TTS
# generates R chars/s plus a fixed O s per chunk, audio plays P s per char.
R = TTS_GEN_CHARS_PER_SEC
P = TTS_AUDIO_SECS_PER_CHAR
O = TTS_CHUNK_OVERHEAD_SECS
FIRST_CHUNK_CHARS = min(int(3.0 * R), TTS_MAX_CHARS)
# INTRODUCED CONSTANT (#367): slack on the "cut now or the queue runs out"
# deadline, for error in the drain estimate (the server can't see the
# browser's player). A guess, not measured.
JIT_MARGIN_SECS = 1.0


class SpeechSchedule:
    """The size rule for streamed TTS (#367): the TTS worker takes the
    next chunk whenever it is free, sized by the time left until the
    browser's queue runs out (``drain_at``), from measured durations.

    With the whole text available at t=0 and a simulated clock this
    reproduces section_aware_chunk_text's schedule exactly as long as no
    stall is predicted; after a real stall (a tool round, a slow stream)
    it restarts from the arrival time instead of assuming playback went
    on. State is per turn: the browser plays the interim node and the
    continuation from one queue."""

    def __init__(self):
        self.drain_at = None

    def plan(self, now, pending_chars):
        """(limit, jit, wake_at) for a chunk taken at *now*: the size
        limit, whether to cut whatever is ready because the queue is
        about to run out, and when that moment comes (None when cold)."""
        if self.drain_at is None or self.drain_at - now <= O:
            # Cold: nothing queued, or nothing can land before the queue
            # runs out. A small chunk restarts playback fast.
            return FIRST_CHUNK_CHARS, False, None
        window = self.drain_at - now - O
        limit = max(min(int(max(window, 2.0) * R), TTS_MAX_CHARS), 1)
        wake_at = self.drain_at - O - pending_chars / R - JIT_MARGIN_SECS
        return limit, now >= wake_at, wake_at

    def played(self, duration, now):
        """A chunk of *duration* seconds reached the queue at *now*."""
        start = now if self.drain_at is None else max(self.drain_at, now)
        self.drain_at = start + duration
