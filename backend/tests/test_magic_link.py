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
    generate_email_change_token,
    verify_email_change_token,
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


class TestEmailChangeTokens:
    """Signed under their own salt (#260): an email-change token must never
    pass for a sign-in token — /auth/magic-link/verify would sign in, or
    create, an account for the address it carries — nor the reverse."""

    def test_round_trip(self, app):
        with app.app_context():
            token = generate_email_change_token(7, "new@example.com")
            assert verify_email_change_token(token) == {
                "user_id": 7, "email": "new@example.com"}

    def test_not_a_sign_in_token_and_vice_versa(self, app):
        with app.app_context():
            change = generate_email_change_token(7, "new@example.com")
            sign_in = generate_magic_link_token("new@example.com")
            assert verify_magic_link_token(change) is None
            assert verify_email_change_token(sign_in) is None

    def test_has_its_own_lifetime(self, app):
        app.config["EMAIL_CHANGE_EXPIRY_SECONDS"] = 3600
        with app.app_context():
            with patch("itsdangerous.timed.TimestampSigner.get_timestamp",
                       return_value=int(time.time()) - 1800):
                half_hour_old = generate_email_change_token(7, "a@example.com")
            with patch("itsdangerous.timed.TimestampSigner.get_timestamp",
                       return_value=int(time.time()) - 3700):
                too_old = generate_email_change_token(7, "a@example.com")
            # older than a sign-in link lives (900 s), still good
            assert verify_email_change_token(half_hour_old) is not None
            assert verify_email_change_token(too_old) is None

    def test_malformed(self, app):
        with app.app_context():
            assert verify_email_change_token("not-a-token") is None
            assert verify_email_change_token(
                generate_email_change_token(7, "a@example.com") + "x") is None


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

    generate_unique_username does deferred imports of User (backend.models)
    and db (backend.extensions), so we mock them via the modules that get
    imported. The uniqueness check is case-insensitive (query.filter with
    lower()), so the mock tracks .filter, not .filter_by.
    """

    def _make_mock_user(self, side_effect):
        mock_user = MagicMock()
        mock_user.query.filter.return_value.first.side_effect = side_effect
        return mock_user

    def _patched_modules(self, mock_user):
        return {
            "backend.models": MagicMock(User=mock_user),
            "backend.extensions": MagicMock(),
        }

    def test_simple_email(self, app):
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            # Re-import to pick up the patched module
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john@gmail.com")
            assert result == "john"

    def test_collision_adds_suffix(self, app):
        existing = MagicMock()
        mock_user = self._make_mock_user([existing, None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john@gmail.com")
            assert result == "john2"

    def test_multiple_collisions(self, app):
        existing = MagicMock()
        mock_user = self._make_mock_user([existing, existing, existing, None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john@gmail.com")
            assert result == "john4"

    def test_email_with_dots_and_plus(self, app):
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("john.doe+tag@gmail.com")
            assert result == "johndoetag"

    def test_empty_prefix_fallback(self, app):
        # The bare 'user' fallback is itself a reserved name (issue #91), so
        # generate_unique_username must skip it and append a suffix.
        mock_user = self._make_mock_user([None, None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("@example.com")
            assert result == "user2"

    def test_brand_substring_email_falls_back_to_user(self, app):
        # 'myloore' contains the brand substring; appending digits never
        # escapes it, so the generator must fall back to the 'user' base
        # instead of looping forever.
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("myloore@example.com")
            assert result == "user2"

    def test_founder_prefix_email_falls_back_to_user(self, app):
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("hrosspetfan@example.com")
            assert result == "user2"

    def test_exact_reserved_email_keeps_base(self, app):
        # Exact-match reservations ARE escaped by a suffix, so the original
        # base is kept: admin -> admin2.
        mock_user = self._make_mock_user([None])
        with app.app_context(), \
                patch.dict("sys.modules", self._patched_modules(mock_user)):
            import backend.utils.magic_link as ml
            result = ml.generate_unique_username("admin@example.com")
            assert result == "admin2"
