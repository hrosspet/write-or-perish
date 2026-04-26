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
    USER_EXPORT_KNOWN_KEYS,
    USER_EXPORT_PATTERN,
    UserExportValidationError,
    parse_max_export_tokens,
    parse_placeholder_params,
    validate_user_export_placeholders,
    warn_unknown_user_export_keys,
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

    def test_whitespace_after_ampersand_recovered(self):
        """Regression: a stray space after `&` (e.g. when typed in voice
        mode or copied from a doc) used to produce a key of
        ' max_export_tokens' that the handler silently missed,
        sending the entire archive to the LLM. Whitespace in keys must
        be stripped."""
        params = parse_placeholder_params(
            "{user_export?keep=newest& max_export_tokens=10000}"
        )
        assert params == {"keep": "newest", "max_export_tokens": "10000"}

    def test_whitespace_around_values_stripped(self):
        params = parse_placeholder_params(
            "{user_export?keep= newest &max_export_tokens= 10000 }"
        )
        assert params == {"keep": "newest", "max_export_tokens": "10000"}


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


# ── warn_unknown_user_export_keys ───────────────────────────────────────

class TestWarnUnknownUserExportKeys:
    @pytest.fixture
    def real_log(self):
        log = logging.getLogger("test_warn_unknown_user_export_keys")
        log.propagate = True
        return log

    def test_known_only_no_warning(self, caplog, real_log):
        with caplog.at_level(logging.WARNING):
            unknown = warn_unknown_user_export_keys(
                {"keep": "newest", "max_export_tokens": "10000"},
                log=real_log,
            )
        assert unknown == set()
        assert not any(
            r.levelno == logging.WARNING for r in caplog.records
        )

    def test_unknown_key_warns(self, caplog, real_log):
        """Catches typos like `max-export-tokens` (hyphens) that
        whitespace-stripping wouldn't fix."""
        with caplog.at_level(logging.WARNING):
            unknown = warn_unknown_user_export_keys(
                {"keep": "newest", "max-export-tokens": "10000"},
                user_id=42,
                placeholder="{user_export?keep=newest&max-export-tokens=10000}",
                log=real_log,
            )
        assert unknown == {"max-export-tokens"}
        assert any(
            "Unrecognized {user_export} param key(s)" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_known_keys_constant(self):
        """Lock the set of recognized keys. Adding a new param means
        updating this constant deliberately."""
        assert USER_EXPORT_KNOWN_KEYS == frozenset(
            {"keep", "max_export_tokens"}
        )


# ── validate_user_export_placeholders ───────────────────────────────────

class TestValidateUserExportPlaceholders:
    """Validation runs upstream of LLM node creation. A misconfigured
    placeholder must raise so the request is aborted before any DB
    writes or LLM API spend."""

    def test_no_placeholder_no_op(self):
        validate_user_export_placeholders("just some text")
        validate_user_export_placeholders("")
        validate_user_export_placeholders(None)

    def test_valid_placeholder_passes(self):
        validate_user_export_placeholders(
            "What patterns? {user_export?keep=newest&max_export_tokens=10000}"
        )

    def test_unknown_key_raises(self):
        with pytest.raises(UserExportValidationError) as exc:
            validate_user_export_placeholders(
                "{user_export?keep=newest&max-tokens=10000}"
            )
        msg = str(exc.value)
        assert "max-tokens" in msg
        assert "Known keys" in msg

    def test_whitespace_typo_recovered_no_raise(self):
        """The whitespace-after-`&` case (the original bug) must
        NOT raise — parse_placeholder_params strips whitespace, so the
        keys are recognized and validation passes."""
        validate_user_export_placeholders(
            "{user_export?keep=newest& max_export_tokens=10000}"
        )

    def test_first_invalid_placeholder_raises(self):
        """When multiple placeholders are present, raise on the first
        invalid one — fail-fast."""
        with pytest.raises(UserExportValidationError):
            validate_user_export_placeholders(
                "first {user_export?keep=newest} "
                "second {user_export?bogus=1}"
            )


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

    def test_handler_warns_on_unknown_keys(self, llm_completion_ast):
        """The handler must call warn_unknown_user_export_keys to surface
        typos like `max-export-tokens` that whitespace-stripping won't
        catch and which would otherwise silently fall back to no cap
        (the bug that cost real $$$ in production)."""
        calls = list(self._calls_named(
            llm_completion_ast, "warn_unknown_user_export_keys"
        ))
        assert calls, (
            "Expected warn_unknown_user_export_keys(...) call in "
            "backend/tasks/llm_completion.py to catch typoed param keys"
        )
        # Must pass log=logger so warnings go to the Celery task logger.
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "log":
                    assert isinstance(kw.value, ast.Name)
                    assert kw.value.id == "logger"
                    return
        pytest.fail("warn_unknown_user_export_keys missing log=logger")

    def test_create_llm_placeholder_validates_upstream(self):
        """Validation must run in create_llm_placeholder BEFORE any DB
        writes. This is what prevents an LLM node from being created
        for a misconfigured placeholder. Source-level AST check on
        llm_nodes.py."""
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "utils", "llm_nodes.py",
        )
        with open(path) as f:
            tree = ast.parse(f.read())
        found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "validate_user_export_placeholders"):
                found = True
                break
        assert found, (
            "Expected validate_user_export_placeholders(...) call in "
            "backend/utils/llm_nodes.py so misconfigured placeholders "
            "abort BEFORE any LLM node is created"
        )
