"""Privacy and AI usage utilities for Write or Perish.

This module provides enums, validation, and authorization functions for
the two-column privacy system:
- privacy_level: controls who can access the node (private/circles/public)
- ai_usage: controls how AI can use the node's content (none/chat/train)
"""

from enum import Enum
from typing import Optional
from flask_login import current_user


class PrivacyLevel(str, Enum):
    """Privacy level controlling who can access a node."""
    PRIVATE = "private"  # Only the owner can read
    CIRCLES = "circles"  # Shared with specific user-defined groups (future)
    PUBLIC = "public"    # Visible to all users


class AIUsage(str, Enum):
    """AI usage permission controlling how AI can use node content."""
    NONE = "none"   # No AI usage allowed
    CHAT = "chat"   # AI can use for generating responses (not training)
    TRAIN = "train" # AI can use for training data


# Valid values for validation
VALID_PRIVACY_LEVELS = {level.value for level in PrivacyLevel}
VALID_AI_USAGE = {usage.value for usage in AIUsage}


def validate_privacy_level(privacy_level: str) -> bool:
    """Validate that a privacy level is valid.

    Args:
        privacy_level: The privacy level to validate

    Returns:
        True if valid, False otherwise
    """
    return privacy_level in VALID_PRIVACY_LEVELS


def validate_ai_usage(ai_usage: str) -> bool:
    """Validate that an AI usage value is valid.

    Args:
        ai_usage: The AI usage value to validate

    Returns:
        True if valid, False otherwise
    """
    return ai_usage in VALID_AI_USAGE


def can_user_access_node(node, user_id: Optional[int] = None) -> bool:
    """Check if a user can access a node based on privacy level.

    Args:
        node: The Node object to check access for
        user_id: The user ID to check (defaults to current_user.id)

    Returns:
        True if user can access, False otherwise
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id

    # Owner can always access their own nodes
    if node.user_id == user_id:
        return True

    # For LLM nodes: check if the user is the requester (parent node's owner)
    # This allows users to access AI responses they requested
    node_type = getattr(node, 'node_type', 'user')
    if node_type == "llm" and node.parent and node.parent.user_id == user_id:
        return True

    # Check privacy level
    privacy_level = getattr(node, 'privacy_level', PrivacyLevel.PRIVATE)

    if privacy_level == PrivacyLevel.PRIVATE:
        return False
    elif privacy_level == PrivacyLevel.PUBLIC:
        return True
    elif privacy_level == PrivacyLevel.CIRCLES:
        # TODO: Implement circles membership check when circles feature is built
        return False

    return False


def can_user_edit_node(node, user_id: Optional[int] = None) -> bool:
    """Check if a user can edit a node.

    A user can edit a node if they are:
    1. The owner of the node (node.user_id == user_id)
    2. The "LLM requester" - the owner of the parent node for an AI-generated node
       (useful when users want to edit AI responses they requested)

    Args:
        node: The Node object to check edit permissions for
        user_id: The user ID to check (defaults to current_user.id)

    Returns:
        True if user can edit, False otherwise
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id

    # Check if user is the owner
    is_owner = node.user_id == user_id

    # Check if user is the LLM requester (parent node owner)
    is_llm_requester = (
        node.node_type == "llm" and
        node.parent and
        node.parent.user_id == user_id
    )

    return is_owner or is_llm_requester


def can_ai_use_node_for_chat(node) -> bool:
    """Check if AI can use a node's content for generating chat responses.

    Args:
        node: The Node object to check

    Returns:
        True if AI can use for chat, False otherwise
    """
    ai_usage = getattr(node, 'ai_usage', AIUsage.NONE)
    return ai_usage in {AIUsage.CHAT, AIUsage.TRAIN}


def can_ai_use_node_for_training(node) -> bool:
    """Check if AI can use a node's content for training data.

    Args:
        node: The Node object to check

    Returns:
        True if AI can use for training, False otherwise
    """
    ai_usage = getattr(node, 'ai_usage', AIUsage.NONE)
    return ai_usage == AIUsage.TRAIN


def get_default_privacy_settings() -> dict:
    """Get the default privacy settings for new nodes.

    Returns:
        Dictionary with default privacy_level and ai_usage
    """
    return {
        'privacy_level': PrivacyLevel.PRIVATE,
        'ai_usage': AIUsage.NONE
    }
