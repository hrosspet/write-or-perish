"""One-shot requests ask OpenAI for no prompt cache (#803).

- A one-shot request on a GPT-5.6+ model (flagged ``cache_diagnostics``)
  carries ``prompt_cache_options: {"mode": "explicit"}``, live and in a
  batch, on both OpenAI endpoints; an older OpenAI model and Anthropic
  never get it.
- The requests that read their own cache (conversation turns and their
  tool rounds, Glean, the todo merge, the voice pre-warm) and every
  Anthropic request are byte for byte what they were before #803: the
  bodies below were recorded from main before the change. The prompt
  cache depends on byte-identical prefixes, so an extra field there would
  be a regression.
- A usage without cache-write tokens is priced as plain input.
- Every one-shot call site says so explicitly; the conversation path and
  the todo merge do not.
"""
import ast
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUPPORTED_MODELS = {
    "gpt-6.2-sol": {"provider": "openai", "api_model": "gpt-6.2-sol",
                    "cache_diagnostics": True,
                    "input_price_per_mtok": 2.00,
                    "output_price_per_mtok": 10.00,
                    "cached_input_multiplier": 0.05},
    "gpt-5.6-luna": {"provider": "openai", "api_model": "gpt-5.6-luna",
                     "cache_diagnostics": True},
    # Retired, still served, and a GPT-6 model: it takes the option.
    "gpt-6-sol": {"provider": "openai", "api_model": "gpt-6-sol",
                  "cache_diagnostics": True, "deprecated": True,
                  "replaced_by": "gpt-6.2-sol"},
    "gpt-5.5": {"provider": "openai", "api_model": "gpt-5.5",
                "deprecated": True},
    "claude-opus-5.5": {"provider": "anthropic",
                        "api_model": "claude-opus-5-5"},
}

ONE_SHOT = {"prompt_cache_options": {"mode": "explicit"}}

TOOLS = [{"name": "search_nodes", "description": "Search.",
          "input_schema": {"type": "object", "properties": {
              "q": {"type": "string"}}}}]
SCHEMA = {"type": "object", "properties": {"edits": {"type": "array"}},
          "required": ["edits"], "additionalProperties": False}
TURN = [
    {"role": "system", "content": [{"type": "text", "text": "System."}]},
    {"role": "user", "content": [{"type": "text", "text": "First."}]},
    {"role": "assistant", "content": [{"type": "text", "text": "Reply."}]},
    {"role": "user", "content": [{"type": "text", "text": "Second."}]},
]
TURN_INPUT = [
    {"role": "system", "content": [{"type": "input_text", "text": "System."}]},
    {"role": "user", "content": [{"type": "input_text", "text": "First."}]},
    {"role": "assistant",
     "content": [{"type": "output_text", "text": "Reply."}]},
    {"role": "user", "content": [{"type": "input_text", "text": "Second."}]},
]
ONE_USER = [{"role": "user", "content": [{"type": "text", "text": "Doc."}]}]
ONE_USER_INPUT = [
    {"role": "user", "content": [{"type": "input_text", "text": "Doc."}]}]


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = SUPPORTED_MODELS
    with app.app_context():
        yield app


