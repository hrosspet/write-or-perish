"""Extract the text of a PDF as Markdown. Runs in a child process.

backend/utils/pdf_import.py starts this file as a script, in Python's
isolated mode (-I) with an empty environment:

    python -I pdf_extract_worker.py PATH CPU_SECONDS MEMORY_BYTES \
        MAX_PAGES MAX_CHARS

It sets the CPU-time and address-space limits before it imports pypdf,
then writes one JSON object to stdout: {"ok": true, ...} or
{"error": CODE, ...}. A crafted PDF can make a parser spin or allocate
without bound; here that only ends this process.

Only the standard library and pypdf are imported, so the script runs
without the app on the path. The tests import the clean-up functions
directly.

What the text goes through:
- per page: running headers and footers (lines repeated at the top or
  bottom of most pages) and lines that are only a page number are
  dropped;
- chapters come from the outline (bookmarks): its top level marks where
  each chapter starts, the entry's title is the chapter heading. With no
  usable outline the text is one part;
- words hyphenated across a line break are joined and lines are reflowed
  into paragraphs.
"""
import json
import math
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

# --- Clean-up heuristics (chosen, not measured; listed in the PR) -------
# How many non-empty lines at the top and at the bottom of each page are
# checked for running headers, footers and page numbers.
EDGE_LINES = 2
# A line (digits ignored) that is at the top, or at the bottom, of at
# least this share of the pages is a running header or footer, and a
# lone number is a page number when (number - page index) is the same on
# at least this share of the pages ...
REPEAT_SHARE = 0.5
# ... and on at least this many pages (so a 2-page PDF keeps its lines).
REPEAT_MIN_PAGES = 3
# Outline entries read at most (all levels); a bigger outline is ignored
# and the text is split by size.
MAX_OUTLINE_ENTRIES = 5000
# Chapters at most; with more, the outline is ignored and the text is
# split by size.
MAX_CHAPTERS = 200
# A line is "full" (its paragraph goes on in the next line) when it is at
# least this share of the document's median line length ...
FULL_LINE_RATIO = 0.75
# ... and at least this many characters. Shorter lines end a paragraph.
FULL_LINE_MIN_CHARS = 40
# A PDF with fewer non-whitespace characters than this per page, on
# average, counts as scanned (pictures of pages, no text layer).
SCANNED_CHARS_PER_PAGE = 25
# Longest chapter heading kept from an outline entry.
MAX_TITLE_CHARS = 200


class Fail(Exception):
    """A specific reason the PDF cannot be imported (see pdf_import)."""

    def __init__(self, code, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


# --- Text clean-up (pure functions) -------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LIGATURES = str.maketrans({
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
})
_WS_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")
_ROMAN = r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_PAGE_NUMBER_RE = re.compile(
    r"^[\s\-–—|·•]*"
    r"(?:(?:page|p\.)\s*)?"
    r"(?P<num>\d{1,4}|(?=[ivxlcdm])" + _ROMAN + r")"
    r"(?:\s*(?:/|of)\s*\d{1,4})?"
    r"[\s\-–—|·•]*$",
    re.IGNORECASE)
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500,
                 "m": 1000}
_BULLET_RE = re.compile(r"^(?:[•◦▪‣●■–\-*]\s+|\d{1,3}[.)]\s+)")
_BULLET_GLYPH_RE = re.compile(r"^[•◦▪‣●■]\s+")
_TITLE_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def clean_page_text(text):
    """Page text as lines: control characters out, ligatures spelled
    out, trailing whitespace dropped. Empty strings mark blank lines."""
    text = _CONTROL_RE.sub("", (text or "").replace("\r\n", "\n")
                           .replace("\r", "\n")).translate(_LIGATURES)
    return [line.rstrip() for line in text.split("\n")]


def _edge_key(line):
    return _WS_RE.sub(" ", _DIGITS_RE.sub("#", line.strip().lower()))


def _edge_indexes(lines):
    filled = [i for i, line in enumerate(lines) if line.strip()]
    return filled[:EDGE_LINES], filled[-EDGE_LINES:]


def is_page_number(line):
    return bool(line.strip()) and bool(_PAGE_NUMBER_RE.match(line))


