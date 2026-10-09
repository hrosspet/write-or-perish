"""What Listen says for links (#461).

A Markdown link ``[text](url)`` is spoken as its text, an image
``![alt](url)`` as its alt text (nothing when the alt text is empty). An
address — bare (``https://…``, ``www.…``), an autolink (``<https://…>``) or
a link whose text is its own address — is spoken short: a GitHub pull
request or issue as "PR 460" / "issue 423", any other address as "a link
to example.com". Code is left as written: fenced code blocks and inline
code spans. Only the text sent to TTS changes; the stored node does not.

LinkSpeaker does this on a stream of text: it releases text as soon as no
later text can change it and holds back an undecided tail (a ``[`` whose
link may still follow, an address that hasn't ended). ``speak_links``
runs the same machine on a whole text at once, so the batch TTS task and a
reply spoken while it is written say the same thing.

Bounded link matching: a link, an address and the heading or fence line
read at a line start have length limits (below), and link text nests
brackets only a few levels deep. Past a limit the text is read as written.
So the work is linear in the text, a streamed reply holds back at most
MAX_LINK_SOURCE characters, and a held link or address is scanned once,
resuming where it stopped when the next delta arrives.

Line-local simplifications (both paths share them): a link's text and
destination don't span lines; an inline code span opened by a backtick run
lasts until a run of the same length or the end of the line.
"""
import re

# INTRODUCED CONSTANTS (#461), bounds on what counts as a link or an
# address; longer text is read as written. Link text (also an image's alt
# text and a link title): sentences, not pages.
MAX_LINK_TEXT = 500
# A link destination, bare address or autolink: the common practical URL
# limit of browsers and servers.
MAX_ADDRESS = 2048
# Brackets inside link text, counting the link's own: enough for
# ``[![badge](img)](url)`` and ``[a [b] c](url)``.
MAX_BRACKET_DEPTH = 3
# A whole link: its text, destination and title, plus the brackets, quotes
# and spaces around them. Also the most a streamed reply holds back.
MAX_LINK_SOURCE = 2 * MAX_LINK_TEXT + MAX_ADDRESS + 32
# A ``#``/``##`` heading line or a code-fence line is read as one only up
# to this length (a longer one is an ordinary line here).
MAX_LINE_HEAD = 500

_SCHEMES = ("http://", "https://")
_ADDRESS_STARTS = _SCHEMES + ("www.",)
_PREFIX_LEN = max(len(p) for p in _ADDRESS_STARTS)
# Trailing characters that end a sentence rather than an address (the
# characters remark-gfm leaves out of an autolinked address, plus ; ' ").
_TRAILING_PUNCT = "?!.,:;*_~'\""
_ASCII_PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"

_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_GITHUB_REF_RE = re.compile(
    r"(?:www\.)?github\.com/[^/?#\s]+/[^/?#\s]+/(pull|issues)/(\d+)"
    r"(?:[/?#]\S*)?", re.IGNORECASE)
_FENCE_OPEN_RE = re.compile(r" {0,3}(`{3,}|~{3,})")
_FENCE_PREFIX_RE = re.compile(r" {0,3}(?:`{0,2}|~{0,2})")
_CHAPTER_START_RE = re.compile(r"#{1,2}[ \t]")
_CHAPTER_LINE_RE = re.compile(r"(#{1,2}[ \t]+)(.*)", re.DOTALL)
# Characters that may start something this module rewrites; anything else
# is released as it comes.
_PLAIN_RE = re.compile(r"[^\n`\\!\[<hHwW]+")
_TICKS_RE = re.compile(r"`+")
_CODE_TEXT_RE = re.compile(r"[^`\n]+")
_BLANKS_RE = re.compile(r"[ \t]*")
_TOKEN_END_RE = re.compile(r"[\s<]")
_ADDRESS_END_RE = _TOKEN_END_RE
_AUTOLINK_STOP_RE = re.compile(r"[\s<>]")
_LABEL_STOP_RE = re.compile(r"[\[\]\\\n]")
_ANGLE_DEST_STOP_RE = re.compile(r"[<>\\\n]")
_DEST_STOP_RE = re.compile(r"[\s()\[\]\\\x00-\x1f\x7f]")
_TITLE_STOP_RE = {c: re.compile(rf"[{re.escape(c)}\n\\]") for c in "\"')"}
_AUTOLINK_HEAD_RE = re.compile(r"<(?:https?://)", re.IGNORECASE)
# Link text that opens MAX_BRACKET_DEPTH more brackets before any closes
# (the scan would give up there too; this only finds it faster).
_LABEL_CHAR = r"(?:[^\[\]\\\n]|\\[^\n])*"
_TOO_DEEP_RE = re.compile((_LABEL_CHAR + r"\[") * MAX_BRACKET_DEPTH)
_ADDRESS_HEAD_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

