"""Tests for reserved/protected username matching (issue #91).

``is_username_reserved`` is a pure function (no DB), so it is tested directly.
``validate_username``'s format/reserved checks also short-circuit before any
DB access; the uniqueness path performs a deferred import of User/db, which we
mock following the pattern used in test_magic_link.py.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from backend.utils.reserved_usernames import (
    derive_available_username,
    is_username_reserved,
    validate_username,
    _normalize,
)


class TestNormalize:
    def test_lowercases_and_strips_non_alnum(self):
        assert _normalize("Pe.ter_X!") == "peterx"

    def test_empty(self):
        assert _normalize("") == ""
        assert _normalize(None) == ""


class TestIsReservedExact:
    @pytest.mark.parametrize("name", [
        "admin", "administrator", "system", "support", "official",
        "moderator", "dashboard", "api", "lore", "lor", "root", "owner",
    ])
    def test_exact_reserved(self, name):
        assert is_username_reserved(name) is True

    def test_exact_case_insensitive(self):
        assert is_username_reserved("ADMIN") is True
        assert is_username_reserved("Admin") is True

    def test_exact_with_punctuation_normalizes(self):
        # 'a.d.m.i.n' normalizes to 'admin'
        assert is_username_reserved("a.d.m.i.n") is True


class TestIsReservedBrandSubstring:
    @pytest.mark.parametrize("name", [
        "loore", "LOORE", "Loore", "myloore", "loore123",
        # Any number of repeated letters (o's >= 2) -- #175 follow-up
        "looore", "loooooore", "looree", "loooreee", "LoOoRe",
        "my_loooree_123", "lloore", "loorre", "lloorree", "llooorrreee",
    ])
    def test_brand_substring_blocked(self, name):
        assert is_username_reserved(name) is True

    @pytest.mark.parametrize("name", ["loor", "looor"])
    def test_no_trailing_e_not_brand(self, name):
        # Without the final 'e' it isn't the brand name. 'loor'/'looor' are
        # not exact-reserved either, so they remain available.
        assert is_username_reserved(name) is False


class TestIsReservedFounderPrefix:
    @pytest.mark.parametrize("name", ["hrosspet", "HRosspet", "hrosspetx", "hrosspet_official"])
    def test_founder_prefix_blocked(self, name):
        assert is_username_reserved(name) is True


class TestNotReserved:
    @pytest.mark.parametrize("name", [
        "explore",     # contains 'lore' but not exact, not 'loore'
        "folklore",    # contains 'lore' but not exact
        "alice",
        "bob123",
        "writer",
        "peter",       # common first names are deliberately not founder-blocked
        "peta",
        "petersen_is_not_me",
    ])
    def test_allowed(self, name):
        assert is_username_reserved(name) is False

    def test_explore_explicitly_allowed(self):
        # Critical: the short exact tokens 'lore'/'lor' must not block 'explore'.
        assert is_username_reserved("explore") is False

    def test_empty_not_reserved(self):
        assert is_username_reserved("") is False


class TestValidateUsernamePureChecks:
    """These checks short-circuit before any DB access."""

    def test_empty(self):
        assert validate_username("") == "Username cannot be empty."

    def test_too_long(self):
        assert validate_username("a" * 65) == "Username must be 64 characters or fewer."

    def test_bad_chars(self):
        err = validate_username("bad name!")
        assert err == "Username may only contain letters, numbers, and underscores."

    @pytest.mark.parametrize("name", ["admin", "LOORE", "Loore", "myloore", "loore123", "hrosspetx"])
    def test_reserved(self, name):
        assert validate_username(name) == "That username is reserved."


class TestValidateUsernameUniqueness:
    """The uniqueness path mocks the deferred User/db imports."""

    def _patch_modules(self, exists):
        mock_user = MagicMock()
        mock_user.query.filter.return_value.filter.return_value.first.return_value = (
            MagicMock() if exists else None
        )
        # When exclude_user_id is None, only one .filter() is chained.
        mock_user.query.filter.return_value.first.return_value = (
            MagicMock() if exists else None
        )
        mock_db = MagicMock()
        return {
            "backend.models": MagicMock(User=mock_user),
            "backend.extensions": MagicMock(db=mock_db),
        }

    def test_valid_unique_allowed(self):
        with patch.dict(sys.modules, self._patch_modules(exists=False)):
            assert validate_username("explore") is None

    def test_duplicate_rejected(self):
        with patch.dict(sys.modules, self._patch_modules(exists=True)):
            assert validate_username("explore") == "That username is already taken."

    def test_valid_unique_with_exclude(self):
        with patch.dict(sys.modules, self._patch_modules(exists=False)):
            assert validate_username("alice", exclude_user_id=42) is None


class TestDeriveAvailableUsername:
    """Shared fallback generator used by magic-link and Twitter OAuth signup.

    Mocks the deferred User/db imports; ``side_effect`` is the sequence of
    .first() results, one per uniqueness DB query (reserved candidates are
    rejected before any query).
    """

    def _patch_modules(self, side_effect):
        mock_user = MagicMock()
        mock_user.query.filter.return_value.first.side_effect = side_effect
        return {
            "backend.models": MagicMock(User=mock_user),
            "backend.extensions": MagicMock(),
        }

    def test_available_base_kept(self):
        with patch.dict(sys.modules, self._patch_modules([None])):
            assert derive_available_username("alice") == "alice"

    def test_taken_base_gets_suffix(self):
        with patch.dict(sys.modules, self._patch_modules([MagicMock(), None])):
            assert derive_available_username("alice") == "alice2"

    def test_exact_reserved_escapes_with_suffix(self):
        # 'admin' is rejected without a DB query; 'admin2' is queried once.
        with patch.dict(sys.modules, self._patch_modules([None])):
            assert derive_available_username("admin") == "admin2"

    def test_brand_substring_falls_back_to_user(self):
        # Digits never escape a substring match, so the base flips to 'user'
        # ('user' itself is exact-reserved, hence 'user2').
        with patch.dict(sys.modules, self._patch_modules([None])):
            assert derive_available_username("myloore") == "user2"

    def test_founder_prefix_falls_back_to_user(self):
        with patch.dict(sys.modules, self._patch_modules([None])):
            assert derive_available_username("hrosspetfan") == "user2"

    def test_empty_base_falls_back_to_user(self):
        with patch.dict(sys.modules, self._patch_modules([None])):
            assert derive_available_username("") == "user2"