def page_number_value(line):
    """The number a page-number-only line shows ("12", "- 12 -", "Page 12
    of 30", "xii"), or None for any other line."""
    m = _PAGE_NUMBER_RE.match(line) if line.strip() else None
    if not m or not m.group("num"):
        return None
    num = m.group("num").lower()
    if num.isdigit():
        return int(num)
    total = 0
    for ch, nxt in zip(num, num[1:] + " "):
        value = _ROMAN_VALUES[ch]
        total += -value if _ROMAN_VALUES.get(nxt, 0) > value else value
    return total


def strip_page_furniture(pages):
    """Drop running headers/footers and page numbers.

    pages: one list of lines per page. Only the EDGE_LINES first and last
    non-empty lines of a page are candidates, and with
    need = max(REPEAT_MIN_PAGES, REPEAT_SHARE x pages):
    - a line that is only a number goes when it follows the page
      sequence: its value minus the page index is the same on at least
      `need` pages (n on one page, n+1 on the next). Any other lone
      number at an edge (a program's output, a diagram label) stays;
    - any other line goes when the same line (digits ignored) sits in
      the same zone (top or bottom) on at least `need` pages.
    """
    need = max(REPEAT_MIN_PAGES, math.ceil(REPEAT_SHARE * len(pages)))
    top_counts, bottom_counts, offsets = Counter(), Counter(), Counter()
    edges = []
    for p, lines in enumerate(pages):
        top, bottom = _edge_indexes(lines)
        numbers = {i: page_number_value(lines[i]) for i in set(top + bottom)}
        numbers = {i: v - p for i, v in numbers.items() if v is not None}
        edges.append((top, bottom, numbers))
        offsets.update(set(numbers.values()))
        top_counts.update({_edge_key(lines[i]) for i in top
                           if i not in numbers})
        bottom_counts.update({_edge_key(lines[i]) for i in bottom
                              if i not in numbers})
    sequence = {off for off, n in offsets.items() if n >= need}
    top_repeated = {k for k, n in top_counts.items() if k and n >= need}
    bottom_repeated = {k for k, n in bottom_counts.items() if k and n >= need}
    out = []
    for lines, (top, bottom, numbers) in zip(pages, edges):
        drop = {i for i, off in numbers.items() if off in sequence}
        drop |= {i for i in top if i not in numbers
                 and _edge_key(lines[i]) in top_repeated}
        drop |= {i for i in bottom if i not in numbers
                 and _edge_key(lines[i]) in bottom_repeated}
        out.append([line for i, line in enumerate(lines) if i not in drop])
    return out


def median_line_length(lines):
    lengths = [len(line.strip()) for line in lines if line.strip()]
    return statistics.median(lengths) if lengths else 0


def _is_full(line, median):
    n = len(line.strip())
    return n >= FULL_LINE_MIN_CHARS and n >= FULL_LINE_RATIO * median


def _join(a, b):
    """Join two lines of one paragraph. A word hyphenated across the break
    is joined without the hyphen when the next line goes on in lower case
    ("exam-" + "ple"); before a capital or a digit the hyphen is part of
    the word and stays ("Capital-" + "Letter")."""
    if a.endswith("­"):
        return a[:-1] + b
    if len(a) >= 2 and a.endswith("-") and a[-2].isalpha() and b[:1].isalnum():
        return a[:-1] + b if b[:1].islower() else a + b
    return f"{a} {b}"


def _markdown_line(text):
    """A paragraph as Markdown: bullet glyphs become "- ", and a leading
    "#" is escaped so body text never turns into a heading."""
    text = _BULLET_GLYPH_RE.sub("- ", text)
    if text.startswith("#"):
        text = "\\" + text
    return text


_SENTENCE_END_RE = re.compile(r"[.!?:;\"'”’)\]]$")


