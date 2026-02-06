"""Tests for privacy utilities."""

import pytest
import sys
from unittest.mock import MagicMock, patch

# Mock flask_login before any backend imports
mock_flask_login = MagicMock()
sys.modules['flask_login'] = mock_flask_login

# Now we can safely import from backend
from backend.utils.privacy import (
    PrivacyLevel,
    AIUsage,
    validate_privacy_level,
    validate_ai_usage,
    can_user_access_node,
    can_user_edit_node,
    find_human_owner,
    can_ai_use_node_for_chat,
    can_ai_use_node_for_training,
    get_default_privacy_settings,
    VALID_PRIVACY_LEVELS,
    VALID_AI_USAGE
)


class TestPrivacyEnums:
    """Test privacy level and AI usage enums."""

    def test_privacy_level_values(self):
        """Test that PrivacyLevel enum has correct values."""
        assert PrivacyLevel.PRIVATE == "private"
        assert PrivacyLevel.CIRCLES == "circles"
        assert PrivacyLevel.PUBLIC == "public"

    def test_ai_usage_values(self):
        """Test that AIUsage enum has correct values."""
        assert AIUsage.NONE == "none"
        assert AIUsage.CHAT == "chat"
        assert AIUsage.TRAIN == "train"

    def test_valid_privacy_levels_set(self):
        """Test that VALID_PRIVACY_LEVELS contains all enum values."""
        assert VALID_PRIVACY_LEVELS == {"private", "circles", "public"}

    def test_valid_ai_usage_set(self):
        """Test that VALID_AI_USAGE contains all enum values."""
        assert VALID_AI_USAGE == {"none", "chat", "train"}


class TestValidationFunctions:
    """Test validation functions."""

    def test_validate_privacy_level_valid(self):
        """Test that valid privacy levels are accepted."""
        assert validate_privacy_level("private") is True
        assert validate_privacy_level("circles") is True
        assert validate_privacy_level("public") is True

    def test_validate_privacy_level_invalid(self):
        """Test that invalid privacy levels are rejected."""
        assert validate_privacy_level("invalid") is False
        assert validate_privacy_level("") is False
        assert validate_privacy_level("PRIVATE") is False  # Case sensitive
        assert validate_privacy_level(None) is False

    def test_validate_ai_usage_valid(self):
        """Test that valid AI usage values are accepted."""
        assert validate_ai_usage("none") is True
        assert validate_ai_usage("chat") is True
        assert validate_ai_usage("train") is True

    def test_validate_ai_usage_invalid(self):
        """Test that invalid AI usage values are rejected."""
        assert validate_ai_usage("invalid") is False
        assert validate_ai_usage("") is False
        assert validate_ai_usage("CHAT") is False  # Case sensitive
        assert validate_ai_usage(None) is False


class TestCanUserAccessNode:
    """Test node access authorization."""

    def test_owner_can_access_private_node(self):
        """Test that node owner can always access their private nodes."""
        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.PRIVATE

        assert can_user_access_node(node, user_id=1) is True

    def test_owner_can_access_public_node(self):
        """Test that node owner can access their public nodes."""
        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.PUBLIC

        assert can_user_access_node(node, user_id=1) is True

    def test_other_user_cannot_access_private_node(self):
        """Test that other users cannot access private nodes."""
        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.PRIVATE

        assert can_user_access_node(node, user_id=2) is False

    def test_other_user_can_access_public_node(self):
        """Test that other users can access public nodes."""
        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.PUBLIC

        assert can_user_access_node(node, user_id=2) is True

    def test_circles_not_yet_implemented(self):
        """Test that circles privacy level denies access (not yet implemented)."""
        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.CIRCLES

        # Owner can still access
        assert can_user_access_node(node, user_id=1) is True
        # Others cannot (circles not implemented yet)
        assert can_user_access_node(node, user_id=2) is False

    @patch('backend.utils.privacy.current_user')
    def test_uses_current_user_when_no_user_id_provided(self, mock_current_user):
        """Test that function uses current_user when no user_id is provided."""
        mock_current_user.is_authenticated = True
        mock_current_user.id = 1

        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.PRIVATE

        assert can_user_access_node(node) is True

    @patch('backend.utils.privacy.current_user')
    def test_denies_access_when_not_authenticated(self, mock_current_user):
        """Test that unauthenticated users are denied access."""
        mock_current_user.is_authenticated = False

        node = MagicMock()
        node.user_id = 1
        node.privacy_level = PrivacyLevel.PUBLIC

        assert can_user_access_node(node) is False

    def test_handles_missing_privacy_level_attribute(self):
        """Test that function handles nodes without privacy_level gracefully."""
        node = MagicMock()
        node.user_id = 1
        del node.privacy_level  # Simulate missing attribute

        # Should default to private behavior
        assert can_user_access_node(node, user_id=1) is True
        assert can_user_access_node(node, user_id=2) is False


