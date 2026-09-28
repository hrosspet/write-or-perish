"""_call_anthropic over the streaming API (#366): mid-stream failures the
SDK does not retry are retried here, a request-level error is not, and a
context-window stop counts as truncated."""
import sys

import anthropic
import httpx
import pytest


@pytest.fixture
def providers(monkeypatch):
    # Import the real provider module fresh (it may be mocked globally).
    # monkeypatch restores both the sys.modules entry and the package
    # attribute the import rebinds; leaving either pointing at the fresh
    # module breaks sibling tests that patch the original.
    import backend
    monkeypatch.setattr(backend, "llm_providers",
                        getattr(backend, "llm_providers", None),
                        raising=False)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers as mod
    monkeypatch.setattr(mod, "ANTHROPIC_STREAM_RETRY_DELAYS", (0, 0))
    return mod


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Text:
    type = "text"
    text = "hi"


def _message(stop_reason="end_turn", content=None):
    class _Msg:
        pass
    m = _Msg()
    m.content = [_Text()] if content is None else content
    m.usage = _Usage()
    m.stop_reason = stop_reason
    return m


def _status_error(status, body):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError(str(body), response=response, body=body)


def _client(outcomes, calls):
    """Anthropic stand-in whose stream() yields each outcome in turn: an
    exception is raised from get_final_message, a message is returned."""
    class _Stream:
        def __init__(self, outcome):
            self.outcome = outcome

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class _Messages:
        def stream(self, **kwargs):
            calls.append(kwargs)
            return _Stream(outcomes.pop(0))

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    return _Client


MSGS = [{"role": "user", "content": "hello"}]
OVERLOADED = {"type": "error",
              "error": {"type": "overloaded_error", "message": "Overloaded"}}


def test_mid_stream_overload_is_retried(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "Anthropic", _client(
        [_status_error(200, OVERLOADED), _message()], calls))
    result = providers.LLMProvider._call_anthropic("m", MSGS, "k")
    assert len(calls) == 2
    assert result["content"] == "hi"


def test_dropped_connection_is_retried(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "Anthropic", _client(
        [httpx.RemoteProtocolError("peer closed"), _message()], calls))
    assert providers.LLMProvider._call_anthropic(
        "m", MSGS, "k")["content"] == "hi"
    assert len(calls) == 2


def test_retries_are_bounded(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "Anthropic", _client(
        [_status_error(200, OVERLOADED) for _ in range(3)], calls))
    with pytest.raises(anthropic.APIStatusError):
        providers.LLMProvider._call_anthropic("m", MSGS, "k")
    assert len(calls) == 3


def test_request_level_error_is_not_retried_here(providers, monkeypatch):
    # A 529 on the initial request was already retried by the SDK.
    calls = []
    monkeypatch.setattr(providers, "Anthropic", _client(
        [_status_error(529, OVERLOADED), _message()], calls))
    with pytest.raises(anthropic.APIStatusError):
        providers.LLMProvider._call_anthropic("m", MSGS, "k")
    assert len(calls) == 1


@pytest.mark.parametrize("stop_reason,truncated", [
    ("max_tokens", True),
    ("model_context_window_exceeded", True),
    ("end_turn", False),
])
def test_truncated_stop_reasons(providers, monkeypatch, stop_reason,
                                truncated):
    monkeypatch.setattr(providers, "Anthropic", _client(
        [_message(stop_reason, content=[])], []))
    result = providers.LLMProvider._call_anthropic("m", MSGS, "k")
    assert result["truncated"] is truncated