def reflow(lines, median):
    """Join lines into paragraphs. A paragraph ends after a line that is
    not full, and at a blank line unless the line before it is full and
    ends mid-sentence (a gap the extractor saw inside a paragraph). A
    bullet or numbered line starts a new paragraph. Returns Markdown
    paragraphs separated by blank lines."""
    paragraphs = []
    current = None
    last = ""

    def flush():
        nonlocal current
        if current:
            paragraphs.append(_markdown_line(current))
        current = None

    for raw in lines:
        line = _WS_RE.sub(" ", raw).strip()
        if not line:
            if not (current and _is_full(last, median)
                    and not _SENTENCE_END_RE.search(last)):
                flush()
            continue
        last = line
        if current is not None and _BULLET_RE.match(line):
            flush()
        current = line if current is None else _join(current, line)
        if not _is_full(line, median):
            flush()
    flush()
    return "\n\n".join(paragraphs)


# --- Chapters from the outline ------------------------------------------

def clean_title(title):
    title = _WS_RE.sub(" ", _CONTROL_RE.sub("", str(title or ""))).strip()
    return title[:MAX_TITLE_CHARS].strip()


def _title_key(text):
    return _TITLE_NORM_RE.sub("", (text or "").lower())


def _outline_entries(level):
    """pypdf's outline is a list of entries, each optionally followed by
    a list of its children. Returns [(entry, children_or_None)]."""
    entries = []
    for item in level or []:
        if isinstance(item, list):
            if entries:
                entries[-1] = (entries[-1][0], item)
        else:
            entries.append((item, None))
    return entries


def outline_chapters(outline, page_of, num_pages):
    """[(page_index, title)] in page order, from the outline's top level.

    When the top level is a single entry with children (a document title
    over its chapters), its children are used instead, as many times as
    that repeats. An outline is usable with at least two entries that
    start on at least two different pages; otherwise returns [].
    page_of(entry) gives the entry's 0-based page index or None.
    """
    entries = _outline_entries(outline)
    while len(entries) == 1 and entries[0][1]:
        entries = _outline_entries(entries[0][1])
    chapters = []
    for entry, _children in entries:
        try:
            title = clean_title(getattr(entry, "title", None))
            page = page_of(entry)
        except Exception:
            continue
        if title and isinstance(page, int) and 0 <= page < num_pages:
            chapters.append((page, title))
    chapters.sort(key=lambda c: c[0])  # stable: same-page order kept
    if len(chapters) < 2 or len({p for p, _ in chapters}) < 2:
        return []
    if len(chapters) > MAX_CHAPTERS:
        return []
    return chapters


def outline_within_limit(reader, limit=None):
    """True when the outline has at most `limit` entries (all levels).
    Walks the outline dictionaries without building pypdf's outline, and
    stops at limit + 1, so a PDF with a huge outline costs little."""
    limit = MAX_OUTLINE_ENTRIES if limit is None else limit
    try:
        outlines = reader.trailer["/Root"].get_object().get("/Outlines")
        outlines = outlines.get_object() if outlines is not None else None
    except Exception:
        return True  # reading the outline will fail and give no chapters
    if not hasattr(outlines, "raw_get"):
        return True
    stack = [outlines.raw_get("/First")] if "/First" in outlines else []
    seen, count = set(), 0
    while stack:
        ref = stack.pop()
        key = (ref.idnum, ref.generation) if hasattr(ref, "idnum") else id(ref)
        if key in seen:
            continue
        seen.add(key)
        count += 1
        if count > limit:
            return False
        try:
            node = ref.get_object()
        except Exception:
            continue
        if hasattr(node, "raw_get"):
            stack.extend(node.raw_get(k) for k in ("/First", "/Next")
                         if k in node)
    return True


# A section label before a heading's title: "1", "3.2", "A.1", "IV.",
# optionally after "Chapter", "Section", "Appendix" or "Part". LaTeX
# bookmarks usually leave the number out ("Introduction" for the page's
# "1 Introduction").
_LABEL = (r"(?:(?i:chapter|section|appendix|part)\s+)?"
          r"(?:\d{1,3}(?:\.\d{1,3})*|[A-Z](?:\.\d{1,3})*|[IVXLC]{1,6})\.?")
_LABEL_PREFIX_RE = re.compile(r"^\s*" + _LABEL + r"\s+(?=\S)")
_LABEL_ONLY_RE = re.compile(r"^\s*" + _LABEL + r"\s*$")