class TestAIUsageChecks:
    """Test AI usage permission checks."""

    def test_can_ai_use_for_chat_with_chat_permission(self):
        """Test that AI can use nodes with 'chat' permission."""
        node = MagicMock()
        node.ai_usage = AIUsage.CHAT

        assert can_ai_use_node_for_chat(node) is True

    def test_can_ai_use_for_chat_with_train_permission(self):
        """Test that AI can use nodes with 'train' permission for chat."""
        node = MagicMock()
        node.ai_usage = AIUsage.TRAIN

        assert can_ai_use_node_for_chat(node) is True

    def test_cannot_ai_use_for_chat_with_none_permission(self):
        """Test that AI cannot use nodes with 'none' permission."""
        node = MagicMock()
        node.ai_usage = AIUsage.NONE

        assert can_ai_use_node_for_chat(node) is False

    def test_can_ai_use_for_training_with_train_permission(self):
        """Test that AI can use nodes with 'train' permission for training."""
        node = MagicMock()
        node.ai_usage = AIUsage.TRAIN

        assert can_ai_use_node_for_training(node) is True

    def test_cannot_ai_use_for_training_with_chat_permission(self):
        """Test that AI cannot use nodes with 'chat' permission for training."""
        node = MagicMock()
        node.ai_usage = AIUsage.CHAT

        assert can_ai_use_node_for_training(node) is False

    def test_cannot_ai_use_for_training_with_none_permission(self):
        """Test that AI cannot use nodes with 'none' permission for training."""
        node = MagicMock()
        node.ai_usage = AIUsage.NONE

        assert can_ai_use_node_for_training(node) is False

    def test_handles_missing_ai_usage_attribute(self):
        """Test that functions handle nodes without ai_usage gracefully."""
        node = MagicMock()
        del node.ai_usage  # Simulate missing attribute

        # Should default to NONE behavior
        assert can_ai_use_node_for_chat(node) is False
        assert can_ai_use_node_for_training(node) is False


class TestFindHumanOwner:
    """Test find_human_owner function for recursive LLM chain traversal."""

    def test_human_to_llm_chain(self):
        """Test Human → LLM: should return Human's user_id."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1

        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100  # LLM account
        llm_node.parent = human_node

        assert find_human_owner(llm_node) == 1

    def test_human_to_llm_to_llm_chain(self):
        """Test Human → LLM → LLM: should return Human's user_id."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        llm_node_1 = MagicMock()
        llm_node_1.node_type = "llm"
        llm_node_1.user_id = 100  # LLM account
        llm_node_1.parent = human_node

        llm_node_2 = MagicMock()
        llm_node_2.node_type = "llm"
        llm_node_2.user_id = 101  # Another LLM account
        llm_node_2.parent = llm_node_1

        assert find_human_owner(llm_node_2) == 1

    def test_deep_llm_chain(self):
        """Test Human → LLM → LLM → LLM: should still return Human's user_id."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 5
        human_node.parent = None

        llm_node_1 = MagicMock()
        llm_node_1.node_type = "llm"
        llm_node_1.user_id = 100
        llm_node_1.parent = human_node

        llm_node_2 = MagicMock()
        llm_node_2.node_type = "llm"
        llm_node_2.user_id = 101
        llm_node_2.parent = llm_node_1

        llm_node_3 = MagicMock()
        llm_node_3.node_type = "llm"
        llm_node_3.user_id = 102
        llm_node_3.parent = llm_node_2

        assert find_human_owner(llm_node_3) == 5

    def test_llm_with_no_parent(self):
        """Test LLM node with no parent: should return None."""
        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100
        llm_node.parent = None

        assert find_human_owner(llm_node) is None

    def test_human_node_returns_none(self):
        """Test that a human node (no parent) returns None for find_human_owner."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        # find_human_owner looks at parent chain, not the node itself
        assert find_human_owner(human_node) is None