def _load_providers():
    """A private copy of backend/llm_providers.py (as in
    test_openai_cache_diagnostics): sibling modules stub or re-import
    backend.llm_providers in sys.modules."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_llm_providers_under_test_803",
        os.path.join(BACKEND, "llm_providers.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.openai_safety_identifier = lambda user_id: f"sid-{user_id}"
    return module


class _CompletedStream(list):
    def __init__(self, response):
        event = SimpleNamespace(type="response.completed", response=response)
        super().__init__([event])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_openai(details=None):
    """The real provider code with a fake OpenAI client. Returns
    (providers module, list of the kwargs each create() call got)."""
    providers = _load_providers()
    calls = []
    usage = SimpleNamespace(input_tokens=1906, output_tokens=5,
                            total_tokens=1911, input_tokens_details=details)
    response = SimpleNamespace(id="resp_new", output=[], usage=usage,
                               status="completed", incomplete_details=None)

    class FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.responses = self

        def create(self, **kwargs):
            calls.append(kwargs)
            return _CompletedStream(response)

    providers.OpenAI = FakeClient
    return providers, calls


def _fake_anthropic(providers):
    calls = []
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=2,
                              cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
        stop_reason="end_turn")

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter(())

        def get_final_message(self):
            return message

    class FakeAnthropic:
        def __init__(self, api_key=None, **kwargs):
            self.messages = self

        def stream(self, **kwargs):
            calls.append(kwargs)
            return Stream()

    providers.Anthropic = FakeAnthropic
    return calls


def _plain(obj):
    """The request as JSON would send it (tuples and dicts compared as
    their JSON)."""
    return json.loads(json.dumps(obj))


# ── Requests that keep their cache: byte for byte as before #803 ─────────

def test_conversation_turn_body_is_unchanged(app):
    """A turn (and each tool round: the same call) with the thread's
    cache key and a diagnostics baseline."""
    providers, calls = _fake_openai()
    result = providers.LLMProvider.get_completion(
        "gpt-6.2-sol", TURN, {"openai": "k"}, tools=TOOLS,
        prompt_cache_key="loore-t42",
        cache_comparison_response_id="resp_prev", user_id=7)
    assert _plain(calls[0]) == {
        "model": "gpt-6.2-sol", "input": TURN_INPUT,
        "max_output_tokens": 32000, "store": False,
        "safety_identifier": "sid-7", "prompt_cache_key": "loore-t42",
        "tools": [{"type": "function", "name": "search_nodes",
                   "description": "Search.",
                   "parameters": TOOLS[0]["input_schema"],
                   "strict": False}],
        "extra_body": {"prompt_cache_options": {
            "comparison_response_id": "resp_prev"}},
        "stream": True}
    assert list(calls[0]) == [
        "model", "input", "max_output_tokens", "store", "safety_identifier",
        "prompt_cache_key", "tools", "extra_body", "stream"]
    assert result["cache_comparison_sent"] is True


def test_glean_turn_body_is_unchanged(app):
    """Glean: a turn on the conversation path, on its default model, with
    a structured reply and no baseline yet."""
    providers, calls = _fake_openai()
    result = providers.LLMProvider.get_completion(
        "gpt-5.6-luna", TURN, {"openai": "k"}, tools=TOOLS,
        prompt_cache_key="loore-t9", output_schema=SCHEMA, user_id=3)
    assert _plain(calls[0]) == {
        "model": "gpt-5.6-luna", "input": TURN_INPUT,
        "max_output_tokens": 32000, "store": False,
        "safety_identifier": "sid-3", "prompt_cache_key": "loore-t9",
        "tools": [{"type": "function", "name": "search_nodes",
                   "description": "Search.",
                   "parameters": TOOLS[0]["input_schema"],
                   "strict": False}],
        "text": {"format": {"type": "json_schema", "name": "feed_reply",
                            "schema": SCHEMA, "strict": True}},
        "stream": True}
    assert result["cache_comparison_sent"] is False


def test_todo_merge_body_is_unchanged(app):
    """The todo merge's retry reads the first attempt's cache."""
    providers, calls = _fake_openai()
    providers.LLMProvider.get_completion(
        "gpt-6.2-sol", ONE_USER, {"openai": "k"}, output_schema=SCHEMA,
        output_schema_name="todo_edits", user_id=5)
    assert _plain(calls[0]) == {
        "model": "gpt-6.2-sol", "input": ONE_USER_INPUT,
        "max_output_tokens": 32000, "store": False,
        "safety_identifier": "sid-5",
        "text": {"format": {"type": "json_schema", "name": "todo_edits",
                            "schema": SCHEMA, "strict": True}},
        "stream": True}


def test_anthropic_live_request_is_unchanged_even_when_one_shot(app):
    providers, _ = _fake_openai()
    calls = _fake_anthropic(providers)
    providers.LLMProvider.get_completion(
        "claude-opus-5.5", TURN, {"anthropic": "k"}, max_tokens=900)
    expected = {
        "model": "claude-opus-5-5", "max_tokens": 900,
        "system": [{"type": "text", "text": "System."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "First."}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": "Reply."}]},
            {"role": "user",
             "content": [{"type": "text", "text": "Second."}]}]}
    assert _plain(calls[0]) == expected
    if _takes_one_shot(providers.LLMProvider.get_completion):
        providers.LLMProvider.get_completion(
            "claude-opus-5.5", TURN, {"anthropic": "k"}, max_tokens=900,
            one_shot=True)
        assert _plain(calls[1]) == expected


def test_voice_prewarm_request_is_unchanged(app):
    """The pre-warm calls _call_anthropic directly, with cache_control
    markers placed upstream."""
    providers, _ = _fake_openai()
    calls = _fake_anthropic(providers)
    marked = [
        {"role": "system", "content": [{"type": "text", "text": "System.",
                                        "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": [{"type": "text", "text": "Now.",
                                      "cache_control": {"type": "ephemeral"}}]},
    ]
    providers.LLMProvider._call_anthropic(
        "claude-opus-5-5", marked, "k", max_tokens=1)
    assert _plain(calls[0]) == {
        "model": "claude-opus-5-5", "max_tokens": 1,
        "system": [{"type": "text", "text": "System."}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Now.",
             "cache_control": {"type": "ephemeral"}}]}]}


# ── Batch bodies ─────────────────────────────────────────────────────────

def _batch_lines(monkeypatch, requests_by_provider):
    """batch_submit with fake SDKs: (OpenAI JSONL lines, Anthropic
    batch requests)."""
    import types
    from backend.utils import llm_batch
    monkeypatch.setattr(
        "backend.utils.safety_identifier.openai_safety_identifier",
        lambda user_id: f"sid-{user_id}")
    lines = []
    oai = MagicMock()

    def upload(file=None, purpose=None):
        lines.extend(json.loads(x) for x in file.read().decode("utf-8")
                     .splitlines() if x.strip())
        return SimpleNamespace(id="file-1")
    oai.files.create.side_effect = upload
    oai.batches.create.return_value = SimpleNamespace(id="b-oai")
    ant = MagicMock()
    ant.messages.batches.create.return_value = SimpleNamespace(id="b-ant")
    fo = types.ModuleType("openai")
    fo.OpenAI = MagicMock(return_value=oai)
    fa = types.ModuleType("anthropic")
    fa.Anthropic = MagicMock(return_value=ant)
    monkeypatch.setitem(sys.modules, "openai", fo)
    monkeypatch.setitem(sys.modules, "anthropic", fa)
    llm_batch.batch_submit(requests_by_provider,
                           {"openai": "k", "anthropic": "k"}, "test")
    anthropic_requests = (
        ant.messages.batches.create.call_args.kwargs["requests"]
        if ant.messages.batches.create.called else [])
    return lines, anthropic_requests


def _req(custom_id, model_id, api_model, **extra):
    return dict({"custom_id": custom_id, "model_id": model_id,
                 "api_model": api_model, "messages": ONE_USER,
                 "max_tokens": 500, "user_id": 7}, **extra)


CHAT_BODY = {"model": "gpt-6.2-sol", "messages": ONE_USER,
             "max_completion_tokens": 500, "temperature": 1,
             "safety_identifier": "sid-7"}
RESPONSES_BODY = {"model": "gpt-6.2-sol", "input": ONE_USER_INPUT,
                  "max_output_tokens": 500, "store": False,
                  "reasoning": {"effort": "high"},
                  "safety_identifier": "sid-7"}
ANTHROPIC_PARAMS = {"model": "claude-opus-5-5", "max_tokens": 500,
                    "messages": [{"role": "user", "content": "Doc."}]}


def test_batch_bodies_without_one_shot_are_unchanged(app, monkeypatch):
    lines, ant = _batch_lines(monkeypatch, {
        "openai": [
            _req("c", "gpt-6.2-sol", "gpt-6.2-sol"),
            _req("r", "gpt-6.2-sol", "gpt-6.2-sol",
                 endpoint="/v1/responses", reasoning_effort="high")],
        "anthropic": [_req("a", "claude-opus-5.5", "claude-opus-5-5")]})
    by_id = {line["custom_id"]: line for line in lines}
    assert by_id["c"]["body"] == CHAT_BODY
    assert list(by_id["c"]["body"]) == list(CHAT_BODY)
    assert by_id["r"]["body"] == RESPONSES_BODY
    assert list(by_id["r"]["body"]) == list(RESPONSES_BODY)
    assert _plain(ant) == [{"custom_id": "a", "params": ANTHROPIC_PARAMS}]


def test_anthropic_batch_params_ignore_one_shot(app, monkeypatch):
    _, ant = _batch_lines(monkeypatch, {"anthropic": [
        _req("a", "claude-opus-5.5", "claude-opus-5-5", one_shot=True)]})
    assert _plain(ant) == [{"custom_id": "a", "params": ANTHROPIC_PARAMS}]


def test_batch_read_body_is_unchanged(app, monkeypatch):
    """The admin's batch reads (a Glean turn as a one-item batch) keep
    the default."""
    from backend.utils import llm_batch
    lines, _ = _batch_lines(monkeypatch, {})
    oai = sys.modules["openai"].OpenAI.return_value
    llm_batch.openai_batch_submit_one(
        "k", "node-1", "gpt-6.2-sol", ONE_USER, 300, user_id=7)
    assert lines[0]["body"] == {
        "model": "gpt-6.2-sol", "input": ONE_USER_INPUT,
        "max_output_tokens": 300, "store": False,
        "safety_identifier": "sid-7"}
    assert oai.batches.create.call_args.kwargs["endpoint"] == "/v1/responses"


# ── One-shot requests: no cache ──────────────────────────────────────────

def _takes_one_shot(fn):
    import inspect
    return "one_shot" in inspect.signature(fn).parameters


def test_one_shot_live_call_asks_for_no_cache(app):
    providers, calls = _fake_openai()
    result = providers.LLMProvider.get_completion(
        "gpt-6.2-sol", ONE_USER, {"openai": "k"}, user_id=7, one_shot=True)
    assert calls[0]["extra_body"] == ONE_SHOT
    assert _plain(calls[0]) == {
        "model": "gpt-6.2-sol", "input": ONE_USER_INPUT,
        "max_output_tokens": 32000, "store": False,
        "safety_identifier": "sid-7", "extra_body": ONE_SHOT,
        "stream": True}
    # No diagnostics baseline went out.
    assert result["cache_comparison_sent"] is False
    assert "cache_diagnostics" not in result


def test_one_shot_merge_keeps_its_reasoning_effort(app):
    providers, calls = _fake_openai()
    providers.LLMProvider.get_completion(
        "gpt-6.2-sol", ONE_USER, {"openai": "k"}, reasoning_effort="high",
        one_shot=True)
    assert calls[0]["reasoning"] == {"effort": "high"}
    assert calls[0]["extra_body"] == ONE_SHOT


def test_one_shot_on_a_retired_model_that_takes_the_option(app):
    """The flag of the model actually called decides."""
    providers, calls = _fake_openai()
    providers.LLMProvider.get_completion(
        "gpt-6-sol", ONE_USER, {"openai": "k"}, one_shot=True)
    assert calls[0]["model"] == "gpt-6-sol"
    assert calls[0]["extra_body"] == ONE_SHOT


def test_one_shot_never_reaches_an_older_model(app):
    providers, calls = _fake_openai()
    providers.LLMProvider.get_completion(
        "gpt-5.5", ONE_USER, {"openai": "k"}, one_shot=True)
    assert "extra_body" not in calls[0]


def test_one_shot_and_a_baseline_share_one_option(app):
    """No call site sends both; if one ever does, both keys go out in
    the one object."""
    providers, calls = _fake_openai()
    result = providers.LLMProvider.get_completion(
        "gpt-6.2-sol", ONE_USER, {"openai": "k"}, one_shot=True,
        cache_comparison_response_id="resp_prev")
    assert calls[0]["extra_body"] == {"prompt_cache_options": {
        "mode": "explicit", "comparison_response_id": "resp_prev"}}
    assert result["cache_comparison_sent"] is True


def test_rejected_one_shot_option_retries_without_it(app, caplog):
    """The saving never costs the job: a 400 naming the option sends the
    request again without it, and says so at error level."""
    import httpx
    import openai
    providers, calls = _fake_openai()
    accept = providers.OpenAI

    class Rejecting(accept):
        def create(self, **kwargs):
            if "extra_body" in kwargs:
                calls.append(kwargs)
                raise openai.BadRequestError(
                    "Unknown parameter: 'prompt_cache_options'.",
                    response=httpx.Response(400, request=httpx.Request(
                        "POST", "https://api.openai.com/v1/responses")),
                    body=None)
            return super().create(**kwargs)

    providers.OpenAI = Rejecting
    with caplog.at_level("ERROR"):
        result = providers.LLMProvider.get_completion(
            "gpt-6.2-sol", ONE_USER, {"openai": "k"}, one_shot=True)
    assert len(calls) == 2
    assert "extra_body" in calls[0] and "extra_body" not in calls[1]
    assert result["response_id"] == "resp_new"
    assert "prompt_cache_options" in caplog.text


def test_one_shot_batch_bodies(app, monkeypatch):
    lines, _ = _batch_lines(monkeypatch, {"openai": [
        _req("c", "gpt-6.2-sol", "gpt-6.2-sol", one_shot=True),
        _req("r", "gpt-6.2-sol", "gpt-6.2-sol", one_shot=True,
             endpoint="/v1/responses", reasoning_effort="high"),
        _req("old", "gpt-5.5", "gpt-5.5", one_shot=True),
        _req("retired", "gpt-6-sol", "gpt-6-sol", one_shot=True),
        # The model called is not the entry's own: nothing is sent.
        _req("other", "gpt-6.2-sol", "gpt-5.5", one_shot=True),
    ]})
    by_id = {line["custom_id"]: line["body"] for line in lines}
    assert by_id["c"] == dict(CHAT_BODY, **ONE_SHOT)
    assert by_id["r"] == dict(RESPONSES_BODY, **ONE_SHOT)
    assert "prompt_cache_options" not in by_id["old"]
    assert by_id["retired"]["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_options" not in by_id["other"]
    # The request dict's flag is not a body field.
    assert all("one_shot" not in body for body in by_id.values())


def test_one_shot_batch_outside_an_app_context_reads_the_config():
    """llm_batch stays usable without Flask: the config's own models."""
    from backend.config import Config
    from backend.utils.one_shot_cache import one_shot_cache_options
    assert Config.SUPPORTED_MODELS["gpt-6.2-sol"].get("cache_diagnostics")
    assert one_shot_cache_options("gpt-6.2-sol", "gpt-6.2-sol") == {
        "mode": "explicit"}
    assert one_shot_cache_options("gpt-5.5", "gpt-5.5") is None


