"""Tests for magic link utilities."""

import hashlib
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies before importing backend code
mock_flask_login = MagicMock()
sys.modules.setdefault('flask_login', mock_flask_login)

mock_flask_dance = MagicMock()
sys.modules.setdefault('flask_dance', mock_flask_dance)
sys.modules.setdefault('flask_dance.contrib', MagicMock())
sys.modules.setdefault('flask_dance.contrib.twitter', MagicMock())

from flask import Flask
from backend.utils.magic_link import (
    generate_magic_link_token,
    verify_magic_link_token,
    hash_token,
    generate_unique_username,
)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"
    app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 900
    return app


class TestTokenRoundTrip:
    def test_generate_and_verify(self, app):
        with app.app_context():
            token = generate_magic_link_token("user@example.com")
            payload = verify_magic_link_token(token)
            assert payload is not None
            assert payload["email"] == "user@example.com"

    def test_generate_with_next_url(self, app):
        with app.app_context():
            token = generate_magic_link_token("user@example.com", "/dashboard")
            payload = verify_magic_link_token(token)
            assert payload["next_url"] == "/dashboard"

    def test_generate_without_next_url(self, app):
        with app.app_context():
            token = generate_magic_link_token("user@example.com")
            payload = verify_magic_link_token(token)
            assert "next_url" not in payload


class TestTokenExpiry:
    def test_expired_token(self, app):
        app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 1
        with app.app_context():
            token = generate_magic_link_token("user@example.com")
            time.sleep(2)
            payload = verify_magic_link_token(token)
            assert payload is None

    def test_invalid_token(self, app):
        with app.app_context():
            payload = verify_magic_link_token("not-a-valid-token")
            assert payload is None

    def test_tampered_token(self, app):
        with app.app_context():
            token = generate_magic_link_token("user@example.com")
            tampered = token + "x"
            payload = verify_magic_link_token(tampered)
            assert payload is None


class TestHashToken:
    def test_deterministic(self):
        assert hash_token("abc") == hash_token("abc")

    def test_different_tokens_different_hashes(self):
        assert hash_token("abc") != hash_token("xyz")

    def test_returns_hex_digest(self):
        h = hash_token("test")
        assert len(h) == 64  # SHA-256 hex digest length
        assert all(c in "0123456789abcdef" for c in h)


class TestGenerateUniqueUsername:
    """Test username generation from email.

    generate_unique_username does a deferred import of User from
    backend.models, so we mock it via the module that gets imported.
    """

    def _make_mock_user(self, side_effect):
        mock_user = MagicMock()
        mock_user.query.filter_by.return_value.first.side_effect = side_effect
        return mock_user

    def test_simple_email(self, app):
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", {"backend.models": MagicMock(User=mock_user)}):
            # Re-import to pick up the patched module
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john@gmail.com")
            assert result == "john"

    def test_collision_adds_suffix(self, app):
        existing = MagicMock()
        mock_user = self._make_mock_user([existing, None])
        with app.app_context(), \
                patch.dict("sys.modules", {"backend.models": MagicMock(User=mock_user)}):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john@gmail.com")
            assert result == "john2"

    def test_multiple_collisions(self, app):
        existing = MagicMock()
        mock_user = self._make_mock_user([existing, existing, existing, None])
        with app.app_context(), \
                patch.dict("sys.modules", {"backend.models": MagicMock(User=mock_user)}):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john@gmail.com")
            assert result == "john4"

    def test_email_with_dots_and_plus(self, app):
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", {"backend.models": MagicMock(User=mock_user)}):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john.doe+tag@gmail.com")
            assert result == "johndoetag"

    def test_empty_prefix_fallback(self, app):
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", {"backend.models": MagicMock(User=mock_user)}):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("@example.com")
            assert result == "user"
