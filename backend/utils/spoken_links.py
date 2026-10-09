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

Line-local simplifications (both paths share them): a link's text and
destination don't span lines; an inline code span opened by a backtick run
lasts until a run of the same length or the end of the line.
"""
import re

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

_WAIT = None   # more text could change the decision
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
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
        elif last == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        elif last == "]" and url.count("]") > url.count("["):
            url = url[:-1]
        else:
            break
    return url


def _escapes(s, i):
    """Does the backslash at s[i] escape the next character? (Not a line
    break; at the end of the text the answer waits for more.)"""
    return s[i] == "\\" and (i + 1 >= len(s) or s[i + 1] != "\n")


def _skip_blanks(s, i):
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


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


class LinkSpeaker:
    """Text in, text with its links spoken out, for text that arrives in
    pieces. ``feed`` and ``close`` return the text released so far;
    ``heading`` takes a chapter heading that the caller took out of the
    text (a line ``# title`` / ``## title`` in ``speak_links``' input)."""

    def __init__(self):
        self._buf = ""
        self._line_start = True
        self._fence = None       # (char, run length) inside a code block
        self._fence_line = ""    # the code block's current line so far
        self._code = 0           # backtick run length of an open code span
        self._prev = "\n"        # last character consumed

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
        self._code = 0
        self._fence_line = ""
        self._prev = "\n"
        return title if self._fence else _speak_title(title)

    # ── the loop ────────────────────────────────────────────────────────
    def _run(self, final):
        s, i, out = self._buf, 0, []
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
            i = j
        self._buf = s[i:]
        return "".join(out)

    def _fenced(self, s, i):
        """Inside a code block: release as is, up to the end of the line,
        and see whether that line closes the block."""
        nl = s.find("\n", i)
        end = len(s) if nl < 0 else nl + 1
        self._fence_line += s[i:end]
        if nl >= 0:
            char, length = self._fence
            if re.fullmatch(rf" {{0,3}}{re.escape(char)}{{{length},}}[ \t\r]*\n",
                            self._fence_line):
                self._fence = None
            self._fence_line = ""
            self._line_start = True
        return s[i:end], end

    def _line_head(self, s, i, final):
        """At the start of a line: a code fence, a chapter heading line,
        or an ordinary line."""
        nl = s.find("\n", i)
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
                    self._fence_line = ""
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

    def _inline(self, s, i, final):
        c = s[i]
        if c == "\n":
            self._line_start = True
            self._code = 0
            return c, i + 1
        if c == "`":
            j = i
            while j < len(s) and s[j] == "`":
                j += 1
            if j == len(s) and not final:
                return _WAIT
            run = j - i
            if not self._code:
                self._code = run
            elif self._code == run:
                self._code = 0
            return s[i:j], j
        if self._code:
            j = i
            while j < len(s) and s[j] not in "`\n":
                j += 1
            return s[i:j], j
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
                step = self._link(s, i + 1, final, image=True)
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
            if self._prev.isalnum() or self._prev in "@.-/":
                # Inside a word, an email or a path: not an address start.
                return c, i + 1
            step = self._address(s, i, final)
            return (c, i + 1) if step is _NO else step
        m = _PLAIN_RE.match(s, i)
        return m.group(0), m.end()

    # ── links ───────────────────────────────────────────────────────────
    @staticmethod
    def _undecided(final):
        return _NO if final else _WAIT

    def _link(self, s, p, final, image):
        """``[text](destination "title")`` with s[p] == "[" (an image when
        "!" precedes it). Returns (spoken, end), _WAIT or _NO."""
        n, q, depth = len(s), p, 0
        while True:
            if q >= n:
                return self._undecided(final)
            ch = s[q]
            if ch == "\n":
                return _NO
            if _escapes(s, q):
                q += 2
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            q += 1
        label = s[p + 1:q]
        if q + 1 >= n:
            return self._undecided(final)
        if s[q + 1] != "(":
            return _NO
        tail = self._link_tail(s, q + 2, final)
        if tail is _WAIT or tail is _NO:
            return tail
        end, dest = tail
        if label and _bare(label) == _bare(dest) and _bare(dest):
            # A link whose text is its own address reads as an address.
            spoken = speak_address(dest)
        else:
            spoken = _speak_inline(label)
        return spoken, end

    def _link_tail(self, s, r, final):
        """The part after ``](``: destination, optional title, ``)``.
        Returns (end, destination), _WAIT or _NO."""
        r = _skip_blanks(s, r)
        if r >= len(s):
            return self._undecided(final)
        found = self._destination(s, r, final)
        if found is _WAIT or found is _NO:
            return found
        dest, spaced = found
        r = _skip_blanks(s, spaced)
        if r < len(s) and s[r] in "\"'(" and r > spaced:
            r = self._title_end(s, r, final)
            if r is _WAIT or r is _NO:
                return r
            r = _skip_blanks(s, r)
        if r >= len(s):
            return self._undecided(final)
        if s[r] != ")":
            return _NO
        return r + 1, dest

    def _destination(self, s, r, final):
        """A link destination at s[r]: ``<…>`` or a run without spaces and
        with balanced parentheses. Returns (destination, end), _WAIT or
        _NO."""
        n = len(s)
        if s[r] == "<":
            e = r + 1
            while e < n and s[e] not in "<>\n":
                e += 2 if _escapes(s, e) else 1
            if e >= n:
                return self._undecided(final)
            return (s[r + 1:e], e + 1) if s[e] == ">" else _NO
        e, depth = r, 0
        while e < n:
            ch = s[e]
            if _escapes(s, e):
                e += 2
                continue
            if ch.isspace() or ord(ch) < 32:
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            e += 1
        if e >= n:
            return self._undecided(final)
        return s[r:e], e

    def _title_end(self, s, r, final):
        """A link title quoted with " ' or ( ) at s[r]: the index after
        it, _WAIT or _NO."""
        close = ")" if s[r] == "(" else s[r]
        e = r + 1
        while e < len(s) and s[e] != close:
            if s[e] == "\n":
                return _NO
            e += 2 if _escapes(s, e) else 1
        if e >= len(s):
            return self._undecided(final)
        return e + 1

    def _autolink(self, s, p, final):
        """``<https://…>``. Returns (spoken, end), _WAIT or _NO."""
        n = len(s)
        head = s[p + 1:p + 1 + _PREFIX_LEN].lower()
        if not any(head.startswith(sch) for sch in _SCHEMES):
            if (p + 1 + len(head) >= n and not final
                    and any(sch.startswith(head) for sch in _SCHEMES)):
                return _WAIT
            return _NO
        e = p + 1
        while e < n and s[e] != ">":
            if s[e].isspace() or s[e] == "<":
                return _NO
            e += 1
        if e >= n:
            return self._undecided(final)
        return speak_address(s[p + 1:e]), e + 1

    def _address(self, s, i, final):
        """A bare ``http(s)://…`` or ``www.…`` address starting at s[i].
        It runs to the next whitespace or ``<``, without the trailing
        punctuation around it. Returns (spoken, end), _WAIT or _NO."""
        n = len(s)
        head = s[i:i + _PREFIX_LEN].lower()
        start = next((p for p in _ADDRESS_STARTS if head.startswith(p)), None)
        if start is None:
            if (i + len(head) >= n and not final
                    and any(p.startswith(head) for p in _ADDRESS_STARTS)):
                return _WAIT
            return _NO
        e = i
        while e < n and not s[e].isspace() and s[e] != "<":
            e += 1
        if e >= n and not final:
            return _WAIT
        url = _trim_address(s[i:e])
        if len(url) <= len(start):
            return _NO
        return speak_address(url), i + len(url)