def test_the_flag_marks_exactly_the_gpt_5_6_and_later_models():
    """Which configured models get the field: OpenAI's GPT-5.6 and later
    (the option is "Supported for gpt-5.6 and later models"); never an
    older OpenAI model, never Anthropic."""
    from backend.config import Config
    from backend.utils.one_shot_cache import takes_prompt_cache_options
    takes = sorted(m for m, cfg in Config.SUPPORTED_MODELS.items()
                   if takes_prompt_cache_options(cfg))
    assert takes == sorted([
        "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-astra",
        "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-luna"])
    for model_id in takes:
        assert Config.SUPPORTED_MODELS[model_id]["api_model"] == model_id


# ── Cost: no cache write is plain input ──────────────────────────────────

def test_live_usage_without_the_write_field_is_plain_input(app):
    """Explicit mode with no breakpoints: OpenAI reports 0 written (or no
    field at all); the row shows 0 and the cost is list input + output."""
    from backend.utils.cost import llm_cost_log_fields
    for details in (None, SimpleNamespace(cached_tokens=0),
                    SimpleNamespace(cached_tokens=0, cache_write_tokens=0)):
        providers, _ = _fake_openai(details=details)
        response = providers.LLMProvider.get_completion(
            "gpt-6.2-sol", ONE_USER, {"openai": "k"}, one_shot=True)
        assert response["cache_write_subset_tokens"] == 0
        fields = llm_cost_log_fields("gpt-6.2-sol", response)
        assert fields["cache_write_tokens"] == 0
        assert fields["cache_read_tokens"] == 0
        # 1906 x $2.00 + 5 x $10.00 per 1M tokens, in microdollars.
        assert fields["cost_microdollars"] == 1906 * 2 + 5 * 10


