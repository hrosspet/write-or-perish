"""The llm-stream SSE's snapshot/delta decision (#367)."""
from backend.routes.sse import text_stream_events


def test_first_event_is_a_snapshot_once_there_is_text():
    assert text_stream_events(None, "") == ([], "")
    assert text_stream_events(None, "Hi") == (
        [("snapshot", {"text": "Hi"})], "Hi")


def test_growth_is_sent_as_a_delta():
    assert text_stream_events("Hi", "Hi there") == (
        [("delta", {"text": " there"})], "Hi there")
    assert text_stream_events("Hi", "Hi") == ([], "Hi")


def test_rewritten_text_is_a_new_snapshot():
    # The model call restarted: the text is no longer an extension.
    assert text_stream_events("Half a thou", "Full") == (
        [("snapshot", {"text": "Full"})], "Full")
    assert text_stream_events("Half", "") == (
        [("snapshot", {"text": ""})], "")
