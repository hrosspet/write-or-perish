"""Both providers over their streaming APIs (#366), collected to the final
response: mid-stream failures the SDKs do not retry are retried here, a
request-level error is not, and a cut-off reply counts as truncated."""
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
    monkeypatch.setattr(mod, "STREAM_RETRY_DELAYS", (0, 0))
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
    with pytest.raises(providers.ProviderUnavailableError) as exc_info:
        providers.LLMProvider._call_anthropic("m", MSGS, "k")
    assert len(calls) == 3
    # The node's error is shown to the user: readable, not the raw event
    # body; the provider's error stays on the chain for the logs.
    message = str(exc_info.value)
    assert "Anthropic" in message and "overloaded_error" not in message
    assert isinstance(exc_info.value.__cause__, anthropic.APIStatusError)


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


# ── OpenAI (Responses API events) ────────────────────────────────────────

def _event(type_, **fields):
    e = type("Event", (), {})()
    e.type = type_
    for k, v in fields.items():
        setattr(e, k, v)
    return e


def _oai_response(status="completed", reason=None, text="hi"):
    block = _event("output_text", text=text)
    usage = _event("usage", input_tokens=10, output_tokens=5,
                   total_tokens=15, input_tokens_details=None)
    r = _event("response", status=status, usage=usage, id="resp_1",
               output=[_event("message", content=[block])] if text else [])
    r.incomplete_details = _event("details", reason=reason) if reason else None
    return r


def _oai_failed(code):
    r = _oai_response()
    r.error = _event("error", code=code, message="boom")
    return _event("response.failed", response=r)


def _oai_client(scripts, calls):
    """OpenAI stand-in: each create(stream=True) call replays the next
    script — a list of events, where an Exception is raised mid-stream."""
    class _Stream:
        def __init__(self, items):
            self.items = items

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            for item in self.items:
                if isinstance(item, Exception):
                    raise item
                yield item

    class _Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["stream"] is True
            return _Stream(scripts.pop(0))

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.responses = _Responses()

    return _Client


def _oai_call(providers):
    return providers.LLMProvider._call_openai("gpt-x", MSGS, "k")


def test_openai_returns_the_terminal_events_response(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client([[
        _event("response.created"),
        _event("response.output_text.delta", delta="h"),
        _event("response.completed", response=_oai_response()),
    ]], calls))
    result = _oai_call(providers)
    assert result["content"] == "hi" and result["truncated"] is False
    assert result["response_id"] == "resp_1"


def test_openai_incomplete_at_max_output_is_truncated(providers, monkeypatch):
    monkeypatch.setattr(providers, "OpenAI", _oai_client([[
        _event("response.incomplete", response=_oai_response(
            status="incomplete", reason="max_output_tokens", text="")),
    ]], []))
    result = _oai_call(providers)
    assert result["content"] == "" and result["truncated"] is True


@pytest.mark.parametrize("failure", [
    _oai_failed("server_error"),
    _event("error", code="rate_limit_exceeded", message="slow down"),
    httpx.ReadError("connection reset"),
])
def test_openai_mid_stream_failure_is_retried(providers, monkeypatch,
                                              failure):
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client([
        [_event("response.created"), failure],
        [_event("response.completed", response=_oai_response())],
    ], calls))
    assert _oai_call(providers)["content"] == "hi"
    assert len(calls) == 2


def test_openai_exhausted_retries_raise_readable_error(providers,
                                                       monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client(
        [[_oai_failed("server_error")] for _ in range(3)], calls))
    with pytest.raises(providers.ProviderUnavailableError) as exc_info:
        _oai_call(providers)
    assert len(calls) == 3
    assert "OpenAI" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, providers.OpenAIStreamError)


@pytest.mark.parametrize("script", [
    [_event("response.completed", response=_oai_response())],
    [_oai_failed("invalid_prompt")],
])
def test_openai_http_client_is_closed(providers, monkeypatch, script):
    # We pass our own HTTP client (for keepalive), and the SDK closes only
    # the ones it creates — so the call must close it, on success or not.
    http_clients = []
    fake = _oai_client([script], [])

    class _Recording(fake):
        def __init__(self, api_key=None, **kwargs):
            super().__init__(api_key, **kwargs)
            http_clients.append(kwargs["http_client"])

    monkeypatch.setattr(providers, "OpenAI", _Recording)
    try:
        _oai_call(providers)
    except providers.OpenAIStreamError:
        pass
    assert len(http_clients) == 1 and http_clients[0].is_closed


def test_openai_failed_event_overflow_maps_to_prompt_too_long(providers,
                                                              monkeypatch):
    # A context overflow reported as a response.failed event, rather than
    # an error the SDK raises, must still reach the export-shrinking retry.
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client(
        [[_oai_failed("context_length_exceeded")]], calls))
    with pytest.raises(providers.PromptTooLongError):
        providers.LLMProvider._call_openai(
            "gpt-x", MSGS, "k", context_window=1_000)
    assert len(calls) == 1


def test_openai_non_transient_failure_is_not_retried(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client([
        [_oai_failed("invalid_prompt")],
        [_event("response.completed", response=_oai_response())],
    ], calls))
    with pytest.raises(providers.OpenAIStreamError):
        _oai_call(providers)
    assert len(calls) == 1


def test_openai_stream_without_terminal_event_fails(providers, monkeypatch):
    monkeypatch.setattr(providers, "OpenAI", _oai_client(
        [[_event("response.created")]], []))
    with pytest.raises(providers.OpenAIStreamError):
        _oai_call(providers)


def _oai_sdk_stream_error(code, message):
    """What the OpenAI SDK raises for a stream event whose data carries an
    ``error``: a plain APIError, no HTTP status."""
    import openai
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APIError(message, request,
                           body={"code": code, "message": message})


def test_openai_mid_stream_overflow_maps_to_prompt_too_long(providers,
                                                            monkeypatch):
    # Streamed, a context overflow arrives after the 200 instead of as a
    # 400; the export-shrinking retry depends on PromptTooLongError.
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client([[
        _oai_sdk_stream_error(
            "context_length_exceeded",
            "Your input exceeds the context window of this model."),
    ]], calls))
    with pytest.raises(providers.PromptTooLongError):
        providers.LLMProvider._call_openai(
            "gpt-x", MSGS, "k", context_window=1_000)
    assert len(calls) == 1


def test_openai_sdk_raised_transient_error_is_retried(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _oai_client([
        [_oai_sdk_stream_error("server_error", "try again")],
        [_event("response.completed", response=_oai_response())],
    ], calls))
    assert _oai_call(providers)["content"] == "hi"
    assert len(calls) == 2
