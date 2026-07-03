"""Parser for the user-facing changelog file (#207).

The changelog is a markdown file (backend/user_changelog.md) with one
``## `` section per announcement, newest at the top. Each section has a
stable id — an ``<!-- id: ... -->`` comment on the line above the heading,
falling back to the slugified heading title — and a date parsed from the
``## YYYY-MM-DD — Title`` heading. Sections dated before a user's signup
are treated as read (new users get no backlog).

Parsed results are cached on (mtime, size) so the file is only re-read
when it changes.
"""
import os
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

CHANGELOG_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "user_changelog.md")

_ID_COMMENT_RE = re.compile(r"<!--\s*id:\s*([a-zA-Z0-9_-]+)\s*-->")
_HEADING_RE = re.compile(
    r"^##\s+(?:(\d{4}-\d{2}-\d{2})\s*[—–-]\s*)?(.+?)\s*$")

# (mtime, size) -> parsed sections
_cache = {"key": None, "sections": []}


def _slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:128] or "untitled"


def _parse_date(date_str, line):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("user_changelog.md: bad date %r in %r",
                       date_str, line)
        return None


def _scan_sections(text):
    """One pass over the file: skip HTML comments EXCEPT id comments (the
    authoring-notes block at the top must not leak into any body), collect
    heading + body lines per section."""
    sections = []
    current = None
    pending_id = None
    in_comment = False
    for line in text.splitlines():
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        id_match = _ID_COMMENT_RE.match(line.strip())
        if id_match:
            pending_id = id_match.group(1)
            continue
        if line.lstrip().startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            date_str, title = heading.group(1), heading.group(2)
            current = {
                "id": pending_id or _slugify(title),
                "title": title,
                "date": _parse_date(date_str, line),
                "body_lines": [],
            }
            sections.append(current)
            pending_id = None
            continue
        if current is not None:
            current["body_lines"].append(line)
    return sections


def parse_changelog(path=None):
    """Return the changelog as a list of section dicts, file order (newest
    first by convention): {id, title, date (datetime.date|None), body}.

    Missing file -> empty list (the feature simply has nothing to show).
    """
    path = path or CHANGELOG_PATH
    try:
        stat = os.stat(path)
    except OSError:
        return []
    cache_key = (path, stat.st_mtime, stat.st_size)
    if _cache["key"] == cache_key:
        return _cache["sections"]

    with open(path, encoding="utf-8") as f:
        sections = _scan_sections(f.read())

    seen_ids = set()
    result = []
    for s in sections:
        if s["id"] in seen_ids:
            logger.warning(
                "user_changelog.md: duplicate section id %r — skipping "
                "the later section", s["id"])
            continue
        seen_ids.add(s["id"])
        result.append({
            "id": s["id"],
            "title": s["title"],
            "date": s["date"],
            "body": "\n".join(s["body_lines"]).strip(),
        })

    _cache["key"] = cache_key
    _cache["sections"] = result
    return result


def unread_sections_for(user, read_states, path=None):
    """Sections the user should see: not marked 'read', and not dated
    before the user signed up. 'skipped' states stay unread by design.

    read_states: iterable of ChangelogReadState for this user.
    """
    read_ids = {rs.section_id for rs in read_states if rs.status == "read"}
    signup_date = user.created_at.date() if user.created_at else None
    unread = []
    for section in parse_changelog(path):
        if section["id"] in read_ids:
            continue
        if (signup_date and section["date"]
                and section["date"] < signup_date):
            continue
        unread.append(section)
    return unread
