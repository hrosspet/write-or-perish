"""Uploaded documents saved as References (ExternalItem source
"document") keep their title and author inside the encrypted content,
as a header, and nothing in plaintext columns:

    # Title

    *By Author*

    the document's text ...

Responses to someone who may read the content (the owner, or the viewer
of a quote) get display fields read back from the decrypted text; they
are never written to a column.
"""
import re

from backend.models import SOURCE_DOCUMENT

_TITLE_RE = re.compile(r"# ([^\n]+)(?:\n+|\Z)")
_BYLINE_RE = re.compile(r"\*By ([^\n*]+)\*(?:\n+|\Z)")
_WS_RE = re.compile(r"\s+")


def _one_line(text, limit):
    return _WS_RE.sub(" ", str(text or "")).strip()[:limit].strip()


def compose(title, author, body):
    """The stored text of a document Reference. The byline stays when
    the title is empty."""
    parts = []
    title = _one_line(title, 512)
    author = _one_line(author, 200).replace("*", "")
    if title:
        parts.append(f"# {title}")
    if author:
        parts.append(f"*By {author}*")
    body = (body or "").strip()
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def body_room(title, author, cap):
    """Characters left for the text under the header in `cap`."""
    header = compose(title, author, "")
    return cap - (len(header) + 2 if header else 0)


def split(content):
    """(title, author, body) of a document Reference's text: an optional
    title heading, then an optional byline, then the text."""
    text = content or ""
    title = author = None
    m = _TITLE_RE.match(text)
    if m:
        title = m.group(1).strip() or None
        text = text[m.end():]
    b = _BYLINE_RE.match(text)
    if b:
        author = b.group(1).strip() or None
        text = text[b.end():]
    return title, author, text


def display(item, content):
    """(title, author_handle, body) to show for a reference whose
    decrypted text is `content`: the columns for every other source, the
    header for a document."""
    if item.source == SOURCE_DOCUMENT:
        return split(content)
    return item.title, item.author_handle, content
