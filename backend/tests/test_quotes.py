"""Tests for quote resolution utilities."""

import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Mock flask_login before any backend imports
mock_flask_login = MagicMock()
sys.modules['flask_login'] = mock_flask_login

# Mock celery logger
mock_celery = MagicMock()
sys.modules['celery'] = mock_celery
sys.modules['celery.utils'] = MagicMock()
sys.modules['celery.utils.log'] = MagicMock()

# Mock backend.models for tests that need Node
mock_models = MagicMock()
sys.modules['backend.models'] = mock_models

from backend.utils.quotes import (  # noqa: E402
    find_quote_ids,
    has_quotes,
    resolve_quotes,
    ExportQuoteResolver,
    resolve_quotes_for_export,
)


class TestFindQuoteIds:
    """Test quote ID extraction from content."""

    def test_no_quotes(self):
        """Test content with no quotes returns empty list."""
        assert find_quote_ids("Hello world") == []
        assert find_quote_ids("") == []
        assert find_quote_ids(None) == []

    def test_single_quote(self):
        """Test extracting a single quote ID."""
        assert find_quote_ids("Check this: {quote:123}") == [123]

    def test_multiple_quotes(self):
        """Test extracting multiple quote IDs."""
        content = "See {quote:1} and {quote:2} and {quote:3}"
        assert find_quote_ids(content) == [1, 2, 3]

    def test_duplicate_quotes(self):
        """Test that duplicate quote IDs are all returned."""
        content = "{quote:5} repeated {quote:5}"
        assert find_quote_ids(content) == [5, 5]

    def test_quote_in_text(self):
        """Test quote embedded in regular text."""
        content = "As I mentioned in {quote:42}, this is important."
        assert find_quote_ids(content) == [42]


class TestHasQuotes:
    """Test quote detection."""

    def test_has_quotes_true(self):
        """Test detecting quotes in content."""
        assert has_quotes("{quote:1}") is True
        assert has_quotes("text {quote:99} more text") is True

    def test_has_quotes_false(self):
        """Test content without quotes."""
        assert has_quotes("no quotes here") is False
        assert has_quotes("") is False
        assert has_quotes(None) is False

    def test_invalid_quote_format(self):
        """Test that invalid formats are not detected as quotes."""
        assert has_quotes("{quote:}") is False  # No ID
        assert has_quotes("{quote:abc}") is False  # Non-numeric
        assert has_quotes("quote:123") is False  # Missing braces


