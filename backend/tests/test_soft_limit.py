"""Celery's soft time limit reaches the task through the provider calls
(backend/utils/soft_limit.py): a token count and a batch submission raise
it instead of returning None or "the submission failed", and the SDKs do
not send the request again (2026-11-07: a profile run past its soft limit
went on rendering until the hard limit killed it).

The real Anthropic and OpenAI SDKs run here; httpx's send is replaced, so
nothing reaches the network.
"""
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
sys.modules.setdefault("celery", MagicMock())

import anthropic  # noqa: E402
import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from billiard.exceptions import SoftTimeLimitExceeded  # noqa: E402
from flask import Flask  # noqa: E402

from backend.utils import soft_limit  # noqa: E402
from backend.utils import llm_batch  # noqa: E402

KEYS = {"anthropic": "k-ant", "openai": "k-oai"}
MODELS = {
    "claude-x": {"provider": "anthropic", "api_model": "claude-x"},
    "gpt-x": {"provider": "openai", "api_model": "gpt-x"},
}


@pytest.fixture
def sends(monkeypatch):
    """httpx's send raises the soft limit, as Celery's signal handler
    does in the middle of a request; counts the sends. The SDKs' retry
    sleeps are skipped."""
    calls = []

    def send(self, request, *a, **k):
        calls.append(str(request.url))
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(httpx.Client, "send", send)
    import anthropic._base_client as abc
    import openai._base_client as obc
    monkeypatch.setattr(abc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(obc.time, "sleep", lambda *_: None)
    return calls


@pytest.fixture
def providers(monkeypatch):
    # The real provider module, imported fresh (other test files mock it);
    # as test_provider_streaming.py does.
    import backend
    monkeypatch.setattr(backend, "llm_providers",
                        getattr(backend, "llm_providers", None),
                        raising=False)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers as mod
    app = Flask(__name__)
    app.config["SUPPORTED_MODELS"] = MODELS
    with app.app_context():
        yield mod


def _batch_req(cid="c1", model="claude-x"):
    return {"custom_id": cid, "model_id": model, "api_model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10}


# ── the SDKs ──────────────────────────────────────────────────────────────

def test_found_in_follows_the_sdk_s_cause():
    soft = SoftTimeLimitExceeded()
    try:
        try:
            raise soft
        except SoftTimeLimitExceeded as e:
            raise anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://x")) from e
    except anthropic.APIConnectionError as wrapped:
        assert soft_limit.found_in(wrapped) is soft
    assert soft_limit.found_in(RuntimeError("x")) is None


def test_without_the_client_the_sdk_retries_and_hides_the_soft_limit(sends):
    """The problem: the SDK's own HTTP client sends the request again and
    then raises a connection error."""
    client = anthropic.Anthropic(api_key="k", max_retries=2)
    with pytest.raises(anthropic.APIConnectionError) as info:
        client.messages.batches.create(requests=[])
    assert len(sends) == 3
    assert soft_limit.found_in(info.value) is not None


@pytest.mark.parametrize("sdk", ["anthropic", "openai"])
def test_with_the_client_the_soft_limit_comes_through_once(sends, sdk):
    if sdk == "anthropic":
        client = anthropic.Anthropic(
            api_key="k", max_retries=2, **soft_limit.client_kwargs(anthropic))
        call = lambda: client.messages.batches.create(requests=[])  # noqa: E731
    else:
        client = openai.OpenAI(
            api_key="k", max_retries=2, **soft_limit.client_kwargs(openai))
        call = lambda: client.batches.create(  # noqa: E731
            input_file_id="f", endpoint="/v1/responses",
            completion_window="24h")
    with pytest.raises(SoftTimeLimitExceeded):
        with soft_limit.passes():
            call()
    assert len(sends) == 1


def test_client_kwargs_of_a_stand_in_module_is_empty():
    assert soft_limit.client_kwargs(types.ModuleType("anthropic")) == {}


# ── token counts ─────────────────────────────────────────────────────────

def test_an_anthropic_token_count_raises_the_soft_limit(sends, providers):
    with pytest.raises(SoftTimeLimitExceeded):
        providers.LLMProvider.count_tokens(
            "claude-x", [{"role": "user", "content": "hi"}], KEYS)
    assert len(sends) == 1


def test_an_anthropic_token_count_still_falls_back_on_other_errors(
        monkeypatch, providers):
    def send(self, request, *a, **k):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(httpx.Client, "send", send)
    assert providers.LLMProvider.count_tokens(
        "claude-x", [{"role": "user", "content": "hi"}], KEYS) is None


def test_an_openai_token_count_raises_the_soft_limit(monkeypatch, providers):
    fake = types.ModuleType("tiktoken")

    def encoding_for_model(model):
        raise SoftTimeLimitExceeded()
    fake.encoding_for_model = encoding_for_model
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    with pytest.raises(SoftTimeLimitExceeded):
        providers.LLMProvider.count_tokens(
            "gpt-x", [{"role": "user", "content": "hi"}], KEYS)


def test_an_openai_token_count_still_falls_back_on_other_errors(
        monkeypatch, providers):
    fake = types.ModuleType("tiktoken")

    def encoding_for_model(model):
        raise RuntimeError("synthetic")
    fake.encoding_for_model = encoding_for_model
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    assert providers.LLMProvider.count_tokens(
        "gpt-x", [{"role": "user", "content": "hi"}], KEYS) is None


# ── batch submission ─────────────────────────────────────────────────────

def test_an_anthropic_batch_submit_raises_the_soft_limit_once(sends):
    with pytest.raises(SoftTimeLimitExceeded) as info:
        llm_batch.batch_submit({"anthropic": [_batch_req()]}, KEYS, "t")
    assert len(sends) == 1
    assert dict(info.value.submitted) == {}


def test_an_openai_batch_submit_raises_the_soft_limit_once(
        sends, monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SoftTimeLimitExceeded):
        llm_batch.batch_submit({"openai": [_batch_req(model="gpt-x")]},
                               KEYS, "t")
    assert len(sends) == 1                  # the file upload, not again
    assert list(tmp_path.iterdir()) == []   # the JSONL file is removed


def test_a_soft_limit_in_our_own_code_is_not_a_failed_submit(
        monkeypatch, tmp_path):
    """Raised while the JSONL file is written (no SDK call yet): the
    submit's own error handling used to log it and return no batch."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def boom(messages):
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(llm_batch, "_responses_input", boom)
    req = dict(_batch_req(model="gpt-x"), endpoint=llm_batch.RESPONSES_ENDPOINT)
    with pytest.raises(SoftTimeLimitExceeded):
        llm_batch.batch_submit({"openai": [req]}, KEYS, "t")
    assert list(tmp_path.iterdir()) == []


def test_the_batches_created_before_the_soft_limit_come_with_it(
        monkeypatch):
    """Anthropic's batch went out, then the soft limit hit OpenAI's: the
    exception carries the Anthropic batch id, so the caller can record
    it (its requests are at the provider and are billed)."""
    ant = MagicMock()
    ant.messages.batches.create.return_value = SimpleNamespace(id="b-ant")
    oai = MagicMock()
    oai.files.create.side_effect = SoftTimeLimitExceeded()
    fa = types.ModuleType("anthropic")
    fa.Anthropic = MagicMock(return_value=ant)
    fo = types.ModuleType("openai")
    fo.OpenAI = MagicMock(return_value=oai)
    monkeypatch.setitem(sys.modules, "anthropic", fa)
    monkeypatch.setitem(sys.modules, "openai", fo)
    with pytest.raises(SoftTimeLimitExceeded) as info:
        llm_batch.batch_submit({"anthropic": [_batch_req("a")],
                                "openai": [_batch_req("o", "gpt-x")]},
                               KEYS, "t")
    assert dict(info.value.submitted) == {"anthropic": "b-ant"}


def test_a_one_item_submit_raises_the_soft_limit_once(sends):
    with pytest.raises(SoftTimeLimitExceeded):
        llm_batch.anthropic_batch_submit_one(
            "k", "c1", "claude-x", [{"role": "user", "content": "hi"}], 10)
    with pytest.raises(SoftTimeLimitExceeded):
        llm_batch.openai_batch_submit_one(
            "k", "c1", "gpt-x", [{"role": "user", "content": "hi"}], 10)
    assert len(sends) == 2
