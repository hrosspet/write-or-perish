"""Tests for {user_export} placeholder helpers.

Covers:
- parse_placeholder_params: URL-style param extraction.
- parse_max_export_tokens: int conversion + structured-warning behavior on
  non-numeric / negative values.
- USER_EXPORT_PATTERN: regex matches new modifier forms.
- Wiring regression: the placeholder handler in llm_completion.py
  passes include_strategy="engaged_threads".
"""

import ast
import logging
import os

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


# ── wiring regression ──────────────────────────────────────────────────

class TestPlaceholderHandlerWiring:
    """Regression guards for the one-line wiring in llm_completion.py.

    We parse the source file with `ast` (rather than importing the
    module, whose Celery + LLM-providers chain is too heavy and conflicts
    with sys.modules mocks in other test files) and scan for the
    relevant Call nodes.
    """

    @pytest.fixture(scope="class")
    def llm_completion_ast(self):
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "tasks", "llm_completion.py",
        )
        with open(path) as f:
            return ast.parse(f.read())

    @staticmethod
    def _calls_named(tree, name):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            actual = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            if actual == name:
                yield node

    @staticmethod
    def _kwarg_constant(call, name):
        for kw in call.keywords:
            if kw.arg == name and isinstance(kw.value, ast.Constant):
                return kw.value.value
        return None

    def test_placeholder_handler_passes_engaged_threads(
        self, llm_completion_ast
    ):
        """The {user_export} handler must pass
        include_strategy='engaged_threads' to build_user_export_content.
        Catches accidental removal of the literal arg."""
        engaged_calls = [
            c for c in self._calls_named(
                llm_completion_ast, "build_user_export_content"
            )
            if self._kwarg_constant(c, "include_strategy")
            == "engaged_threads"
        ]
        assert engaged_calls, (
            "Expected at least one call to build_user_export_content "
            "with include_strategy='engaged_threads' in "
            "backend/tasks/llm_completion.py"
        )

    def test_placeholder_handler_threads_max_export_tokens(
        self, llm_completion_ast
    ):
        """The handler must thread the parsed max_export_tokens into
        build_user_export_content via the max_tokens kwarg. Verifies the
        call uses the local `max_export_tokens` variable (not a literal,
        not None)."""
        for call in self._calls_named(
            llm_completion_ast, "build_user_export_content"
        ):
            if (
                self._kwarg_constant(call, "include_strategy")
                != "engaged_threads"
            ):
                continue
            for kw in call.keywords:
                if kw.arg == "max_tokens":
                    assert isinstance(kw.value, ast.Name), (
                        "max_tokens should be passed as a variable "
                        "(the parsed budget), not a literal"
                    )
                    assert kw.value.id == "max_export_tokens"
                    return
            pytest.fail(
                "build_user_export_content call missing max_tokens kwarg"
            )
        pytest.fail("No engaged_threads call found")

    def test_parse_max_export_tokens_called_with_logger(
        self, llm_completion_ast
    ):
        """The handler must pass log=logger to parse_max_export_tokens
        so warnings go to the Celery task logger, not the helper's
        default stdlib logger."""
        for call in self._calls_named(
            llm_completion_ast, "parse_max_export_tokens"
        ):
            for kw in call.keywords:
                if kw.arg == "log":
                    assert isinstance(kw.value, ast.Name)
                    assert kw.value.id == "logger"
                    return
        pytest.fail(
            "Expected parse_max_export_tokens(..., log=logger) call in "
            "backend/tasks/llm_completion.py"
        )
