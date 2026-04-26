"""Tests for {user_export} placeholder helpers.

Covers:
- parse_placeholder_params: URL-style param extraction.
- parse_max_export_tokens: int conversion + structured-warning behavior on
  non-numeric / negative values.
- USER_EXPORT_PATTERN: regex matches new modifier forms.
"""

import logging

import pytest

from backend.utils.placeholders import (
    USER_EXPORT_PATTERN,
    parse_max_export_tokens,
    parse_placeholder_params,
)


# ── parse_placeholder_params ────────────────────────────────────────────

class TestParsePlaceholderParams:
    def test_no_params(self):
        assert parse_placeholder_params("{user_export}") == {}

    def test_keep_only(self):
        assert parse_placeholder_params(
            "{user_export?keep=oldest}"
        ) == {"keep": "oldest"}

    def test_max_export_tokens_only(self):
        assert parse_placeholder_params(
            "{user_export?max_export_tokens=100000}"
        ) == {"max_export_tokens": "100000"}

    def test_keep_and_max_export_tokens_combined(self):
        params = parse_placeholder_params(
            "{user_export?keep=oldest&max_export_tokens=50000}"
        )
        assert params == {"keep": "oldest", "max_export_tokens": "50000"}


# ── parse_max_export_tokens ─────────────────────────────────────────────

class TestParseMaxExportTokens:
    @pytest.fixture
    def real_log(self):
        log = logging.getLogger("test_parse_max_export_tokens")
        log.propagate = True
        return log

    def test_none_returns_none(self):
        assert parse_max_export_tokens(None) is None

    def test_valid_numeric_string(self):
        assert parse_max_export_tokens("100000") == 100000

    def test_zero_returns_zero(self):
        # 0 is a valid value — caller short-circuits the export when it sees
        # 0, mirroring today's `max_export_tokens != 0` guard.
        assert parse_max_export_tokens("0") == 0

    def test_non_numeric_falls_back_with_warning(self, caplog, real_log):
        with caplog.at_level(logging.WARNING):
            result = parse_max_export_tokens(
                "abc",
                user_id=42,
                placeholder="{user_export?max_export_tokens=abc}",
                log=real_log,
            )
        assert result is None
        assert any(
            "non-numeric max_export_tokens" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_negative_falls_back_with_warning(self, caplog, real_log):
        with caplog.at_level(logging.WARNING):
            result = parse_max_export_tokens(
                "-100",
                user_id=42,
                placeholder="{user_export?max_export_tokens=-100}",
                log=real_log,
            )
        assert result is None
        assert any(
            "negative max_export_tokens" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_warning_includes_user_id_and_placeholder(
        self, caplog, real_log
    ):
        with caplog.at_level(logging.WARNING):
            parse_max_export_tokens(
                "abc",
                user_id=99,
                placeholder="{user_export?max_export_tokens=abc}",
                log=real_log,
            )
        record = next(
            r for r in caplog.records
            if "non-numeric max_export_tokens" in r.getMessage()
        )
        # Structured args (user_id, raw, placeholder)
        assert 99 in record.args
        assert "abc" in record.args
        assert "{user_export?max_export_tokens=abc}" in record.args


# ── regex compatibility ─────────────────────────────────────────────────

class TestUserExportPattern:
    def test_matches_bare(self):
        assert USER_EXPORT_PATTERN.search("hello {user_export} world")

    def test_matches_with_keep(self):
        m = USER_EXPORT_PATTERN.search("{user_export?keep=oldest}")
        assert m is not None
        assert m.group(0) == "{user_export?keep=oldest}"

    def test_matches_with_max_export_tokens(self):
        m = USER_EXPORT_PATTERN.search(
            "{user_export?max_export_tokens=100000}"
        )
        assert m is not None
        assert m.group(0) == "{user_export?max_export_tokens=100000}"

    def test_matches_combined(self):
        m = USER_EXPORT_PATTERN.search(
            "{user_export?keep=newest&max_export_tokens=50000}"
        )
        assert m is not None
        assert m.group(0) == (
            "{user_export?keep=newest&max_export_tokens=50000}"
        )
