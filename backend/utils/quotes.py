"""
Quote resolution utilities for {quote:ID} placeholders.

This module provides functionality to detect and resolve inline node quotes,
similar to how {user_profile} and {user_export} placeholders work.
"""
import re
from typing import Tuple, List, Dict, Optional
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

# Pattern to match {quote:123} where 123 is a node ID
QUOTE_PLACEHOLDER_PATTERN = r'\{quote:(\d+)\}'


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
    from backend.models import Node, User
    from backend.utils.privacy import can_user_access_node

    result = {}
    user = User.query.get(user_id) if user_id else None

    for node_id in node_ids:
        node = Node.query.get(node_id)
        if node and can_user_access_node(user, node):
            result[node_id] = {
                "id": node.id,
                "content": node.content,
                "username": node.user.username if node.user else "Unknown",
                "user_id": node.user_id,
                "created_at": node.created_at.isoformat() if node.created_at else None,
                "node_type": node.node_type,
            }
        else:
            result[node_id] = None

    return result


def resolve_quotes(content: str, user_id: int, for_llm: bool = False) -> Tuple[str, List[int]]:
    """
    Replace {quote:ID} placeholders with quoted node content.

    Args:
        content: Text containing {quote:ID} placeholders
        user_id: ID of requesting user (for access checks)
        for_llm: If True, wrap content in XML tags for LLM context;
                 if False, use human-readable format

    Returns:
        Tuple of (resolved_content, list_of_quoted_node_ids)
    """
    if not content:
        return content, []

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

        resolved_ids.append(node_id)
        node_content = data["content"] or ""
        username = data["username"]

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