def _line_keys(line):
    """Keys a line can match a title by: the whole line, and the line
    without a leading section label."""
    keys = [_title_key(line)]
    m = _LABEL_PREFIX_RE.match(line)
    if m:
        keys.append(_title_key(line[m.end():]))
    return [k for k in keys if k]


def _find_heading(lines, start, end, key):
    """(index, span) of the line(s) in lines[start:end] that spell the
    chapter title (letters and digits compared, case ignored), or None.
    The line may start with a section label ("1 Introduction" for the
    title "Introduction"), the title may be broken over two lines, and a
    label may stand on the line above it ("Chapter 3", then the title)."""
    if not key:
        return None
    for j in range(start, end):
        keys = _line_keys(lines[j])
        if not keys:
            continue
        if key in keys:
            return j, 1
        if j + 1 < end:
            after = _title_key(lines[j + 1])
            if any(key.startswith(a) and a + after == key for a in keys):
                return j, 2
            if after == key and _LABEL_ONLY_RE.match(lines[j]):
                return j, 2
    return None


def build_markdown(page_texts, chapters):
    """Markdown for the whole document. Returns (markdown, chapter_count).

    With chapters, each one starts on its page at the line that spells
    its title (that line is replaced by a "## title" heading), or at the
    top of the page when no line does. Text before the first chapter is
    its own part, before the first heading. A chapter with no text of
    its own (a part's title page, or two bookmarks at one place) shares
    the next chapter's heading ("Part One — Chapter 1"), so no entry
    holds only a heading.
    """
    pages = strip_page_furniture([clean_page_text(t) for t in page_texts])
    flat, offsets = [], []
    for lines in pages:
        offsets.append(len(flat))
        flat.extend(lines)
    offsets.append(len(flat))
    median = median_line_length(flat)

    cuts = []  # (start, skip, title)
    prev_page, prev_end = None, 0
    for page, title in chapters:
        page_start, page_end = offsets[page], offsets[page + 1]
        search_from = prev_end if prev_page == page else page_start
        found = _find_heading(flat, search_from, page_end, _title_key(title))
        if found:
            start, skip = found
        else:
            start, skip = search_from, 0
        cuts.append((start, skip, title))
        prev_page, prev_end = page, start + skip

    if not cuts:
        return reflow(flat, median), 0
    parts = []
    preamble = reflow(flat[:cuts[0][0]], median)
    if preamble:
        parts.append(preamble)
    pending, count = [], 0
    for k, (start, skip, title) in enumerate(cuts):
        end = cuts[k + 1][0] if k + 1 < len(cuts) else len(flat)
        body = reflow(flat[start + skip:end], median)
        if not body:
            pending.append(title)
            continue
        parts.append(f"## {' — '.join(pending + [title])}\n\n{body}")
        pending, count = [], count + 1
    if pending:
        # Chapters at the very end with no text: their titles close the
        # last part as a line of text.
        parts.append(_markdown_line(" — ".join(pending)))
    return "\n\n".join(parts), count


# --- Metadata --------------------------------------------------------------

def _iso_date(get):
    """A metadata date as a naive-UTC ISO string, or None when missing,
    unreadable, before 1900 or in the future."""
    try:
        dt = get()
    except Exception:
        return None
    if not isinstance(dt, datetime):
        return None
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, ValueError):
        return None
    if dt.year < 1900 or dt > datetime.utcnow() + timedelta(days=1):
        return None
    return dt.isoformat()


def read_metadata(reader):
    try:
        meta = reader.metadata
    except Exception:
        meta = None
    if meta is None:
        return {"title": None, "author": None, "created": None,
                "modified": None}

    def text(get):
        try:
            return clean_title(get()) or None
        except Exception:
            return None

    return {
        "title": text(lambda: meta.title),
        "author": text(lambda: meta.author),
        "created": _iso_date(lambda: meta.creation_date),
        "modified": _iso_date(lambda: meta.modification_date),
    }


# --- Extraction ------------------------------------------------------------

