"""Tests for prompt default-hash tracking and auto-upgrade logic."""

import hashlib
import sys
import types
from unittest.mock import MagicMock, patch

# Mock flask_login before any backend imports
mock_flask_login = MagicMock()
sys.modules['flask_login'] = mock_flask_login

from backend.utils.prompts import default_prompt_hash, get_user_prompt


def _fake_models_module(query_result):
    """Create a real module with a mock UserPrompt whose query returns
    *query_result*.  Using a real module avoids MagicMock-as-module
    attribute lookup issues that arise when test_quotes.py replaces
    sys.modules['backend.models'] with a MagicMock at collection time.
    """
    mod = types.ModuleType('backend.models')
    mock_cls = MagicMock()
    mock_cls.query.filter_by.return_value \
        .order_by.return_value \
        .first.return_value = query_result
    mod.UserPrompt = mock_cls
    return mod


class TestDefaultPromptHash:
    """Tests for the default_prompt_hash helper."""

    @patch('backend.utils.prompts.load_default_prompt')
    def test_returns_consistent_hash(self, mock_load):
        mock_load.return_value = "Hello world"
        expected = hashlib.sha256("Hello world".encode()).hexdigest()
        assert default_prompt_hash('reflect') == expected
        # Calling again should return the same hash
        assert default_prompt_hash('reflect') == expected

    @patch('backend.utils.prompts.load_default_prompt')
    def test_returns_none_for_unknown_key(self, mock_load):
        mock_load.return_value = None
        assert default_prompt_hash('nonexistent') is None

    @patch('backend.utils.prompts.load_default_prompt')
    def test_different_content_different_hash(self, mock_load):
        mock_load.return_value = "Version 1"
        hash1 = default_prompt_hash('reflect')
        mock_load.return_value = "Version 2"
        hash2 = default_prompt_hash('reflect')
        assert hash1 != hash2


class TestGetUserPromptAutoUpgrade:
    """Tests for the auto-upgrade behaviour in get_user_prompt."""

    @patch('backend.utils.prompts.load_default_prompt')
    @patch('backend.utils.prompts.default_prompt_hash')
    def test_auto_upgrades_default_row_when_hash_mismatches(
        self, mock_hash, mock_load
    ):
        """generated_by='default' + stale hash -> return file default."""
        mock_hash.return_value = "new_hash_abc"
        mock_load.return_value = "Updated default content"

        mock_prompt = MagicMock()
        mock_prompt.generated_by = "default"
        mock_prompt.based_on_default_hash = "old_hash_xyz"
        mock_prompt.get_content.return_value = "Old default content"

        fake_mod = _fake_models_module(mock_prompt)
        with patch.dict(sys.modules, {'backend.models': fake_mod}):
            result = get_user_prompt(1, 'reflect')
            assert result == "Updated default content"

    @patch('backend.utils.prompts.load_default_prompt')
    @patch('backend.utils.prompts.default_prompt_hash')
    def test_returns_db_content_when_default_hash_matches(
        self, mock_hash, mock_load
    ):
        """generated_by='default' + matching hash -> return DB content."""
        mock_hash.return_value = "same_hash"

        mock_prompt = MagicMock()
        mock_prompt.generated_by = "default"
        mock_prompt.based_on_default_hash = "same_hash"
        mock_prompt.get_content.return_value = "DB content"

        fake_mod = _fake_models_module(mock_prompt)
        with patch.dict(sys.modules, {'backend.models': fake_mod}):
            result = get_user_prompt(1, 'reflect')
            assert result == "DB content"

    @patch('backend.utils.prompts.load_default_prompt')
    @patch('backend.utils.prompts.default_prompt_hash')
    def test_does_not_upgrade_user_edited_prompt(
        self, mock_hash, mock_load
    ):
        """generated_by='user' -> always return DB content."""
        mock_hash.return_value = "new_hash"

        mock_prompt = MagicMock()
        mock_prompt.generated_by = "user"
        mock_prompt.based_on_default_hash = "old_hash"
        mock_prompt.get_content.return_value = "User's custom content"

        fake_mod = _fake_models_module(mock_prompt)
        with patch.dict(sys.modules, {'backend.models': fake_mod}):
            result = get_user_prompt(1, 'reflect')
            assert result == "User's custom content"

    @patch('backend.utils.prompts.load_default_prompt')
    @patch('backend.utils.prompts.default_prompt_hash')
    def test_does_not_upgrade_reverted_prompt(
        self, mock_hash, mock_load
    ):
        """generated_by='revert' -> always return DB content."""
        mock_hash.return_value = "new_hash"

        mock_prompt = MagicMock()
        mock_prompt.generated_by = "revert"
        mock_prompt.based_on_default_hash = "old_hash"
        mock_prompt.get_content.return_value = "Reverted content"

        fake_mod = _fake_models_module(mock_prompt)
        with patch.dict(sys.modules, {'backend.models': fake_mod}):
            result = get_user_prompt(1, 'reflect')
            assert result == "Reverted content"

    @patch('backend.utils.prompts.load_default_prompt')
    def test_falls_back_to_file_when_no_db_row(self, mock_load):
        """No DB row -> return file default (unchanged behaviour)."""
        mock_load.return_value = "File content"

        fake_mod = _fake_models_module(None)
        with patch.dict(sys.modules, {'backend.models': fake_mod}):
            result = get_user_prompt(1, 'reflect')
            assert result == "File content"

    @patch('backend.utils.prompts.load_default_prompt')
    @patch('backend.utils.prompts.default_prompt_hash')
    def test_auto_upgrades_default_row_with_null_hash(
        self, mock_hash, mock_load
    ):
        """generated_by='default' + NULL hash (legacy row) -> auto-upgrade."""
        mock_hash.return_value = "current_hash"
        mock_load.return_value = "Current default"

        mock_prompt = MagicMock()
        mock_prompt.generated_by = "default"
        mock_prompt.based_on_default_hash = None
        mock_prompt.get_content.return_value = "Stale default"

        fake_mod = _fake_models_module(mock_prompt)
        with patch.dict(sys.modules, {'backend.models': fake_mod}):
            result = get_user_prompt(1, 'reflect')
            assert result == "Current default"