_WAIT = None   # more text could change the decision
_MORE = "more"  # a link step waits for the next character
_NO = False    # decided: not a link / address here


def speak_address(url):
    """How an address is spoken: "PR 460", "issue 423" for a GitHub pull
    request or issue, otherwise "a link to <domain>" (without www.)."""
    m = _SCHEME_RE.match(url)
    rest = url[m.end():] if m else url
    ref = _GITHUB_REF_RE.fullmatch(rest)
    if ref:
        kind = "PR" if ref.group(1).lower() == "pull" else "issue"
        return f"{kind} {ref.group(2)}"
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = host.rpartition("@")[2].split(":", 1)[0].strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"a link to {host}" if host else url


def _bare(address):
    """An address without scheme, www. and trailing slash, for comparing
    a link's text with its destination."""
    m = _SCHEME_RE.match(address)
    rest = (address[m.end():] if m else address).lower().rstrip("/")
    return rest[4:] if rest.startswith("www.") else rest


def _trim_address(url):
    """Drop trailing punctuation and unbalanced closing brackets, which
    belong to the sentence around a bare address."""
    opens = {")": url.count("("), "]": url.count("[")}
    closes = {")": url.count(")"), "]": url.count("]")}
    end = len(url)
    while end:
        last = url[end - 1]
        if last in _TRAILING_PUNCT:
            end -= 1
        elif last in closes and closes[last] > opens[last]:
            closes[last] -= 1
            end -= 1
        else:
            break
    return url[:end]


def _seek(regex, s, base, pos, stop):
    """Offset (from *base*) of the first match of *regex* in
    s[base+pos : base+stop], or -1."""
    m = regex.search(s, base + pos, base + stop)
    return m.start() - base if m else -1


def _escape_step(s, base, k):
    """Offset after a backslash at s[base+k]: past the escaped character,
    or past the backslash alone before a line break. None when the next
    character hasn't arrived."""
    if base + k + 1 >= len(s):
        return None
    return k + 1 if s[base + k + 1] == "\n" else k + 2


def _closer_step(state, piece, fence):
    """Track whether a code-block line can still be the closing fence
    (up to 3 spaces, the fence character at least as many times as the
    opening run, then only spaces). *state* is (phase, count), or None
    once the line can't close the block."""
    char, length = fence
    phase, count = state
    for ch in piece:
        if phase == 0:
            if ch == " " and count < 3:
                count += 1
                continue
            if ch == char:
                phase, count = 1, 1
                continue
            return None
        if phase == 1:
            if ch == char:
                count += 1
                continue
            if count >= length and ch in " \t\r":
                phase = 2
                continue
            return None
        if ch not in " \t\r":
            return None
    return phase, count


def _speak_inline(text):
    """Spoken form of a piece of one line (a link's text, a heading)."""
    speaker = LinkSpeaker()
    speaker._line_start = False
    speaker._buf = text
    return speaker._run(final=True)


def _speak_title(title):
    """A chapter title as spoken and shown in the player's chapter list.
    A title that would be left empty (only an image without alt text)
    stays as written: an empty heading isn't a chapter."""
    return _speak_inline(title).strip() or title