class TestResolveQuotes:
    """Test direct quote resolution with depth support."""

    @patch('backend.utils.quotes.get_quote_data')
    def test_no_quotes_returns_unchanged(self, mock_get_quote_data):
        """Test that content without quotes is returned unchanged."""
        content = "Hello world"
        result, resolved_ids = resolve_quotes(content, user_id=1)
        assert result == content
        assert resolved_ids == []
        mock_get_quote_data.assert_not_called()

    @patch('backend.utils.quotes.get_quote_data')
    def test_single_quote_resolution(self, mock_get_quote_data):
        """Test resolving a single quote."""
        mock_get_quote_data.return_value = {
            10: {
                "id": 10,
                "content": "Quoted content here",
                "username": "alice",
                "user_id": 2,
            }
        }

        content = "See {quote:10} for details"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True)

        assert 10 in resolved_ids
        assert "Quoted content here" in result
        assert 'id="10"' in result
        assert 'author="alice"' in result

    @patch('backend.utils.quotes.get_quote_data')
    def test_inaccessible_quote(self, mock_get_quote_data):
        """Test handling of inaccessible quoted node."""
        mock_get_quote_data.return_value = {10: None}  # Not accessible

        content = "See {quote:10}"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True)

        assert "not accessible" in result
        assert 10 not in resolved_ids

    @patch('backend.utils.quotes.get_quote_data')
    def test_recursive_resolution_depth_2(self, mock_get_quote_data):
        """Test recursive quote resolution at depth 2."""
        # Node 10 quotes Node 20
        mock_get_quote_data.side_effect = [
            {10: {"id": 10, "content": "A quotes {quote:20}", "username": "alice", "user_id": 1}},
            {20: {"id": 20, "content": "B's content", "username": "bob", "user_id": 2}},
        ]

        content = "Start {quote:10} end"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True, max_depth=3)

        assert 10 in resolved_ids
        assert 20 in resolved_ids
        assert "A quotes" in result
        assert "B's content" in result

    @patch('backend.utils.quotes.get_quote_data')
    def test_max_depth_limits_recursion(self, mock_get_quote_data):
        """Test that max_depth=1 prevents recursive resolution."""
        mock_get_quote_data.return_value = {
            10: {"id": 10, "content": "A quotes {quote:20}", "username": "alice", "user_id": 1}
        }

        content = "Start {quote:10} end"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True, max_depth=1)

        # Only node 10 should be resolved, {quote:20} should remain as placeholder
        assert 10 in resolved_ids
        assert 20 not in resolved_ids
        assert "{quote:20}" in result

    @patch('backend.utils.quotes.get_quote_data')
    def test_cycle_detection(self, mock_get_quote_data):
        """Test that circular references are detected and handled."""
        # Node 10 quotes Node 20, Node 20 quotes Node 10 (cycle)
        def mock_data(ids, user_id):
            data = {
                10: {"id": 10, "content": "A quotes {quote:20}", "username": "alice", "user_id": 1},
                20: {"id": 20, "content": "B quotes {quote:10}", "username": "bob", "user_id": 2},
            }
            return {id: data.get(id) for id in ids}

        mock_get_quote_data.side_effect = mock_data

        content = "Start {quote:10} end"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True, max_depth=5)

        # Should detect cycle and not infinite loop
        assert "circular reference" in result.lower()

    @patch('backend.utils.quotes.get_quote_data')
    def test_human_readable_format(self, mock_get_quote_data):
        """Test human-readable format (for_llm=False)."""
        mock_get_quote_data.return_value = {
            10: {"id": 10, "content": "Quoted text", "username": "alice", "user_id": 1}
        }

        content = "See {quote:10}"
        result, _ = resolve_quotes(content, user_id=1, for_llm=False)

        assert "--- Quoted from @alice" in result
        assert "node #10" in result
        assert "--- End quote ---" in result


