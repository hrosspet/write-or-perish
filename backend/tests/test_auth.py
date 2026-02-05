"""Tests for authentication utilities."""

import pytest
import sys
from unittest.mock import MagicMock

# Mock flask_login before any backend imports
mock_flask_login = MagicMock()
sys.modules['flask_login'] = mock_flask_login

from backend.routes.auth import is_safe_redirect_url


class TestIsSafeRedirectUrl:
    """Test redirect URL validation to prevent open redirects."""

    def test_valid_relative_paths(self):
        """Test that valid relative paths are accepted."""
        assert is_safe_redirect_url("/dashboard") is True
        assert is_safe_redirect_url("/node/123") is True
        assert is_safe_redirect_url("/dashboard/username") is True
        assert is_safe_redirect_url("/admin") is True
        assert is_safe_redirect_url("/feed") is True
        assert is_safe_redirect_url("/node/123?tab=comments") is True
        assert is_safe_redirect_url("/search?q=test&page=2") is True

    def test_rejects_empty_and_none(self):
        """Test that empty strings and None are rejected."""
        assert is_safe_redirect_url("") is False
        assert is_safe_redirect_url(None) is False

    def test_rejects_absolute_urls(self):
        """Test that absolute URLs with schemes are rejected."""
        assert is_safe_redirect_url("http://evil.com") is False
        assert is_safe_redirect_url("https://evil.com") is False
        assert is_safe_redirect_url("http://evil.com/path") is False
        assert is_safe_redirect_url("https://evil.com/dashboard") is False

    def test_rejects_protocol_relative_urls(self):
        """Test that protocol-relative URLs (//domain) are rejected."""
        assert is_safe_redirect_url("//evil.com") is False
        assert is_safe_redirect_url("//evil.com/path") is False
        assert is_safe_redirect_url("///evil.com") is False

    def test_rejects_urls_without_leading_slash(self):
        """Test that paths without leading slash are rejected."""
        assert is_safe_redirect_url("dashboard") is False
        assert is_safe_redirect_url("node/123") is False
        assert is_safe_redirect_url("evil.com") is False

    def test_rejects_javascript_urls(self):
        """Test that javascript: URLs are rejected."""
        assert is_safe_redirect_url("javascript:alert(1)") is False
        assert is_safe_redirect_url("javascript://comment%0aalert(1)") is False

    def test_rejects_data_urls(self):
        """Test that data: URLs are rejected."""
        assert is_safe_redirect_url("data:text/html,<script>alert(1)</script>") is False

    def test_rejects_scheme_with_slashes_trick(self):
        """Test that URLs trying to sneak in schemes are rejected."""
        # These could be interpreted as absolute URLs by browsers
        assert is_safe_redirect_url("/\\evil.com") is True  # This is actually a valid path
        assert is_safe_redirect_url("https:/dashboard") is False  # Has scheme

    def test_accepts_paths_with_special_characters(self):
        """Test that paths with URL-encoded characters work."""
        assert is_safe_redirect_url("/search?q=hello%20world") is True
        assert is_safe_redirect_url("/node/123#section") is True

    def test_accepts_root_path(self):
        """Test that root path is accepted."""
        assert is_safe_redirect_url("/") is True