def speak_links(text):
    """The whole *text* with its links spoken (see module docstring)."""
    speaker = LinkSpeaker()
    return speaker.feed(text) + speaker.close()


# Phases of a link's parse.
(_LABEL, _OPEN, _DEST_BLANKS, _ANGLE_DEST, _DEST, _AFTER_DEST, _TITLE,
 _AFTER_TITLE, _CLOSE) = range(9)


class _LinkScan:
    """Where a link's parse stopped. Offsets count from the link's first
    character (the ``[``, or the ``!`` of an image), so a held link
    resumes after the buffer before it is released."""
    __slots__ = ("phase", "q", "depth", "part", "label_end", "dest_start",
                 "dest_end", "after_dest", "close")

    def __init__(self, label_start):
        self.phase = _LABEL
        self.q = self.part = label_start
        self.depth = 1
        self.label_end = self.dest_start = self.dest_end = 0
        self.after_dest = 0
        self.close = ""


class LinkSpeaker:
    """Text in, text with its links spoken out, for text that arrives in
    pieces. ``feed`` and ``close`` return the text released so far;
    ``heading`` takes a chapter heading that the caller took out of the
    text (a line ``# title`` / ``## title`` in ``speak_links``' input)."""

    def __init__(self):
        self._buf = ""
        self._line_start = True
        self._fence = None       # (char, run length) inside a code block
        self._closer = None      # can the code block's line close it?
        self._code = 0           # backtick run length of an open code span
        self._ticks = 0          # backticks of the run being read
        self._prev = "\n"        # last character consumed
        # An address longer than MAX_ADDRESS: the rest of its run of
        # non-space characters starts no address.
        self._long_token = False
        self._resume = None      # (kind, state) of the held construct
        self._resume_in = None
        # The first "](" at or after _rp_from in the text _rp_src (-1:
        # none), so a "[" that can't be a link is passed over at once.
        self._rp_src, self._rp_from, self._rp = None, 0, -1

    # ── public ──────────────────────────────────────────────────────────
    def feed(self, text):
        self._buf += text
        return self._run(final=False)

    def close(self):
        return self._run(final=True)

    def end_line(self):
        """The current line has ended without a newline in the text (a
        chapter heading follows): release whatever it held back."""
        return self._run(final=True)

    def heading(self, title):
        """A chapter heading taken out of the text: its title as spoken.
        Inside a code block it stays as written, as its line would."""
        self._line_start = True
        self._code = self._ticks = 0
        self._long_token = False
        self._resume = None
        if self._fence:
            self._closer = (0, 0)
        self._prev = "\n"
        return title if self._fence else _speak_title(title)

    # ── the loop ────────────────────────────────────────────────────────
    def _run(self, final):
        s, i, out = self._buf, 0, []
        self._resume_in, self._resume = self._resume, None
        while i < len(s):
            if self._fence:
                step = self._fenced(s, i)
            elif self._line_start:
                step = self._line_head(s, i, final)
            else:
                step = self._inline(s, i, final)
            if step is _WAIT:
                break
            text, j = step
            out.append(text)
            if j > i:
                self._prev = s[j - 1]
                self._resume_in = None
                if self._long_token and _TOKEN_END_RE.search(s, i, j):
                    self._long_token = False
            i = j
        self._buf = s[i:]
        return "".join(out)

    def _more(self, final, kind, state):
        """The held construct needs more text (at the end: it isn't one)."""
        if final:
            return _NO
        self._resume = (kind, state)
        return _WAIT

    def _resumed(self, kind, start):
        """The saved parse of the construct at *start*, if it was held."""
        held, self._resume_in = self._resume_in, None
        if start == 0 and held and held[0] == kind:
            return held[1]
        return None

    def _fenced(self, s, i):
        """Inside a code block: release as is, up to the end of the line,
        and see whether that line closes the block."""
        nl = s.find("\n", i)
        end = len(s) if nl < 0 else nl + 1
        if self._closer is not None:
            piece = s[i:nl] if nl >= 0 else s[i:]
            self._closer = _closer_step(self._closer, piece, self._fence)
        if nl >= 0:
            closer = self._closer
            if closer and (closer[0] == 2 or (
                    closer[0] == 1 and closer[1] >= self._fence[1])):
                self._fence = None
            self._closer = (0, 0)
            self._line_start = True
        return s[i:end], end

    def _line_head(self, s, i, final):
        """At the start of a line: a code fence, a chapter heading line,
        or an ordinary line."""
        nl = s.find("\n", i, i + MAX_LINE_HEAD + 1)
        if nl < 0 and len(s) - i > MAX_LINE_HEAD:
            self._line_start = False     # a long line is an ordinary one
            return "", i
        complete = nl >= 0 or final
        line = s[i:nl] if nl >= 0 else s[i:]
        end = nl + 1 if nl >= 0 else len(s)
        newline = "\n" if nl >= 0 else ""
        if line.startswith("#"):
            if _CHAPTER_START_RE.match(line):
                if not complete:
                    return _WAIT
                return self._chapter_line(line) + newline, end
            if line in ("#", "##") and not complete:
                return _WAIT
        else:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                if not complete:
                    return _WAIT   # a fence line is decided once it ends
                char = m.group(1)[0]
                if char == "~" or "`" not in line[m.end():]:
                    self._fence = (char, len(m.group(1)))
                    self._closer = (0, 0)
                    return line + newline, end
            elif not complete and _FENCE_PREFIX_RE.fullmatch(line):
                return _WAIT
        self._line_start = False
        return "", i

    def _chapter_line(self, line):
        """A ``#`` / ``##`` heading line, its title spoken as a streamed
        reply's chapter heading is."""
        m = _CHAPTER_LINE_RE.fullmatch(line)
        title = m.group(2).strip()
        spoken = _speak_title(title)
        return line if spoken == title else m.group(1) + spoken

    def _end_ticks(self):
        """A backtick run has ended: it opens or closes a code span."""
        run, self._ticks = self._ticks, 0
        if not self._code:
            self._code = run
        elif self._code == run:
            self._code = 0

    def _inline(self, s, i, final):
        c = s[i]
        if c == "`":
            # Released as they come; the run's length decides the code
            # span once a different character follows.
            m = _TICKS_RE.match(s, i)
            self._ticks += m.end() - i
            return m.group(0), m.end()
        if self._ticks:
            self._end_ticks()
        if c == "\n":
            self._line_start = True
            self._code = 0
            return c, i + 1
        if self._code:
            m = _CODE_TEXT_RE.match(s, i)
            return m.group(0), m.end()
        if c == "\\":
            if i + 1 == len(s) and not final:
                return _WAIT
            if i + 1 < len(s) and s[i + 1] in _ASCII_PUNCT:
                return s[i:i + 2], i + 2
            return c, i + 1
        if c == "!":
            if i + 1 == len(s) and not final:
                return _WAIT
            if i + 1 < len(s) and s[i + 1] == "[":
                step = self._link(s, i, final, image=True)
                if step is not _NO:
                    return step
            return c, i + 1
        if c == "[":
            step = self._link(s, i, final, image=False)
            return (c, i + 1) if step is _NO else step
        if c == "<":
            step = self._autolink(s, i, final)
            return (c, i + 1) if step is _NO else step
        if c in "hHwW":
            if (self._long_token or self._prev.isalnum()
                    or self._prev in "@.-/"):
                # Inside a word, an email, a path or an over-long
                # address: not an address start.
                return c, i + 1
            step = self._address(s, i, final)
            return (c, i + 1) if step is _NO else step
        m = _PLAIN_RE.match(s, i)
        return m.group(0), m.end()

    # ── links ───────────────────────────────────────────────────────────
    def _link(self, s, b, final, image):
        """``[text](destination "title")`` starting at s[b] (an image when
        s[b] is the "!"). Returns (spoken, end), _WAIT or _NO."""
        kind = "image" if image else "link"
        st = self._resumed(kind, b)
        if st is None:
            st = _LinkScan(2 if image else 1)
            label_start = b + st.part
            if (not self._may_close(s, label_start, final)
                    or _TOO_DEEP_RE.match(
                        s, label_start, label_start + MAX_LINK_TEXT + 1)):
                return _NO
        n = len(s) - b
        while True:
            q = st.q
            if q > MAX_LINK_SOURCE:
                return _NO
            if q >= n:
                return self._more(final, kind, st)
            phase = st.phase
            if phase == _OPEN:
                if s[b + q] != "(":
                    return _NO
                st.q, st.phase = q + 1, _DEST_BLANKS
                continue
            if phase == _CLOSE:
                if s[b + q] != ")":
                    return _NO
                break
            if phase == _LABEL:
                result = self._label_step(s, b, n, st)
            elif phase in (_ANGLE_DEST, _DEST):
                result = self._dest_step(s, b, n, st)
            elif phase == _TITLE:
                result = self._title_step(s, b, n, st)
            else:
                result = self._blanks_step(s, b, n, st)
            if result is False:
                return _NO
            if result is _MORE:
                return self._more(final, kind, st)
        start = 2 if image else 1
        label = s[b + start:b + st.label_end]
        dest = s[b + st.dest_start:b + st.dest_end]
        if (label and _ADDRESS_HEAD_RE.match(dest)
                and _bare(label) == _bare(dest)):
            # A link whose text is its own web address reads as an
            # address; a relative one ("README.md") keeps its text.
            spoken = speak_address(dest)
        else:
            spoken = _speak_inline(label)
        return spoken, b + st.q + 1

    def _may_close(self, s, label_start, final):
        """Is there a "](" where this link's text could end? (Only a
        shortcut: the scan decides.)"""
        if (self._rp_src is not s or self._rp_from > label_start
                or 0 <= self._rp < label_start):
            self._rp_src, self._rp_from = s, label_start
            self._rp = s.find("](", label_start)
        last = label_start + MAX_LINK_TEXT     # the latest "]" allowed
        if self._rp >= 0:
            return self._rp <= last
        return not final and len(s) <= last + 1

    # Each step scans its part of the link from st.q and returns True
    # (the part is done, or the text so far is used up: st.q says where it
    # stopped), False (not a link) or _MORE (a backslash at st.q waits for
    # the character it escapes).
    @staticmethod
    def _label_step(s, b, n, st):
        stop = min(n, st.part + MAX_LINK_TEXT + 1)
        search = _LABEL_STOP_RE.search
        q, depth = st.q, st.depth
        while True:
            m = search(s, b + q, b + stop)
            if m is None:
                st.q, st.depth = max(stop, q), depth
                return stop - st.part <= MAX_LINK_TEXT
            k = m.start() - b
            ch = s[b + k]
            if ch == "]":
                depth -= 1
                q = k + 1
                if depth == 0:
                    st.q, st.depth, st.label_end, st.phase = q, 0, k, _OPEN
                    return True
            elif ch == "[":
                depth += 1
                if depth > MAX_BRACKET_DEPTH:
                    return False
                q = k + 1
            elif ch == "\\":
                q = _escape_step(s, b, k)
                if q is None:
                    st.q, st.depth = k, depth
                    return _MORE
            else:   # a line break
                return False

    @staticmethod
    def _blanks_step(s, b, n, st):
        end = b + min(n, MAX_LINK_SOURCE + 1)
        k = _BLANKS_RE.match(s, b + st.q, end).end() - b
        st.q = k
        if k >= n or k > MAX_LINK_SOURCE:
            return True
        ch = s[b + k]
        if st.phase == _DEST_BLANKS:
            if ch == "<":
                st.phase, st.dest_start, st.q = _ANGLE_DEST, k + 1, k + 1
            else:
                st.phase, st.dest_start, st.depth = _DEST, k, 0
        elif st.phase == _AFTER_DEST and ch in "\"'(" and k > st.after_dest:
            st.phase, st.part, st.q = _TITLE, k + 1, k + 1
            st.close = ")" if ch == "(" else ch
        else:
            st.phase = _CLOSE
        return True

    @staticmethod
    def _dest_step(s, b, n, st):
        angle = st.phase == _ANGLE_DEST
        stop = min(n, st.dest_start + MAX_ADDRESS + 1)
        search = (_ANGLE_DEST_STOP_RE if angle else _DEST_STOP_RE).search
        q, depth = st.q, st.depth
        while True:
            m = search(s, b + q, b + stop)
            if m is None:
                st.q, st.depth = max(stop, q), depth
                return stop - st.dest_start <= MAX_ADDRESS
            k = m.start() - b
            ch = s[b + k]
            if ch == "\\":
                q = _escape_step(s, b, k)
                if q is None:
                    st.q, st.depth = k, depth
                    return _MORE
            elif angle:
                if ch != ">":
                    return False
                st.dest_end, st.after_dest = k, k + 1
                st.q, st.phase = k + 1, _AFTER_DEST
                return True
            elif ch == "(":
                depth += 1
                q = k + 1
            elif ch == ")" and depth:
                depth -= 1
                q = k + 1
            elif ch in "[]":
                return False
            else:   # a space, a control character or the closing ")"
                st.dest_end = st.after_dest = st.q = k
                st.depth, st.phase = depth, _AFTER_DEST
                return True

    @staticmethod
    def _title_step(s, b, n, st):
        stop = min(n, st.part + MAX_LINK_TEXT + 1)
        search = _TITLE_STOP_RE[st.close].search
        q = st.q
        while True:
            m = search(s, b + q, b + stop)
            if m is None:
                st.q = max(stop, q)
                return stop - st.part <= MAX_LINK_TEXT
            k = m.start() - b
            ch = s[b + k]
            if ch == "\n":
                return False
            if ch != "\\":
                st.q, st.phase = k + 1, _AFTER_TITLE
                return True
            q = _escape_step(s, b, k)
            if q is None:
                st.q = k
                return _MORE

    def _autolink(self, s, p, final):
        """``<https://…>``. Returns (spoken, end), _WAIT or _NO."""
        n = len(s)
        scanned = self._resumed("autolink", p)
        if scanned is None:
            m = _AUTOLINK_HEAD_RE.match(s, p)
            if m is None:
                head = s[p + 1:p + 1 + _PREFIX_LEN].lower()
                if (p + 1 + len(head) >= n and not final
                        and any(x.startswith(head) for x in _SCHEMES)):
                    return _WAIT
                return _NO
            scanned = m.end() - p
        stop = min(n, p + 2 + MAX_ADDRESS)
        k = _seek(_AUTOLINK_STOP_RE, s, p, scanned, stop - p)
        if k < 0:
            if stop - p - 1 > MAX_ADDRESS:
                return _NO
            return self._more(final, "autolink", stop - p)
        if s[p + k] != ">":
            return _NO
        return speak_address(s[p + 1:p + k]), p + k + 1

    def _address(self, s, i, final):
        """A bare ``http(s)://…`` or ``www.…`` address starting at s[i].
        It runs to the next whitespace or ``<``, without the trailing
        punctuation around it. Returns (spoken, end), _WAIT or _NO."""
        n = len(s)
        held = self._resumed("address", i)
        if held is None:
            m = _ADDRESS_HEAD_RE.match(s, i)
            if m is None:
                head = s[i:i + _PREFIX_LEN].lower()
                if (i + len(head) >= n and not final
                        and any(x.startswith(head) for x in _ADDRESS_STARTS)):
                    return _WAIT
                return _NO
            held = (m.end() - i, m.end() - i)
        prefix, scanned = held
        stop = min(n, i + MAX_ADDRESS + 1)
        k = _seek(_ADDRESS_END_RE, s, i, scanned, stop - i)
        if k < 0:
            if stop - i > MAX_ADDRESS:
                self._long_token = True
                return _NO
            if not final:
                return self._more(final, "address", (prefix, stop - i))
            k = n - i
        url = _trim_address(s[i:i + k])
        if len(url) <= prefix:
            return _NO
        return speak_address(url), i + len(url)