class TestExportQuoteResolver:
    """Test the ExportQuoteResolver for truncated exports."""

    def _create_mock_node(self, node_id, content, created_at, username="testuser"):
        """Helper to create a mock node."""
        node = MagicMock()
        node.id = node_id
        node.get_content.return_value = content
        node.created_at = created_at
        node.user = MagicMock()
        node.user.username = username
        return node

    def test_basic_truncation_without_quotes(self):
        """Test that truncation works correctly without quotes."""
        resolver = ExportQuoteResolver(user_id=1, max_tokens=100)

        now = datetime.utcnow()
        # Add nodes: newest first (will be sorted by resolver)
        # Each node ~25 tokens (100 chars / 4)
        resolver.add_node(1, now, "A" * 100)  # 25 tokens
        resolver.add_node(2, now - timedelta(hours=1), "B" * 100)  # 25 tokens
        resolver.add_node(3, now - timedelta(hours=2), "C" * 100)  # 25 tokens
        resolver.add_node(4, now - timedelta(hours=3), "D" * 100)  # 25 tokens
        resolver.add_node(5, now - timedelta(hours=4), "E" * 100)  # 25 tokens

        resolver.resolve()
        included_ids, _ = resolver.get_resolution_result()

        # Should include ~4 nodes (100 tokens budget, 25 each)
        assert len(included_ids) == 4
        # Newest nodes should be included
        assert 1 in included_ids
        assert 2 in included_ids
        assert 3 in included_ids
        assert 4 in included_ids
        # Oldest node should be excluded
        assert 5 not in included_ids

    def test_quote_resolved_by_reference(self):
        """Test that quotes are resolved by reference when quoted node is in export."""
        resolver = ExportQuoteResolver(user_id=1, max_tokens=200)

        now = datetime.utcnow()
        # Node 1 (newest) quotes Node 2, both fit in budget
        resolver.add_node(1, now, "A says {quote:2}")  # ~6 tokens
        resolver.add_node(2, now - timedelta(hours=1), "B content")  # ~3 tokens

        resolver.resolve()
        included_ids, embedded_quotes = resolver.get_resolution_result()

        # Both nodes should be included
        assert 1 in included_ids
        assert 2 in included_ids
        # No embedding needed - node 2 is already in export
        assert 1 not in embedded_quotes or 2 not in embedded_quotes.get(1, {})

    def test_quote_embedded_when_not_in_export(self):
        """Test that quotes are embedded when quoted node is truncated out."""
        resolver = ExportQuoteResolver(user_id=1, max_tokens=50)

        now = datetime.utcnow()
        # Node 1 (newest) quotes Node 3 (oldest, will be truncated)
        resolver.add_node(1, now, "A says {quote:3}")  # ~6 tokens
        resolver.add_node(2, now - timedelta(hours=1), "B" * 100)  # ~25 tokens
        resolver.add_node(3, now - timedelta(hours=2), "C" * 100)  # ~25 tokens (truncated)

        # Mock _get_node_metadata to return data for node 3
        def mock_get_metadata(node_id):
            # Use cached data first (nodes we added)
            if node_id in resolver._node_cache:
                return resolver._node_cache[node_id]
            # For external nodes, return mock data
            if node_id == 3:
                return {
                    'tokens': 10,
                    'quote_ids': [],
                    'content': 'Old content',
                    'username': 'olduser'
                }
            return None

        resolver._get_node_metadata = mock_get_metadata

        resolver.resolve()
        included_ids, embedded_quotes = resolver.get_resolution_result()

        # Node 1 and 2 should be included initially
        assert 1 in included_ids
        # Node 3 was truncated but then its content was embedded into node 1
        # So node 3's ID should now be in included_ids (content is present via embedding)
        assert 3 in included_ids
        # Embedding should be recorded
        assert 1 in embedded_quotes
        assert 3 in embedded_quotes[1]

    def test_nested_quotes_in_export(self):
        """Test that nested quotes are handled correctly."""
        resolver = ExportQuoteResolver(user_id=1, max_tokens=50)

        now = datetime.utcnow()
        # Only node 1 fits initially, quotes node 2 which quotes node 3
        resolver.add_node(1, now, "A says {quote:2}")  # ~6 tokens

        # Mock _get_node_metadata to return data for nodes 2 and 3
        def mock_get_metadata(node_id):
            if node_id in resolver._node_cache:
                return resolver._node_cache[node_id]
            if node_id == 2:
                return {
                    'tokens': 15,
                    'quote_ids': [3],  # Node 2 quotes Node 3
                    'content': 'B quotes {quote:3}',
                    'username': 'user2'
                }
            elif node_id == 3:
                return {
                    'tokens': 10,
                    'quote_ids': [],
                    'content': 'C final content',
                    'username': 'user3'
                }
            return None

        resolver._get_node_metadata = mock_get_metadata

        resolver.resolve()
        included_ids, embedded_quotes = resolver.get_resolution_result()

        # All three nodes should be marked as included (content present)
        assert 1 in included_ids
        assert 2 in included_ids  # Embedded into node 1
        assert 3 in included_ids  # Inherited from node 2's quotes, also embedded

    def test_empty_resolver(self):
        """Test resolver with no nodes."""
        resolver = ExportQuoteResolver(user_id=1, max_tokens=100)
        resolver.resolve()
        included_ids, embedded_quotes = resolver.get_resolution_result()

        assert included_ids == set()
        assert embedded_quotes == {}