def _open(path):
    """(reader, page count). Raises Fail for a damaged or locked file."""
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, FileNotDecryptedError

    try:
        reader = PdfReader(path, strict=False)
        encrypted = reader.is_encrypted
    except MemoryError:
        raise
    except Exception:
        raise Fail("damaged")
    if encrypted:
        # "Restricted" PDFs (no printing or copying) open with an empty
        # password; anything else needs the user to remove the password.
        try:
            opened = reader.decrypt("")
        except DependencyError:
            raise Fail("encryption_unsupported")
        except MemoryError:
            raise
        except Exception:
            raise Fail("password")
        if not opened:
            raise Fail("password")
    try:
        num_pages = len(reader.pages)
    except FileNotDecryptedError:
        raise Fail("password")
    except MemoryError:
        raise
    except Exception:
        raise Fail("damaged")
    if num_pages == 0:
        raise Fail("damaged")
    return reader, num_pages


def _page_texts(reader, num_pages, max_chars):
    """Each page's extracted text ("" for a page pypdf cannot read)."""
    texts, total, failed = [], 0, 0
    for i in range(num_pages):
        try:
            text = reader.pages[i].extract_text() or ""
        except MemoryError:
            raise
        except Exception:
            text, failed = "", failed + 1
        total += len(text)
        if total > max_chars:
            raise Fail("too_much_text")
        texts.append(text)
    if failed == num_pages:
        raise Fail("damaged")
    return texts


def _outline(reader):
    """The outline, or [] when it is unreadable or has more than
    MAX_OUTLINE_ENTRIES entries."""
    try:
        if not outline_within_limit(reader):
            return []
        return reader.outline
    except MemoryError:
        raise
    except Exception:
        return []


def extract(path, max_pages, max_chars):
    reader, num_pages = _open(path)
    if num_pages > max_pages:
        raise Fail("too_many_pages", pages=num_pages)
    texts = _page_texts(reader, num_pages, max_chars)
    visible = sum(len(_WS_RE.sub("", t)) for t in texts)
    if visible < SCANNED_CHARS_PER_PAGE * num_pages:
        raise Fail("scanned")
    chapters = outline_chapters(
        _outline(reader), reader.get_destination_page_number, num_pages)
    markdown, chapter_count = build_markdown(texts, chapters)
    if not markdown.strip():
        raise Fail("scanned")
    # Headings add text: the result itself must fit the same cap
    if len(markdown) > max_chars:
        raise Fail("too_much_text")
    return {"ok": True, "markdown": markdown, "pages": num_pages,
            "chapters": chapter_count, **read_metadata(reader)}


def set_limits(cpu_seconds, memory_bytes):
    """CPU seconds (SIGXCPU at the soft limit, SIGKILL two seconds later)
    and address space. macOS does not enforce RLIMIT_AS and may refuse
    it; on Linux, where Loore runs, a refusal is an error."""
    import resource

    def lower(kind, value, hard_extra=0):
        _soft, hard = resource.getrlimit(kind)
        new_hard = value + hard_extra
        if hard != resource.RLIM_INFINITY:
            new_hard = min(hard, new_hard)
        resource.setrlimit(kind, (min(value, new_hard), new_hard))

    lower(resource.RLIMIT_CPU, cpu_seconds, hard_extra=2)
    try:
        lower(resource.RLIMIT_AS, memory_bytes)
    except (ValueError, OSError):
        if sys.platform.startswith("linux"):
            raise


def main(argv):
    try:
        path = argv[1]
        cpu_seconds, memory_bytes, max_pages, max_chars = (
            int(a) for a in argv[2:6])
    except (IndexError, ValueError):
        return 2
    import logging
    logging.disable(logging.CRITICAL)  # pypdf warns on stderr; not needed
    try:
        set_limits(cpu_seconds, memory_bytes)
    except Exception:
        result = {"error": "limits"}
    else:
        try:
            result = extract(path, max_pages, max_chars)
        except Fail as e:
            result = {"error": e.code, **e.detail}
        except MemoryError:
            result = {"error": "memory"}
        except RecursionError:
            result = {"error": "damaged"}
        except Exception as e:
            result = {"error": "unreadable", "type": type(e).__name__}
    # UTF-8, not \u escapes: at most 4 bytes a character instead of 6-12
    out = json.dumps(result, ensure_ascii=False).encode("utf-8", "replace")
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