def test_batch_usage_without_the_write_field_is_plain_input(app):
    from backend.utils.cost import llm_cost_log_fields
    from backend.utils.llm_batch import _responses_result
    result = _responses_result({"object": "response", "status": "completed",
                                "output": [], "usage": {
                                    "input_tokens": 1906,
                                    "output_tokens": 5}})
    assert result["cache_write_subset_tokens"] == 0
    fields = llm_cost_log_fields("gpt-6.2-sol", result)
    assert fields["cache_write_tokens"] == 0
    # Batch: half of the list prices, no discount beyond that.
    assert fields["cost_microdollars"] == round((1906 * 2 + 5 * 10) / 2)


def test_batch_chat_usage_without_the_write_field_is_plain_input(
        app, monkeypatch):
    from backend.utils import llm_batch
    from backend.utils.cost import llm_cost_log_fields
    entry = {"custom_id": "c", "response": {"status_code": 200, "body": {
        "choices": [{"message": {"content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1907, "completion_tokens": 4,
                  "prompt_tokens_details": {"cached_tokens": 0}}}}}
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        id="b", status="completed", output_file_id="out", error_file_id=None,
        created_at=1, completed_at=2, request_counts=None, errors=None)
    client.files.content.return_value = SimpleNamespace(
        content=(json.dumps(entry) + "\n").encode())
    import types
    fo = types.ModuleType("openai")
    fo.OpenAI = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", fo)
    results, pending, _ = llm_batch.batch_check_and_collect(
        {"openai:gpt-6.2-sol": "b"}, {"openai": "k"})
    assert pending == {}
    assert results["c"]["cache_write_subset_tokens"] == 0
    fields = llm_cost_log_fields("gpt-6.2-sol", results["c"])
    assert fields["cache_write_tokens"] == 0
    assert fields["cost_microdollars"] == round((1907 * 2 + 4 * 10) / 2)


# ── Each call site says what it is ───────────────────────────────────────

# The one-shot jobs: every LLMProvider.get_completion call and every
# batch request dict in these files passes the flag.
ONE_SHOT_FILES = [
    "tasks/external_digest.py",      # nightly batch + direct rebuild
    "tasks/recent_context.py",       # batch + direct
    "tasks/intentions.py",           # batch + synchronous
    "tasks/profile_windows.py",      # windows, group and final merges
    "tasks/profile_batch.py",        # chained steps, merge, integration
    "tasks/exports.py",              # synchronous build, merge, integration
    "tasks/poll_draft.py",           # batch
    "utils/prefill_bundle_merge.py",  # the pre-fill bundle's final merge
    "scripts/backfill_intentions.py",  # the intentions job, by hand
]
# The requests that read their own cache: no flag anywhere.
KEEP_FILES = [
    "tasks/llm_completion.py",       # turns, tool rounds, Glean, pre-warm
    "utils/todo_merge_edits.py",     # the retry reads the first attempt
    "tasks/voice_todo_merge.py",
    "scripts/compare_todo_merge_models.py",
]


def _sites(rel):
    """(get_completion calls, batch request dicts) of one source file,
    each with whether it passes one_shot=True."""
    tree = ast.parse(open(os.path.join(BACKEND, rel), encoding="utf-8")
                     .read())
    calls, dicts = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(
                node.func, "attr", None) == "get_completion":
            flag = [k for k in node.keywords if k.arg == "one_shot"]
            calls.append((node.lineno, bool(flag) and isinstance(
                flag[0].value, ast.Constant) and flag[0].value.value is True))
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant)}
            if {"api_model", "messages"} <= keys:
                flag = [v for k, v in zip(node.keys, node.values)
                        if isinstance(k, ast.Constant)
                        and k.value == "one_shot"]
                dicts.append((node.lineno, bool(flag) and isinstance(
                    flag[0], ast.Constant) and flag[0].value is True))
    return calls, dicts


@pytest.mark.parametrize("rel", ONE_SHOT_FILES)
def test_every_one_shot_call_site_passes_the_flag(rel):
    calls, dicts = _sites(rel)
    assert calls or dicts, f"{rel}: no call site found"
    missing = [line for line, flagged in calls + dicts if not flagged]
    assert not missing, f"{rel}: lines {missing} send no one_shot=True"


@pytest.mark.parametrize("rel", KEEP_FILES)
def test_requests_that_read_their_cache_pass_no_flag(rel):
    calls, dicts = _sites(rel)
    flagged = [line for line, f in calls + dicts if f]
    assert not flagged, f"{rel}: lines {flagged} pass one_shot"
    source = open(os.path.join(BACKEND, rel), encoding="utf-8").read()
    assert "one_shot" not in source