class TestResolveQuotesForExport:
    """Test the final export rendering function."""

    def setup_method(self):
        """Set up mock Node for each test."""
        # Configure the mock Node in backend.models
        self.mock_node = MagicMock()
        self.mock_node.user.username = "alice"
        mock_models.Node.query.get.return_value = self.mock_node

    def test_no_quotes_unchanged(self):
        """Test that content without quotes is unchanged."""
        content = "No quotes here"
        result = resolve_quotes_for_export(content, node_id=1, embedded_quotes={}, user_id=1)
        assert result == content

    def test_embedded_quote_rendered(self):
        """Test that embedded quotes are rendered with content."""
        embedded_quotes = {
            1: {10: "This is the quoted content"}
        }

        content = "See {quote:10} for details"
        result = resolve_quotes_for_export(content, node_id=1, embedded_quotes=embedded_quotes, user_id=1)

        assert "This is the quoted content" in result
        assert "--- Quoted from @alice" in result
        assert "node #10" in result

    def test_reference_quote_rendered(self):
        """Test that non-embedded quotes show reference marker."""
        content = "See {quote:10}"
        result = resolve_quotes_for_export(content, node_id=1, embedded_quotes={}, user_id=1)

        assert "[See node #10 in export]" in result

    def test_nested_embedded_quotes(self):
        """Test that nested quotes in embedded content are also resolved."""
        # Node 1 embeds Node 10, which has a quote of Node 20 (also embedded in node 1)
        embedded_quotes = {
            1: {
                10: "A quotes {quote:20}",
                20: "B final content"
            }
        }

        content = "Start {quote:10} end"
        result = resolve_quotes_for_export(content, node_id=1, embedded_quotes=embedded_quotes, user_id=1)

        assert "A quotes" in result
        assert "B final content" in result

    def test_circular_reference_in_rendering(self):
        """Test that circular references in embedded quotes are handled."""
        # Circular: Node 1 embeds Node 10 which quotes Node 10
        embedded_quotes = {
            1: {
                10: "A quotes {quote:10}"  # Self-reference
            }
        }

        content = "Start {quote:10} end"
        result = resolve_quotes_for_export(content, node_id=1, embedded_quotes=embedded_quotes, user_id=1)

        # Should not infinite loop, should show circular reference message
        assert "Circular reference" in result or "A quotes" in result


class TestIntegration:
    """Integration tests combining multiple components."""

    @patch('backend.utils.quotes.get_quote_data')
    def test_depth_3_chain(self, mock_get_quote_data):
        """Test a chain of 3 nested quotes (A -> B -> C -> D)."""
        def mock_data(ids, user_id):
            data = {
                1: {"id": 1, "content": "A quotes {quote:2}", "username": "a", "user_id": 1},
                2: {"id": 2, "content": "B quotes {quote:3}", "username": "b", "user_id": 1},
                3: {"id": 3, "content": "C quotes {quote:4}", "username": "c", "user_id": 1},
                4: {"id": 4, "content": "D final", "username": "d", "user_id": 1},
            }
            return {id: data.get(id) for id in ids}

        mock_get_quote_data.side_effect = mock_data

        # With depth 3, should resolve A -> B -> C, but not D
        content = "{quote:1}"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True, max_depth=3)

        assert 1 in resolved_ids
        assert 2 in resolved_ids
        assert 3 in resolved_ids
        # Node 4 should NOT be resolved (depth limit reached)
        assert 4 not in resolved_ids
        assert "{quote:4}" in result  # Placeholder remains

    @patch('backend.utils.quotes.get_quote_data')
    def test_depth_4_chain(self, mock_get_quote_data):
        """Test that depth 4 resolves the full chain."""
        def mock_data(ids, user_id):
            data = {
                1: {"id": 1, "content": "A quotes {quote:2}", "username": "a", "user_id": 1},
                2: {"id": 2, "content": "B quotes {quote:3}", "username": "b", "user_id": 1},
                3: {"id": 3, "content": "C quotes {quote:4}", "username": "c", "user_id": 1},
                4: {"id": 4, "content": "D final", "username": "d", "user_id": 1},
            }
            return {id: data.get(id) for id in ids}

        mock_get_quote_data.side_effect = mock_data

        content = "{quote:1}"
        result, resolved_ids = resolve_quotes(content, user_id=1, for_llm=True, max_depth=4)

        assert 1 in resolved_ids
        assert 2 in resolved_ids
        assert 3 in resolved_ids
        assert 4 in resolved_ids
        assert "D final" in result
        assert "{quote:" not in result  # All placeholders resolved
