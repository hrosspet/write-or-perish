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
from typing import Tuple, List, Dict, Optional, Set
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

# Pattern to match {quote:123} where 123 is a node ID
QUOTE_PLACEHOLDER_PATTERN = r'\{quote:(\d+)\}'

# Default max depth for recursive quote resolution in direct conversation
DEFAULT_MAX_DEPTH = 3


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


def get_quote_data(node_ids: List[int], user_id: int) -> Dict[int, Optional[dict]]:
    """
    Fetch node data for the given IDs, checking access permissions.

    Args:
        node_ids: List of node IDs to fetch
        user_id: ID of the user requesting access (for permission checks)

    Returns:
        Dict mapping node_id to node data dict (or None if not accessible)
    """
    from backend.models import Node
    from backend.utils.privacy import can_user_access_node

    result = {}

    for node_id in node_ids:
        node = Node.query.get(node_id)
        if node and can_user_access_node(node, user_id):
            result[node_id] = {
                "id": node.id,
                "content": node.get_content(),
                "username": node.user.username if node.user else "Unknown",
                "user_id": node.user_id,
                "created_at": node.created_at.isoformat() if node.created_at else None,
                "node_type": node.node_type,
            }
        else:
            result[node_id] = None

    return result


def resolve_quotes(
    content: str,
    user_id: int,
    for_llm: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
    _seen_ids: Optional[Set[int]] = None
) -> Tuple[str, List[int]]:
    """
    Replace {quote:ID} placeholders with quoted node content, recursively.

    Args:
        content: Text containing {quote:ID} placeholders
        user_id: ID of requesting user (for access checks)
        for_llm: If True, wrap content in XML tags for LLM context;
                 if False, use human-readable format
        max_depth: Maximum recursion depth (default 3). Set to 1 for no recursion.
        _seen_ids: Internal - tracks visited node IDs to prevent cycles

    Returns:
        Tuple of (resolved_content, list_of_quoted_node_ids)
    """
    if not content:
        return content, []

    if max_depth <= 0:
        return content, []

    if _seen_ids is None:
        _seen_ids = set()

    quote_ids = find_quote_ids(content)
    if not quote_ids:
        return content, []

    # Fetch all quoted nodes
    quote_data = get_quote_data(quote_ids, user_id)
    resolved_ids = []

    def replace_quote(match):
        node_id = int(match.group(1))
        data = quote_data.get(node_id)

        if data is None:
            # Node not found or not accessible
            if for_llm:
                return f"[Quote #{node_id}: not accessible]"
            else:
                return f"[Quote not accessible: node {node_id}]"

        # Cycle detection
        if node_id in _seen_ids:
            if for_llm:
                return f"[Quote #{node_id}: circular reference]"
            else:
                return f"[Circular quote reference: node {node_id}]"

        resolved_ids.append(node_id)
        node_content = data["content"] or ""
        username = data["username"]

        # Recursively resolve quotes in the quoted content
        if max_depth > 1 and has_quotes(node_content):
            new_seen = _seen_ids | {node_id}
            node_content, nested_ids = resolve_quotes(
                node_content,
                user_id,
                for_llm=for_llm,
                max_depth=max_depth - 1,
                _seen_ids=new_seen
            )
            resolved_ids.extend(nested_ids)

        if for_llm:
            # XML format for clear LLM context
            return f'<quoted_node id="{node_id}" author="{username}">\n{node_content}\n</quoted_node>'
        else:
            # Human-readable format for exports
            return f'\n--- Quoted from @{username} (node #{node_id}) ---\n{node_content}\n--- End quote ---\n'

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
        included_ids, embedded_quotes = resolver.get_resolution_result()
    """

    def __init__(self, user_id: int, max_tokens: int):
        """
        Initialize the resolver.

        Args:
            user_id: User ID for access checks when fetching quoted content
            max_tokens: Maximum tokens allowed in the export
        """
        self.user_id = user_id
        self.max_tokens = max_tokens
        self.entries: List[NodeEntry] = []
        self.included_count: int = 0
        self.included_ids: Set[int] = set()
        # Maps node_id -> {quoted_node_id -> embedded_content} for final rendering
        self.embedded_quotes: Dict[int, Dict[int, str]] = {}
        # Cache of node metadata (tokens, quote_ids) to avoid repeated DB queries
        self._node_cache: Dict[int, dict] = {}

    def add_node(
        self,
        node_id: int,
        created_at: 'datetime',
        content: str,
        token_count: Optional[int] = None
    ):
        """
        Add a node to the export.

        Args:
            node_id: The node's ID
            created_at: When the node was created (for sorting)
            content: The node's content (used to extract quote IDs and estimate tokens)
            token_count: Optional pre-computed token count
        """
        from backend.utils.tokens import approximate_token_count

        if token_count is None:
            token_count = approximate_token_count(content)

        quote_ids = find_quote_ids(content)

        entry = NodeEntry(
            node_id=node_id,
            created_at=created_at,
            base_tokens=token_count,
            quote_ids=quote_ids
        )
        self.entries.append(entry)

        # Cache for later use
        self._node_cache[node_id] = {
            'tokens': token_count,
            'quote_ids': quote_ids,
            'content': content
        }

    def _truncate(self):
        """
        Recalculate which entries fit within max_tokens.

        Entries are kept in order (assumed to be sorted newest-first),
        and we include as many as fit within the budget.
        """
        total = 0
        self.included_count = 0
        self.included_ids = set()

        for i, entry in enumerate(self.entries):
            entry_tokens = entry.total_tokens
            if total + entry_tokens > self.max_tokens:
                self.included_count = i
                return
            total += entry_tokens
            self.included_ids.add(entry.node_id)

        self.included_count = len(self.entries)

    def _get_node_metadata(self, node_id: int) -> Optional[dict]:
        """
        Get metadata for a node (tokens, quote_ids, content).

        Uses cache if available, otherwise fetches from database.
        """
        if node_id in self._node_cache:
            return self._node_cache[node_id]

        # Fetch from database
        from backend.models import Node
        from backend.utils.privacy import can_user_access_node
        from backend.utils.tokens import approximate_token_count

        node = Node.query.get(node_id)
        if not node or not can_user_access_node(node, self.user_id):
            return None

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
        # Sort entries by created_at descending (newest first)
        self.entries.sort(key=lambda e: e.created_at, reverse=True)

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

                    # Inherit quotes from the embedded content
                    for nested_quote_id in metadata['quote_ids']:
                        if nested_quote_id not in entry.quote_ids:
                            entry.quote_ids.append(nested_quote_id)

                    # Mark as included (content is now in the export via embedding)
                    self.included_ids.add(quoted_id)

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

    def get_resolution_result(self) -> Tuple[Set[int], Dict[int, Dict[int, str]]]:
        """
        Get the resolution result.

        Returns:
            Tuple of:
            - Set of node IDs that should be included in the export
            - Dict mapping node_id -> {quoted_id -> content} for embedded quotes
        """
        return self.included_ids, self.embedded_quotes

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
    _resolving: Optional[Set[int]] = None
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
        _resolving: Internal - tracks IDs being resolved to prevent infinite loops

    Returns:
        Content with quotes resolved (embedded or marked as reference)
    """
    if not has_quotes(content):
        return content

    if _resolving is None:
        _resolving = set()

    node_embeds = embedded_quotes.get(node_id, {})

    def replace_quote(match):
        quoted_id = int(match.group(1))

        # Prevent infinite loops
        if quoted_id in _resolving:
            return f'[Circular reference: node #{quoted_id}]'

        if quoted_id in node_embeds:
            # Embedded quote - include the content
            embedded_content = node_embeds[quoted_id]

            # Recursively resolve any quotes in the embedded content
            if has_quotes(embedded_content):
                new_resolving = _resolving | {quoted_id}
                embedded_content = resolve_quotes_for_export(
                    embedded_content,
                    node_id,  # Use same node_id to access same embeds dict
                    embedded_quotes,
                    user_id,
                    _resolving=new_resolving
                )

            # Get username for formatting
            from backend.models import Node
            quoted_node = Node.query.get(quoted_id)
            username = quoted_node.user.username if quoted_node and quoted_node.user else "Unknown"
            return f'\n--- Quoted from @{username} (node #{quoted_id}) ---\n{embedded_content}\n--- End quote ---\n'
        else:
            # Resolved by reference - the quoted node is elsewhere in the export
            return f'[See node #{quoted_id} in export]'

    return re.sub(QUOTE_PLACEHOLDER_PATTERN, replace_quote, content)
