"""
Quote resolution utilities for {quote:ID} placeholders.

This module provides functionality to detect and resolve inline node quotes,
similar to how {user_profile} and {user_export} placeholders work.

Supports two modes:
1. Direct quote resolution (for conversation context) - resolves quotes recursively
   up to a configurable depth with cycle detection.
2. Export quote resolution (for {user_export}) - uses an efficient abstract
   representation to resolve quotes within token-limited exports, ensuring
   quoted content is available even when the export is truncated.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Tuple, List, Dict, Optional, Set
from celery.utils.log import get_task_logger

from backend.utils.timefmt import iso_utc, local_stamp

logger = get_task_logger(__name__)

# Pattern to match {quote:123} where 123 is a node ID
QUOTE_PLACEHOLDER_PATTERN = r'\{quote:(\d+)\}'

# Pattern to match {quote_ext:123} where 123 is an ExternalItem ID —
# a saved reference (tweet/bookmark) quoted in a response. Resolved
# verbatim from the DB like node quotes (never LLM-retyped), owner-only.
EXT_QUOTE_PLACEHOLDER_PATTERN = r'\{quote_ext:(\d+)\}'

# Default max depth for recursive quote resolution in direct conversation
DEFAULT_MAX_DEPTH = 3

# Preamble body for a pinned profile/todo/artifact version whose ai_usage
# keeps it from the model (#340). The thread keeps its "(ref #N)" line, so
# the preamble says why the content is missing instead of dropping the ref.
AI_BLOCKED_ARTIFACT_TEXT = "[AI usage not permitted by author]"


def ai_blocked_artifact(row) -> bool:
    """True when an artifact row (profile, todo, UserArtifact) must not
    reach a model: its ai_usage is outside AI_ALLOWED. Rows without an
    ai_usage column (UserPrompt) are never blocked."""
    from backend.utils.privacy import AI_ALLOWED
    if not hasattr(row, "ai_usage"):
        return False
    return row.ai_usage not in AI_ALLOWED


def find_quote_ids(content: str) -> List[int]:
    """
    Find all node IDs referenced in {quote:ID} placeholders.

    Args:
        content: Text that may contain {quote:ID} placeholders

    Returns:
        List of node IDs found in the content
    """
    if not content:
        return []
    matches = re.findall(QUOTE_PLACEHOLDER_PATTERN, content)
    return [int(node_id) for node_id in matches]


def find_ext_quote_ids(content: str) -> List[int]:
    """Find all ExternalItem IDs referenced in {quote_ext:ID} placeholders."""
    if not content:
        return []
    matches = re.findall(EXT_QUOTE_PLACEHOLDER_PATTERN, content)
    return [int(item_id) for item_id in matches]


def get_ext_quote_data(item_ids: List[int], user_id: int) -> Dict[int, Optional[dict]]:
    """Fetch external-item data for the given IDs, owner-only.

    External items belong to the user who imported them; unlike nodes
    there is no cross-user sharing, so access == ownership.
    """
    from backend.models import ExternalItem
    from backend.utils import document_reference
    from backend.utils.timefmt import iso_utc

    result = {}
    for item_id in item_ids:
        item = ExternalItem.query.get(item_id)
        if item is None or item.user_id != user_id:
            result[item_id] = None
            continue
        # An uploaded document's title and author live in its encrypted
        # text (utils/document_reference); the card shows them as fields.
        title, author, content = document_reference.display(
            item, item.get_content() or "")
        result[item_id] = {
            "id": item.id,
            "content": content,
            "source": item.source,
            "author_handle": author,
            # The tweet card's byline (#435): display name, then handle.
            "author_name": item.author_name,
            "title": title,
            "url": item.url,
            "posted_at": iso_utc(item.posted_at) if item.posted_at else None,
            # The reference's owner and their own read mark: the quote
            # bubble offers "Mark as read" only to the owner (the read
            # endpoint is owner-only), so a viewer of a public node just
            # sees the quote.
            "user_id": item.user_id,
            "read_at": iso_utc(item.read_at) if item.read_at else None,
            "feedback": item.feedback,
        }
    return result


def quote_stamp(iso: Optional[str], tz_name: Optional[str] = None) -> Optional[str]:
    """Render an ``iso_utc`` string as ``YYYY-MM-DD HH:MM TZ`` in ``tz_name``
    (UTC when unset) — the same rendering thread messages carry, minus the
    brackets, so a quoted node's date reads like its neighbours' (#130).
    Returns ``None`` when there is no timestamp to render."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return local_stamp(dt, tz_name).strip("[]")


def resolve_ext_quotes(content: str, user_id: int,
                       for_llm: bool = False) -> Tuple[str, List[int]]:
    """Replace {quote_ext:ID} placeholders with the saved reference's
    content, verbatim from the DB. No recursion — external items don't
    nest. Returns (resolved_content, list_of_resolved_item_ids).

    A read reply's picks, re-sent to the model on later turns of its
    thread, resolve through ca_feed.render_read_reply_for_model instead:
    by author and text, with no id and no marks."""
    if not content:
        return content, []
    item_ids = find_ext_quote_ids(content)
    if not item_ids:
        return content, []

    data = get_ext_quote_data(item_ids, user_id)
    resolved_ids = []

    def replace(match):
        item_id = int(match.group(1))
        d = data.get(item_id)
        if d is None:
            if for_llm:
                return f"[Quoted reference #{item_id}: inaccessible]"
            return f"[Quoted reference inaccessible: item {item_id}]"
        resolved_ids.append(item_id)
        # Tweets have @handles; a web clip's or an uploaded document's
        # author is a byline or the site's hostname, which an @ would only
        # misrepresent.
        from backend.models import BYLINE_SOURCES
        author = d["author_handle"] or "unknown"
        if d["source"] not in BYLINE_SOURCES:
            author = f"@{author}"
        title = d.get("title")
        if for_llm:
            title_attr = f' title="{title}"' if title else ""
            posted_attr = (f' posted_at="{d["posted_at"][:10]}"'
                           if d.get("posted_at") else "")
            # The user's own marks on this pick, so a thread that
            # continues after the quote knows whether they read it and
            # how they rated it (the same two facts search previews
            # carry). Absent attributes mean unread / unrated.
            read_attr = (f' read="{d["read_at"][:10]}"'
                         if d.get("read_at") else "")
            verdict_attr = (f' verdict="{d["feedback"]}"'
                            if d.get("feedback") else "")
            return (
                f'<quoted_reference id="{item_id}" source="{d["source"]}" '
                f'author="{author}"{title_attr}{posted_attr}'
                f'{read_attr}{verdict_attr}>\n'
                f'{d["content"]}\n</quoted_reference>')
        posted = f' ({d["posted_at"][:10]})' if d.get("posted_at") else ""
        label = f'"{title}" from {author}' if title else f"from {author}"
        # The same two marks in the human rendering (exports): they are
        # the user's own record of the pick.
        mark_text = ""
        if d.get("read_at"):
            mark_text += f' · read {d["read_at"][:10]}'
        if d.get("feedback"):
            mark_text += f' · rated a {d["feedback"]} quote'
        return (f'\n--- Saved reference {label}{posted}{mark_text} ---\n'
                f'{d["content"]}\n--- End reference ---\n')

    resolved = re.sub(EXT_QUOTE_PLACEHOLDER_PATTERN, replace, content)
    return resolved, resolved_ids


def has_ext_quotes(content: str) -> bool:
    """Check if content contains any {quote_ext:ID} placeholders."""
    if not content:
        return False
    return bool(re.search(EXT_QUOTE_PLACEHOLDER_PATTERN, content))


# A marker in text that someone other than the reader wrote, held so a
# later pass over the reader's whole text (the saved-reference pass, or
# the prompt's quote passes over an archive filled into it) cannot
# resolve it with the reader's access. release_quotes puts it back as
# written once those passes are done.
_HELD_EXT_QUOTE = "{quote_ext\ue000:"
_HELD_QUOTE = "{quote\ue000:"


def hold_ext_quotes(text: str) -> str:
    """*text* with every {quote_ext:ID} held (see _HELD_EXT_QUOTE)."""
    return text.replace("{quote_ext:", _HELD_EXT_QUOTE) if text else text


def hold_quotes(text: str) -> str:
    """*text* with every {quote:ID} and {quote_ext:ID} held."""
    if not text:
        return text
    return hold_ext_quotes(text).replace("{quote:", _HELD_QUOTE)


def release_quotes(text: str) -> str:
    """*text* with every held marker back as written."""
    if not text:
        return text
    return text.replace(_HELD_EXT_QUOTE, "{quote_ext:").replace(
        _HELD_QUOTE, "{quote:")


# How the recent raw window of a call with tools shows a quoted saved
# reference (reference_title_lines): one line naming it, and with
# REFERENCE_LINE_NAMED also the name read_full opens it by, for a reply
# that has read_full.
REFERENCE_LINE = "line"
REFERENCE_LINE_NAMED = "named line"


class ReferenceLines:
    """One rendering of the recent raw window with reference lines
    (the export's *reference_titles*): whether the lines carry the name
    read_full opens a reference by (*named*), and the ids of the
    references named that way (*ids*), filled in as the window renders.
    With the "External references" toggle off, read_full opens only
    these by name."""

    def __init__(self, named: bool):
        self.named = named
        self.ids: Set[int] = set()


# A saved reference named in the recent raw window is opened by this
# name with the read_full tool (reference_title_lines).
_REFERENCE_NAME_RE = re.compile(r"reference\s*#?\s*(\d+)", re.IGNORECASE)

# Where a reference came from, as its card's first line says it
# (frontend utils/references.js, sourceLine); web clips name their site.
_REFERENCE_SOURCE_TEXT = {
    "community_archive": "tweet from the Community Archive",
    "read_pick": "tweet from the Community Archive",
    "twitter_bookmark": "tweet from the user's Twitter bookmarks",
    "twitter_like": "tweet from the user's Twitter likes",
    "document": "document the user uploaded",
}


def reference_name(item_id: int) -> str:
    """The name read_full opens the saved reference *item_id* by."""
    return f"reference {item_id}"


def reference_id_from_name(ref: str) -> Optional[int]:
    """The id in a read_full ref of the form reference_name gives
    ("reference 12", any case, quotes around it allowed), or None."""
    match = _REFERENCE_NAME_RE.fullmatch((ref or "").strip().strip("\"'"))
    return int(match.group(1)) if match else None


def _reference_source_text(item) -> str:
    if item.source == "web_clip":
        from urllib.parse import urlparse
        try:
            host = urlparse(item.url).hostname if item.url else None
        except ValueError:
            host = None
        if host and host.startswith("www."):
            host = host[4:]
        return f"page from {host}" if host else "saved page"
    return _REFERENCE_SOURCE_TEXT.get(item.source, item.source)


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def reference_title_lines(content: str, user_id: int, named: bool = True
                          ) -> Tuple[str, List[int], List[int]]:
    """Replace each {quote_ext:ID} with one line that names the saved
    reference instead of giving its text: its title (with no title, its
    author), where it came from, and with *named* the name read_full
    opens it by. For the recent raw window of a call with tools (Peter,
    voice review 2026-11-12: recent entries show a saved reference by
    title); the model opens it with read_full when it needs the text.
    A reference the user cannot read is the export's inaccessible line
    (resolve_ext_quotes).

    Only an uploaded document is decrypted, as its title and author are
    inside its text (utils/document_reference); every other source has
    them in columns. A marker inside a title or author comes back held
    (hold_quotes), so no later quote pass resolves it.

    Returns (text, the ids named, the ids whose line carries a title: the
    reference's own words, where a line without one carries only its
    author and source)."""
    if not content:
        return content, [], []
    item_ids = find_ext_quote_ids(content)
    if not item_ids:
        return content, [], []

    from backend.extensions import db
    from backend.models import BYLINE_SOURCES, SOURCE_DOCUMENT, ExternalItem
    from backend.utils import document_reference

    items = {}
    for item_id in set(item_ids):
        item = db.session.get(ExternalItem, item_id)
        if item is not None and item.user_id == user_id:
            items[item_id] = item
    named_ids, titled = [], []

    def replace(match):
        item_id = int(match.group(1))
        item = items.get(item_id)
        if item is None:
            return f"[Quoted reference inaccessible: item {item_id}]"
        named_ids.append(item_id)
        if item.source == SOURCE_DOCUMENT:
            title, author, _body = document_reference.split(
                item.get_content() or "")
        else:
            title, author = item.title, item.author_handle
        title, author = _one_line(title), _one_line(author)
        if title:
            titled.append(item_id)
            what = f' "{title}"'
        elif author:
            if item.source not in BYLINE_SOURCES:
                author = f"@{author}"
            what = f" by {author}"
        else:
            what = ""
        opener = (f'; read_full "{reference_name(item_id)}" opens it'
                  if named else "")
        return hold_quotes(
            f"[Saved reference{what} ({_reference_source_text(item)}), "
            f"text not included{opener}]")

    text = re.sub(EXT_QUOTE_PLACEHOLDER_PATTERN, replace, content)
    return text, named_ids, titled


def written_by(node, user_id) -> bool:
    """True when *user_id* wrote *node*: their own entry, or an AI reply
    they asked for. An AI reply from before human_owner_id was stored
    belongs to the person above it."""
    owner = node.human_owner_id
    if owner is None and node.node_type == "llm":
        from backend.utils.privacy import find_human_owner
        owner = find_human_owner(node)
    return (owner or node.user_id) == user_id


def readable_by_everyone(node_id) -> bool:
    """True when the node *node_id* is public and not deleted: every
    reader of any node can read it."""
    from backend.extensions import db
    from backend.models import Node
    node = db.session.get(Node, node_id)
    return (node is not None and node.deleted_at is None
            and node.privacy_level == "public")


def quotes_inside(user_id, quotable_for=None):
    """The *inside* test of resolve_quotes and the export: for a quoted
    node's id, None when *user_id* wrote that node (its quotes resolve
    with their access, as before), else the test each {quote:ID} in its
    text must pass: quotable_for(node), by default "the quoted node is
    public" (readable_by_everyone)."""
    from backend.extensions import db
    from backend.models import Node

    def inside(node_id):
        node = db.session.get(Node, node_id)
        if node is not None and written_by(node, user_id):
            return None
        if node is None or quotable_for is None:
            return readable_by_everyone
        return quotable_for(node)
    return inside


def get_quote_data(node_ids: List[int], user_id: int) -> Dict[int, Optional[dict]]:
    """
    Fetch node data for the given IDs, checking access permissions.

    Args:
        node_ids: List of node IDs to fetch
        user_id: ID of the user requesting access (for permission checks)

    Returns:
        Dict mapping node_id to node data dict (or None if not accessible).
        Soft-deleted targets the viewer had pre-deletion access to are
        returned with `"deleted": True` and no content; the resolver
        renders these as `[Quoted node deleted]` rather than the privacy
        "not accessible" string.
    """
    from backend.models import Node
    from backend.utils.privacy import (
        author_gone, can_user_access_node, can_user_view_tombstone,
        owner_hidden,
    )

    result = {}

    for node_id in node_ids:
        node = Node.query.get(node_id)
        if node is None:
            result[node_id] = None
            continue
        # Soft-deleted: emit a tombstone payload iff viewer had pre-deletion
        # access; otherwise None (renders as "not accessible"). A node whose
        # author deleted the account (#269) quotes as deleted, without a name.
        if node.deleted_at is not None or owner_hidden(node):
            if can_user_view_tombstone(node, user_id):
                gone = author_gone(node)
                result[node_id] = {
                    "id": node.id,
                    "deleted": True,
                    "content": None,
                    "username": (None if gone else node.user.username
                                 if node.user else "Unknown"),
                    "user_id": None if gone else node.user_id,
                    "created_at": iso_utc(node.created_at),
                    "node_type": node.node_type,
                    "ai_usage": node.ai_usage,
                }
            else:
                result[node_id] = None
            continue
        # Alive: standard access check.
        if can_user_access_node(node, user_id):
            result[node_id] = {
                "id": node.id,
                "content": node.get_content(),
                "username": node.user.username if node.user else "Unknown",
                "user_id": node.user_id,
                "created_at": iso_utc(node.created_at),
                "node_type": node.node_type,
                "ai_usage": node.ai_usage,
            }
        else:
            result[node_id] = None

    return result


def resolve_quotes(
    content: str,
    user_id: int,
    for_llm: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
    _seen_ids: Optional[Set[int]] = None,
    tz_name: Optional[str] = None,
    block_ai_none: Optional[bool] = None,
    only=None,
    inside=None,
) -> Tuple[str, List[int]]:
    """
    Replace {quote:ID} placeholders with quoted node content, recursively.

    *only* (a callable taking a node id): a quote whose id it refuses is
    left as written, at every depth. Used for text another person wrote
    into a shared thread, where a quote resolves only when every reader
    of the thread can read the quoted node (llm_completion).

    *inside* (quotes_inside): for each quoted node, None when the user
    wrote it, else the test the quotes in its text must pass. Text the
    user did not write, and everything quoted below it, then resolves
    its quotes only through that test (or *only*). A quote it refuses,
    and every marker left in such text, comes back held (hold_quotes):
    no later pass over the caller's text resolves it, and
    release_quotes puts it back as written. A caller passing *only* or
    *inside* releases.

    Args:
        content: Text containing {quote:ID} placeholders
        user_id: ID of requesting user (for access checks)
        for_llm: If True, wrap content in XML tags for LLM context;
                 if False, use human-readable format
        max_depth: Maximum recursion depth (default 3). Set to 1 for no recursion.
        _seen_ids: Internal - tracks visited node IDs to prevent cycles
        tz_name: IANA timezone the quoted node's creation time is rendered
                 in (UTC when unset) — pass the conversation owner's so it
                 matches the thread's message stamps
        block_ai_none: Replace quotes of nodes marked ai_usage='none' with a
                 placeholder. Defaults to *for_llm*; the AI-filtered export
                 sets it with the human-readable format (#340 review).

    Returns:
        Tuple of (resolved_content, list_of_quoted_node_ids)
    """
    if not content:
        return content, []

    if max_depth <= 0:
        return content, []

    if _seen_ids is None:
        _seen_ids = set()
    if block_ai_none is None:
        block_ai_none = for_llm

    quote_ids = find_quote_ids(content)
    if not quote_ids:
        return content, []

    # Fetch all quoted nodes
    quote_data = get_quote_data(quote_ids, user_id)
    resolved_ids = []

    def replace_quote(match):
        node_id = int(match.group(1))
        if only is not None and not only(node_id):
            return hold_quotes(match.group(0))
        data = quote_data.get(node_id)

        if data is None:
            # Node not found, privacy-blocked, or soft-deleted with no
            # pre-deletion access — render as inaccessible (vocabulary
            # aligned with the rest of the deletion plan).
            if for_llm:
                return f"[Quote #{node_id}: inaccessible]"
            else:
                return f"[Quoted node inaccessible: node {node_id}]"

        # Soft-deleted target the viewer had pre-deletion access to:
        # render a deletion-specific placeholder, distinct from
        # privacy-blocked. Don't ingest deleted content into the LLM.
        if data.get("deleted"):
            if for_llm:
                return f"[Quote #{node_id}: deleted by author]"
            else:
                return f"[Quoted node deleted: node {node_id}]"

        # AI usage check: block content from nodes that don't permit AI usage
        if block_ai_none and data.get("ai_usage") == "none":
            return f"[Quote #{node_id}: AI usage not permitted by author]"

        # Cycle detection
        if node_id in _seen_ids:
            if for_llm:
                return f"[Quote #{node_id}: circular reference]"
            else:
                return f"[Circular quote reference: node {node_id}]"

        resolved_ids.append(node_id)
        node_content = data["content"] or ""
        username = data["username"]

        # The quoted text's own quotes: under the test of the text it
        # sits in (*only*), or, quoted from text the user wrote, under
        # the test for the quoted node when someone else wrote it.
        inner_only = only
        if inner_only is None and inside is not None:
            inner_only = inside(node_id)
        if inner_only is not None:
            node_content = hold_ext_quotes(node_content)

        # Recursively resolve quotes in the quoted content
        if max_depth > 1 and has_quotes(node_content):
            new_seen = _seen_ids | {node_id}
            node_content, nested_ids = resolve_quotes(
                node_content,
                user_id,
                for_llm=for_llm,
                max_depth=max_depth - 1,
                _seen_ids=new_seen,
                tz_name=tz_name,
                block_ai_none=block_ai_none,
                only=inner_only,
                inside=inside,
            )
            resolved_ids.extend(nested_ids)
        if inner_only is not None:
            # Whatever the depth left unresolved stays as written.
            node_content = hold_quotes(node_content)

        stamp = quote_stamp(data.get("created_at"), tz_name)
        if for_llm:
            # XML format for clear LLM context. The creation stamp is the
            # quote's only temporal anchor — without it the model can't
            # place the quoted entry in time relative to the thread.
            stamp_attr = f' created_at="{stamp}"' if stamp else ""
            return (f'<quoted_node id="{node_id}" author="{username}"{stamp_attr}>\n'
                    f'{node_content}\n</quoted_node>')
        else:
            # Human-readable format for exports
            when = f", {stamp}" if stamp else ""
            return (f'\n--- Quoted from @{username} (node #{node_id}{when}) ---\n'
                    f'{node_content}\n--- End quote ---\n')

    resolved_content = re.sub(QUOTE_PLACEHOLDER_PATTERN, replace_quote, content)
    return resolved_content, resolved_ids


def has_quotes(content: str) -> bool:
    """
    Check if content contains any {quote:ID} placeholders.

    Args:
        content: Text to check

    Returns:
        True if content contains quote placeholders
    """
    if not content:
        return False
    return bool(re.search(QUOTE_PLACEHOLDER_PATTERN, content))


# =============================================================================
# Export Quote Resolution - Abstract Representation Approach
# =============================================================================
#
# For large exports that may be truncated to fit context windows, we use an
# efficient algorithm that:
# 1. Builds an abstract representation of the export (node IDs + token counts)
# 2. Resolves quotes by embedding content only when the quoted node is NOT
#    already present in the truncated export
# 3. Processes quotes newest-to-oldest to prioritize recent content
# 4. Re-truncates after each embedding, repeating until all quotes are resolved
#
# This ensures quoted content is always available to the LLM, even when the
# full export doesn't fit in the context window.
# =============================================================================


@dataclass
class NodeEntry:
    """
    Abstract representation of a node in the export.

    Tracks token counts and unresolved quotes without storing full content,
    enabling efficient truncation calculations.
    """
    node_id: int
    created_at: datetime
    base_tokens: int  # Tokens for this node's own content
    embedded_tokens: int = 0  # Additional tokens from embedded quote content
    quote_ids: List[int] = field(default_factory=list)  # Unresolved quote targets
    is_top_level: bool = True  # Whether this is a top-level entry vs embedded
    user_prompt_id: Optional[int] = None  # System prompt reference (legacy)
    artifacts: List[Tuple[str, int]] = field(default_factory=list)  # (type, id)
    # quote id -> the test of the text it was found in, for a quote found
    # only in text the export's user did not write (quotes_inside); a
    # quote found in their own text is not listed.
    found_under: Dict[int, Callable[[int], bool]] = field(
        default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.base_tokens + self.embedded_tokens


class ExportQuoteResolver:
    """
    Resolves quotes in user exports using an abstract representation.

    This class implements an efficient algorithm for resolving {quote:ID}
    placeholders in large exports that may be truncated to fit context windows.

    The algorithm ensures that:
    - Quoted content is always present (either as a separate entry or embedded)
    - Newest content is prioritized when truncating
    - No duplicate content (if quoted node is in export, just reference it)
    - Efficient O(1) lookups for "is node in export?" checks

    Usage:
        resolver = ExportQuoteResolver(user_id, max_tokens)
        resolver.add_node(node_id, created_at, content, quote_ids)
        ...
        resolver.resolve()  # Runs the resolution algorithm
        included_ids, embedded_quotes, ai_blocked_ids = resolver.get_resolution_result()
    """

    def __init__(self, user_id: int, max_tokens: Optional[int],
                 filter_ai_usage: bool = False,
                 chronological: bool = False,
                 with_preamble: bool = True,
                 inside=None):
        """
        Initialize the resolver.

        Args:
            user_id: User ID for access checks when fetching quoted content
            max_tokens: Maximum tokens allowed in the export. None: no
                       budget, every entry stays and only the quotes are
                       resolved (the caller's selection already sized the
                       window and must see all of it rendered, #552).
            filter_ai_usage: If True, block nodes with ai_usage='none' from
                            being resolved (their content won't be embedded)
            chronological: If True, sort oldest-first (for iterative profile
                          building). Default is newest-first.
            with_preamble: Whether the export emits the artifacts preamble
                          (the full text of each referenced system prompt,
                          profile and todo list). False when the caller
                          collapses artifacts to reference lines or omits
                          them (#550): then the
                          budget counts only the entries, and the profile
                          and todo texts are not loaded (#518).
            inside: quotes_inside(user_id), or None. In an entry the
                   user did not write, and in quoted text they did not
                   write, a quote is embedded only when it passes the
                   test of the text it is in (resolve_quotes_for_export
                   renders by the same rule).
        """
        self.user_id = user_id
        self.inside = inside
        self.max_tokens = max_tokens
        self.filter_ai_usage = filter_ai_usage
        self.chronological = chronological
        self.with_preamble = with_preamble
        self.entries: List[NodeEntry] = []
        self.included_count: int = 0
        self.included_ids: Set[int] = set()
        # Tracks node IDs whose content was embedded (separate from truncation)
        self._embedded_ids: Set[int] = set()
        # Tracks node IDs blocked due to ai_usage='none'
        self._ai_blocked_ids: Set[int] = set()
        # Maps node_id -> {quoted_node_id -> embedded_content} for final rendering
        self.embedded_quotes: Dict[int, Dict[int, str]] = {}
        # Cache of node metadata (tokens, quote_ids) to avoid repeated DB queries
        self._node_cache: Dict[int, dict] = {}
        # System prompt tracking
        self._prompt_tokens: Dict[int, int] = {}  # user_prompt_id → token cost
        self._prompt_contents: Dict[int, str] = {}  # user_prompt_id → content
        # user_prompt_id → the node whose content the preamble shows, so
        # an export going to a model can report it (#326).
        self.prompt_node_ids: Dict[int, int] = {}
        self.referenced_prompt_ids: Set[int] = set()  # final result
        # Generic artifact tracking: (artifact_type, artifact_id) → content
        self._artifact_contents: Dict[Tuple[str, int], str] = {}
        self._artifact_tokens: Dict[Tuple[str, int], int] = {}
        # (artifact_type, artifact_id) → the user the version belongs to
        self._artifact_owner: Dict[Tuple[str, int], Optional[int]] = {}
        self.referenced_artifacts: Dict[str, Set[int]] = {
            "prompt": set(), "profile": set(), "todo": set(),
        }

    def add_node(
        self,
        node_id: int,
        created_at: 'datetime',
        content: str,
        token_count: Optional[int] = None,
        user_prompt_id: Optional[int] = None,
        prompt_content: Optional[str] = None,
        prompt_label: Optional[str] = None,
        artifacts: Optional[List[Tuple[str, int]]] = None,
    ):
        """
        Add a node to the export.

        Args:
            node_id: The node's ID
            created_at: When the node was created (for sorting)
            content: The node's content (used to extract quote IDs and estimate tokens)
            token_count: Optional pre-computed token count
            user_prompt_id: Optional system prompt FK (legacy)
            prompt_content: Full prompt content (for preamble)
            prompt_label: Label like "Reflect v3" (for reference text)
            artifacts: List of (artifact_type, artifact_id) tuples
        """
        from backend.utils.tokens import approximate_token_count

        if user_prompt_id is not None:
            # System prompt node: base_tokens is just the reference line
            ref_text = f"[System Prompt: {prompt_label or 'Prompt'}]"
            base_tokens = approximate_token_count(ref_text)
            # Cache prompt content/tokens if not already cached
            if user_prompt_id not in self._prompt_tokens and prompt_content:
                self._prompt_tokens[user_prompt_id] = approximate_token_count(
                    prompt_content
                )
                self._prompt_contents[user_prompt_id] = prompt_content
                self.prompt_node_ids[user_prompt_id] = node_id
        else:
            if token_count is None:
                token_count = approximate_token_count(content)
            base_tokens = token_count

        all_quote_ids = find_quote_ids(content)
        quote_ids = all_quote_ids
        # An entry someone else wrote: only the quotes its test passes.
        entry_only = (self.inside(node_id)
                      if self.inside is not None and all_quote_ids else None)
        found_under = {}
        if entry_only is not None:
            quote_ids = [q for q in all_quote_ids if entry_only(q)]
            found_under = {q: entry_only for q in quote_ids}

        # Only the export user's own versions: a node of someone else's
        # in the user's threads (their system prompt above the user's
        # reply, their AI reply that read their todo) pins theirs.
        artifacts = [
            (atype, aid) for atype, aid in (artifacts or [])
            if atype == "prompt" or self._artifact_is_own(atype, aid)
        ]

        entry = NodeEntry(
            node_id=node_id,
            created_at=created_at,
            base_tokens=base_tokens,
            quote_ids=quote_ids,
            user_prompt_id=user_prompt_id,
            artifacts=artifacts or [],
            found_under=found_under,
        )
        self.entries.append(entry)

        # Cache artifact content for preamble building
        if artifacts and self.with_preamble:
            for atype, aid in artifacts:
                key = (atype, aid)
                if key not in self._artifact_contents:
                    acontent = self._load_artifact_content(
                        atype, aid, filter_ai_usage=self.filter_ai_usage)
                    if acontent:
                        self._artifact_contents[key] = acontent
                        self._artifact_tokens[key] = (
                            approximate_token_count(acontent)
                        )

        # Cache for later use
        self._node_cache[node_id] = {
            'tokens': base_tokens,
            'quote_ids': all_quote_ids,
            'content': content
        }

    def add_tombstone(self, node_id: int, created_at: 'datetime',
                      token_count: int):
        """Add a deleted node, which the export shows as a placeholder
        line (#525). It counts *token_count* (the placeholder's size) and
        has no quotes to resolve. Its text is never read, and it is not
        cached: a quote of it in another entry is looked up like a quote
        of any deleted node, which is never embedded."""
        self.entries.append(NodeEntry(
            node_id=node_id,
            created_at=created_at,
            base_tokens=token_count,
        ))

    def _artifact_is_own(self, artifact_type, artifact_id):
        """Whether the pinned version belongs to the export's user."""
        from backend.utils.context_artifacts import pinned_row
        key = (artifact_type, artifact_id)
        if key not in self._artifact_owner:
            row = pinned_row(artifact_type, artifact_id)
            self._artifact_owner[key] = row.user_id if row else None
        return self._artifact_owner[key] == self.user_id

    @staticmethod
    def _load_artifact_content(artifact_type, artifact_id,
                               filter_ai_usage=False):
        """Load content for an artifact by type and id.

        With *filter_ai_usage*, a profile or todo version marked outside
        AI_ALLOWED yields AI_BLOCKED_ARTIFACT_TEXT instead of its content
        (#340), the same rule the resolver applies to nodes. Prompts carry
        no ai_usage.
        """
        if artifact_type == "prompt":
            from backend.models import UserPrompt
            obj = UserPrompt.query.get(artifact_id)
            return obj.get_content() if obj else None
        if artifact_type == "profile":
            from backend.models import UserProfile
            obj = UserProfile.query.get(artifact_id)
        elif artifact_type == "todo":
            from backend.models import UserTodo
            obj = UserTodo.query.get(artifact_id)
        else:
            return None
        if obj is None:
            return None
        if filter_ai_usage and ai_blocked_artifact(obj):
            return AI_BLOCKED_ARTIFACT_TEXT
        return obj.get_content()

    def _truncate(self):
        """
        Recalculate which entries fit within max_tokens.

        Entries are kept in order (sorted by the configured sort order:
        newest-first by default, or oldest-first when chronological=True),
        and we include as many as fit within the budget.

        Note: Preserves _embedded_ids - nodes whose content was embedded
        are always considered "included" even if they're not in the
        truncated entry list.

        Also accounts for prompt preamble overhead: each unique
        user_prompt_id referenced by included entries adds its full
        content token cost once. Only when the preamble is emitted
        (with_preamble): a collapsed export shows a prompt as its
        reference line, which the entry's base_tokens already count.

        Without a budget (max_tokens None) every entry is included.
        """
        total = 0
        self.included_count = 0
        # Start with embedded IDs - these are always "included"
        self.included_ids = set(self._embedded_ids)
        budgeted = self.max_tokens is not None

        for i, entry in enumerate(self.entries):
            entry_tokens = entry.total_tokens
            if budgeted and total + entry_tokens > self.max_tokens:
                self.included_count = i
                break
            total += entry_tokens
            self.included_ids.add(entry.node_id)
        else:
            self.included_count = len(self.entries)

        # Account for prompt preamble overhead
        if self._prompt_tokens and self.with_preamble and budgeted:
            while self.included_count > 0:
                entry_total = sum(
                    e.total_tokens
                    for e in self.entries[:self.included_count]
                )
                referenced = {
                    e.user_prompt_id
                    for e in self.entries[:self.included_count]
                    if e.user_prompt_id is not None
                }
                prompt_overhead = sum(
                    self._prompt_tokens.get(pid, 0) for pid in referenced
                )
                if entry_total + prompt_overhead <= self.max_tokens:
                    break
                self.included_count -= 1

            # Rebuild included_ids after prompt-aware truncation
            self.included_ids = set(self._embedded_ids)
            for entry in self.entries[:self.included_count]:
                self.included_ids.add(entry.node_id)

        # Compute final referenced prompt IDs
        self.referenced_prompt_ids = {
            e.user_prompt_id
            for e in self.entries[:self.included_count]
            if e.user_prompt_id is not None
        }

        # Compute final referenced artifacts (all types)
        self.referenced_artifacts = {
            "prompt": set(), "profile": set(), "todo": set(),
        }
        for e in self.entries[:self.included_count]:
            for atype, aid in e.artifacts:
                if atype in self.referenced_artifacts:
                    self.referenced_artifacts[atype].add(aid)

    def _get_node_metadata(self, node_id: int) -> Optional[dict]:
        """
        Get metadata for a node (tokens, quote_ids, content), or None when
        its text must not be embedded: the node is gone, the user cannot
        access it, or (with filter_ai_usage) it is marked 'none'.

        The checks apply to every node, also one whose content is already
        cached (an entry added with add_node); the cache only saves
        decrypting it again.
        """
        from backend.models import Node
        from backend.utils.privacy import can_user_access_node
        from backend.utils.tokens import approximate_token_count

        node = Node.query.get(node_id)
        if not node or not can_user_access_node(node, self.user_id):
            return None

        # Block nodes with ai_usage='none' when filtering is enabled
        if self.filter_ai_usage and node.ai_usage == 'none':
            self._ai_blocked_ids.add(node_id)
            return None

        if node_id in self._node_cache:
            return self._node_cache[node_id]

        content = node.get_content()
        metadata = {
            'tokens': approximate_token_count(content),
            'quote_ids': find_quote_ids(content),
            'content': content,
            'username': node.user.username if node.user else "Unknown"
        }
        self._node_cache[node_id] = metadata
        return metadata

    def resolve(self):
        """
        Run the quote resolution algorithm.

        This implements the iterative resolution process:
        1. Sort entries newest-first
        2. Truncate to fit max_tokens
        3. For each quote in included entries (newest-first):
           - If quoted node is in included_ids: resolved by reference
           - Otherwise: embed the quoted content and re-truncate
        4. Repeat until a full pass with no embeddings
        """
        # Sort entries by created_at (newest-first by default,
        # oldest-first when chronological=True)
        self.entries.sort(
            key=lambda e: e.created_at,
            reverse=not self.chronological
        )

        # Initial truncation
        self._truncate()

        # Iterative resolution
        max_iterations = 1000  # Safety limit
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            changed = False

            # Collect all unresolved quotes from included entries
            # Process by quoting node's timestamp (newest first)
            for entry in self.entries[:self.included_count]:
                for quoted_id in list(entry.quote_ids):  # Copy to allow modification
                    if quoted_id in self.included_ids:
                        # Resolved by reference - quoted node is in the export
                        continue

                    # Need to embed this quote
                    metadata = self._get_node_metadata(quoted_id)
                    if metadata is None:
                        # Node not accessible - remove from quote_ids
                        entry.quote_ids.remove(quoted_id)
                        continue

                    # Embed the quoted content
                    entry.embedded_tokens += metadata['tokens']
                    entry.quote_ids.remove(quoted_id)

                    # Inherit quotes from the embedded content: under the
                    # test of the text it was quoted from, or, quoted from
                    # the user's own text, the test for the quoted node
                    # when someone else wrote it (quotes_inside). A quote
                    # found in the user's own text at all is theirs.
                    inner = entry.found_under.get(quoted_id)
                    if inner is None and self.inside is not None:
                        inner = self.inside(quoted_id)
                    for nested_quote_id in metadata['quote_ids']:
                        if inner is not None and not inner(nested_quote_id):
                            continue
                        if nested_quote_id not in entry.quote_ids:
                            entry.quote_ids.append(nested_quote_id)
                            if inner is not None:
                                entry.found_under[nested_quote_id] = inner
                            else:
                                entry.found_under.pop(nested_quote_id, None)
                        elif inner is None:
                            entry.found_under.pop(nested_quote_id, None)

                    # Mark as embedded (content is now in the export via embedding)
                    # This is tracked separately so _truncate() preserves it
                    self._embedded_ids.add(quoted_id)

                    # Track for final rendering
                    if entry.node_id not in self.embedded_quotes:
                        self.embedded_quotes[entry.node_id] = {}
                    self.embedded_quotes[entry.node_id][quoted_id] = metadata['content']

                    # Re-truncate and restart
                    self._truncate()
                    changed = True
                    break  # Restart outer loop

                if changed:
                    break

            if not changed:
                # Full pass with no changes - all quotes resolved
                break

        if iteration >= max_iterations:
            logger.warning(f"Quote resolution hit max iterations ({max_iterations})")

    def get_resolution_result(self) -> Tuple[Set[int], Dict[int, Dict[int, str]], Set[int]]:
        """
        Get the resolution result.

        Returns:
            Tuple of:
            - Set of node IDs that should be included in the export
            - Dict mapping node_id -> {quoted_id -> content} for embedded quotes
            - Set of node IDs blocked due to ai_usage='none'
        """
        return self.included_ids, self.embedded_quotes, self._ai_blocked_ids

    def get_prompt_preamble(self) -> str:
        """Build a preamble section listing all referenced system prompts."""
        if not self.referenced_prompt_ids:
            return ""
        sorted_ids = sorted(self.referenced_prompt_ids)
        lines = ["## System Prompts Referenced\n"]
        for i, pid in enumerate(sorted_ids):
            content = self._prompt_contents.get(pid, "")
            lines.append(f"### Prompt (ref #{pid})\n")
            lines.append(content)
            if i < len(sorted_ids) - 1:
                lines.append("\n===\n")
            else:
                lines.append("\n")
        return "\n".join(lines)

    def get_artifacts_preamble(self) -> str:
        """Build preamble sections for all referenced artifact types
        ("" for a resolver built without one)."""
        if not self.with_preamble:
            return ""
        sections = []

        # Prompts (reuse existing logic)
        prompt_text = self.get_prompt_preamble()
        if prompt_text:
            sections.append(prompt_text)

        # Profiles
        profile_ids = sorted(self.referenced_artifacts.get("profile", set()))
        if profile_ids:
            lines = ["## User Profiles Referenced\n"]
            for i, pid in enumerate(profile_ids):
                content = self._artifact_contents.get(("profile", pid), "")
                lines.append(f"### User Profile (ref #{pid})\n")
                lines.append(content)
                if i < len(profile_ids) - 1:
                    lines.append("\n===\n")
                else:
                    lines.append("\n")
            sections.append("\n".join(lines))

        # TODOs
        todo_ids = sorted(self.referenced_artifacts.get("todo", set()))
        if todo_ids:
            lines = ["## User TODOs Referenced\n"]
            for i, tid in enumerate(todo_ids):
                content = self._artifact_contents.get(("todo", tid), "")
                lines.append(f"### User TODO (ref #{tid})\n")
                lines.append(content)
                if i < len(todo_ids) - 1:
                    lines.append("\n===\n")
                else:
                    lines.append("\n")
            sections.append("\n".join(lines))

        return "\n===\n\n".join(sections)

    def get_included_entries(self) -> List[NodeEntry]:
        """
        Get the list of included entries (after truncation and resolution).

        Returns:
            List of NodeEntry objects that fit within the token budget
        """
        return self.entries[:self.included_count]


def resolve_quotes_for_export(
    content: str,
    node_id: int,
    embedded_quotes: Dict[int, Dict[int, str]],
    user_id: int,
    ai_blocked_ids: Optional[Set[int]] = None,
    _resolving: Optional[Set[int]] = None,
    reference: Optional[Callable[[int], str]] = None,
    only=None,
    inside=None,
) -> str:
    """
    Resolve quotes in a node's content for export rendering.

    This function is used during final export string building, after the
    ExportQuoteResolver has determined which quotes need embedding.

    Handles nested quotes recursively - if embedded content also has quotes
    that were embedded, those are resolved as well.

    Args:
        content: The node's content with {quote:ID} placeholders
        node_id: The node's ID (to look up embedded quotes)
        embedded_quotes: Dict from ExportQuoteResolver mapping
                        node_id -> {quoted_id -> content}
        user_id: User ID for formatting
        ai_blocked_ids: Optional set of node IDs blocked due to ai_usage='none'.
                       Quotes referencing these nodes show a blocked message.
        _resolving: Internal - tracks IDs being resolved to prevent infinite loops
        reference: Optional quoted node id -> text for a quote resolved
                   by reference. The export passes a marker it fills in
                   with the quoted entry's position label once the whole
                   document is rendered (#592); without it the text
                   names the node id.
        only, inside: as in resolve_quotes: a quote *only* refuses comes
                   back held, and embedded text the user did not write
                   resolves its quotes only through its test and has
                   every marker left in it held (the caller releases).

    Returns:
        Content with quotes resolved (embedded or marked as reference)
    """
    if not has_quotes(content):
        return content

    if _resolving is None:
        _resolving = set()

    if ai_blocked_ids is None:
        ai_blocked_ids = set()

    node_embeds = embedded_quotes.get(node_id, {})

    def replace_quote(match):
        quoted_id = int(match.group(1))
        if only is not None and not only(quoted_id):
            return hold_quotes(match.group(0))

        # Prevent infinite loops
        if quoted_id in _resolving:
            return f'[Circular reference: node #{quoted_id}]'

        # Block content from nodes that don't permit AI usage
        if quoted_id in ai_blocked_ids:
            return f'[Quote #{quoted_id}: AI usage not permitted by author]'

        if quoted_id in node_embeds:
            # Embedded quote - include the content
            embedded_content = node_embeds[quoted_id]
            inner_only = only
            if inner_only is None and inside is not None:
                inner_only = inside(quoted_id)
            if inner_only is not None:
                embedded_content = hold_ext_quotes(embedded_content)

            # Recursively resolve any quotes in the embedded content
            if has_quotes(embedded_content):
                new_resolving = _resolving | {quoted_id}
                embedded_content = resolve_quotes_for_export(
                    embedded_content,
                    node_id,  # Use same node_id to access same embeds dict
                    embedded_quotes,
                    user_id,
                    ai_blocked_ids=ai_blocked_ids,
                    _resolving=new_resolving,
                    reference=reference,
                    only=inner_only,
                    inside=inside,
                )
            if inner_only is not None:
                embedded_content = hold_quotes(embedded_content)

            # Get username for formatting
            from backend.models import Node
            quoted_node = Node.query.get(quoted_id)
            username = quoted_node.user.username if quoted_node and quoted_node.user else "Unknown"
            stamp = quote_stamp(
                iso_utc(quoted_node.created_at) if quoted_node else None)
            when = f", {stamp}" if stamp else ""
            return (f'\n--- Quoted from @{username} (node #{quoted_id}{when}) ---\n'
                    f'{embedded_content}\n--- End quote ---\n')
        else:
            # Resolved by reference - the quoted node is elsewhere in the export
            if reference is not None:
                return reference(quoted_id)
            return f'[See node #{quoted_id} in export]'

    return re.sub(QUOTE_PLACEHOLDER_PATTERN, replace_quote, content)
