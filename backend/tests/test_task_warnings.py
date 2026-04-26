"""Tests for backend.utils.task_warnings.

The helpers operate on Node-shaped objects with a `llm_task_warnings`
text attribute. We use a tiny stand-in class instead of spinning up the
full SQLAlchemy stack — the helper is pure Python over JSON.
"""

import pytest

from backend.utils.task_warnings import (
    load_task_warnings,
    record_task_warning,
)


class _FakeNode:
    """Bag of attributes mirroring Node.llm_task_warnings."""
    def __init__(self, llm_task_warnings=None):
        self.llm_task_warnings = llm_task_warnings


# ── record_task_warning ─────────────────────────────────────────────────

class TestRecordTaskWarning:
    def test_first_warning_initializes_list(self):
        node = _FakeNode()
        record_task_warning(node, "first")
        assert node.llm_task_warnings == '["first"]'

    def test_appends_to_existing_list(self):
        node = _FakeNode('["first"]')
        record_task_warning(node, "second")
        assert load_task_warnings(node) == ["first", "second"]

    def test_malformed_json_resets(self):
        node = _FakeNode("not-json")
        record_task_warning(node, "fresh")
        assert load_task_warnings(node) == ["fresh"]

    def test_non_list_json_resets(self):
        node = _FakeNode('{"oops": "object"}')
        record_task_warning(node, "fresh")
        assert load_task_warnings(node) == ["fresh"]


# ── load_task_warnings ──────────────────────────────────────────────────

class TestLoadTaskWarnings:
    def test_none_returns_empty(self):
        assert load_task_warnings(_FakeNode(None)) == []

    def test_empty_string_returns_empty(self):
        assert load_task_warnings(_FakeNode("")) == []

    def test_returns_parsed_list(self):
        assert load_task_warnings(_FakeNode('["a", "b"]')) == ["a", "b"]

    def test_malformed_json_returns_empty(self):
        assert load_task_warnings(_FakeNode("not-json")) == []

    def test_non_list_returns_empty(self):
        assert load_task_warnings(_FakeNode('"a string"')) == []
        assert load_task_warnings(_FakeNode('{"k": "v"}')) == []