class TestCanUserAccessNodeLLMChains:
    """Test can_user_access_node with LLM response chains."""

    def test_human_can_access_direct_llm_response(self):
        """Human can access LLM response directly under their node."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1

        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100
        llm_node.privacy_level = PrivacyLevel.PRIVATE
        llm_node.parent = human_node

        assert can_user_access_node(llm_node, user_id=1) is True

    def test_human_can_access_nested_llm_response(self):
        """Human can access LLM response nested under another LLM response."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        llm_node_1 = MagicMock()
        llm_node_1.node_type = "llm"
        llm_node_1.user_id = 100
        llm_node_1.parent = human_node

        llm_node_2 = MagicMock()
        llm_node_2.node_type = "llm"
        llm_node_2.user_id = 101
        llm_node_2.privacy_level = PrivacyLevel.PRIVATE
        llm_node_2.parent = llm_node_1

        assert can_user_access_node(llm_node_2, user_id=1) is True

    def test_other_user_cannot_access_private_llm_response(self):
        """Other users cannot access private LLM responses."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100
        llm_node.privacy_level = PrivacyLevel.PRIVATE
        llm_node.parent = human_node

        # User 2 is not the human owner
        assert can_user_access_node(llm_node, user_id=2) is False


class TestCanUserEditNodeLLMChains:
    """Test can_user_edit_node with LLM response chains."""

    def test_human_can_edit_direct_llm_response(self):
        """Human can edit LLM response directly under their node."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1

        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100
        llm_node.parent = human_node

        assert can_user_edit_node(llm_node, user_id=1) is True

    def test_human_can_edit_nested_llm_response(self):
        """Human can edit LLM response nested under another LLM response."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        llm_node_1 = MagicMock()
        llm_node_1.node_type = "llm"
        llm_node_1.user_id = 100
        llm_node_1.parent = human_node

        llm_node_2 = MagicMock()
        llm_node_2.node_type = "llm"
        llm_node_2.user_id = 101
        llm_node_2.parent = llm_node_1

        assert can_user_edit_node(llm_node_2, user_id=1) is True

    def test_human_can_edit_deeply_nested_llm_response(self):
        """Human can edit LLM response deeply nested in chain."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 5
        human_node.parent = None

        llm_node_1 = MagicMock()
        llm_node_1.node_type = "llm"
        llm_node_1.user_id = 100
        llm_node_1.parent = human_node

        llm_node_2 = MagicMock()
        llm_node_2.node_type = "llm"
        llm_node_2.user_id = 101
        llm_node_2.parent = llm_node_1

        llm_node_3 = MagicMock()
        llm_node_3.node_type = "llm"
        llm_node_3.user_id = 102
        llm_node_3.parent = llm_node_2

        assert can_user_edit_node(llm_node_3, user_id=5) is True

    def test_other_user_cannot_edit_llm_response(self):
        """Other users cannot edit LLM responses they didn't request."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100
        llm_node.parent = human_node

        # User 2 is not the human owner
        assert can_user_edit_node(llm_node, user_id=2) is False

    def test_owner_can_still_edit_own_node(self):
        """User can still edit their own non-LLM node."""
        node = MagicMock()
        node.node_type = "user"
        node.user_id = 1

        assert can_user_edit_node(node, user_id=1) is True

    def test_llm_account_cannot_edit_own_node(self):
        """LLM account being the owner doesn't grant edit to random users."""
        human_node = MagicMock()
        human_node.node_type = "user"
        human_node.user_id = 1
        human_node.parent = None

        llm_node = MagicMock()
        llm_node.node_type = "llm"
        llm_node.user_id = 100  # LLM account
        llm_node.parent = human_node

        # User 100 (the LLM account) could technically edit as owner,
        # but user 99 (a random user) should not be able to
        assert can_user_edit_node(llm_node, user_id=99) is False


class TestDefaultPrivacySettings:
    """Test default privacy settings."""

    def test_get_default_privacy_settings(self):
        """Test that default privacy settings are correct."""
        defaults = get_default_privacy_settings()

        assert defaults['privacy_level'] == PrivacyLevel.PRIVATE
        assert defaults['ai_usage'] == AIUsage.NONE
