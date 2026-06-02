"""Unit tests for backend/utils/llm_batch.py — the shared Batch API helpers
extracted from the prompt-RCT harness and used by the profile-batch pipeline.

SDK clients are faked via sys.modules so neither `anthropic` nor `openai`
needs to be installed; the functions import them lazily inside the body.
"""
import json
import os
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
sys.modules.setdefault("celery", MagicMock())

import pytest  # noqa: E402

from backend.utils.llm_batch import (  # noqa: E402
    apply_batch_key_override,
    _convert_messages_for_anthropic,
    batch_submit,
    batch_check_and_collect,
)

KEYS = {"anthropic": "k-ant", "openai": "k-oai"}


def _install_fake_sdks(monkeypatch, anthropic_client=None, openai_client=None):
    fa = types.ModuleType("anthropic")
    fa.Anthropic = MagicMock(return_value=anthropic_client or MagicMock())
    fo = types.ModuleType("openai")
    fo.OpenAI = MagicMock(return_value=openai_client or MagicMock())
    monkeypatch.setitem(sys.modules, "anthropic", fa)
    monkeypatch.setitem(sys.modules, "openai", fo)


# ── pure helpers ─────────────────────────────────────────────────────────

def test_apply_batch_key_override_overlays_and_is_non_mutating():
    base = {"openai": "interactive", "anthropic": "ant"}
    out = apply_batch_key_override(base, {"OPENAI_API_KEY_BATCH": "batchkey"})
    assert out["openai"] == "batchkey"
    assert out["anthropic"] == "ant"
    assert base["openai"] == "interactive"   # input untouched


def test_apply_batch_key_override_noop_without_batch_key():
    base = {"openai": "interactive", "anthropic": "ant"}
    out = apply_batch_key_override(base, {})
    assert out == base


def test_convert_messages_for_anthropic_splits_system_and_text():
    messages = [
        {"role": "system", "content": "you are X"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": "hi"},
    ]
    system_param, ant_messages = _convert_messages_for_anthropic(messages)
    assert system_param == [{"type": "text", "text": "you are X"}]
    assert ant_messages == [
        {"role": "user", "content": "hello"},      # list-of-dict flattened
        {"role": "assistant", "content": "hi"},
    ]


# ── submit ───────────────────────────────────────────────────────────────

def test_batch_submit_anthropic(monkeypatch):
    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="b-ant")
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    reqs = {"anthropic": [{
        "custom_id": "profile:1:0:1:chunk", "model_id": "claude",
        "api_model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}], "max_tokens": 500,
    }]}
    out = batch_submit(reqs, KEYS, "profile")

    assert out == {"anthropic": "b-ant"}
    sent = client.messages.batches.create.call_args.kwargs["requests"]
    assert sent[0]["custom_id"] == "profile:1:0:1:chunk"
    assert sent[0]["params"]["model"] == "claude-x"
    assert sent[0]["params"]["max_tokens"] == 500


def test_batch_submit_openai(monkeypatch, tmp_path):
    client = MagicMock()
    client.files.create.return_value = SimpleNamespace(id="file-1")
    client.batches.create.return_value = SimpleNamespace(id="b-oai")
    _install_fake_sdks(monkeypatch, openai_client=client)

    reqs = {"openai": [{
        "custom_id": "profile:2:0:1:chunk", "model_id": "gpt",
        "api_model": "gpt-x",
        "messages": [{"role": "user", "content": "hi"}], "max_tokens": 500,
    }]}
    out = batch_submit(reqs, KEYS, "profile")

    assert out == {"openai:gpt-x": "b-oai"}
    assert client.batches.create.call_args.kwargs["completion_window"] == "24h"


# ── check + collect ───────────────────────────────────────────────────────

def test_collect_anthropic_succeeded(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="counts",
        created_at=datetime(2026, 6, 1, 0, 0, 0),
        ended_at=datetime(2026, 6, 1, 0, 2, 0),
    )
    entry = SimpleNamespace(
        custom_id="profile:1:0:1:chunk",
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                content=[SimpleNamespace(text="PROFILE TEXT")],
                usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            ),
        ),
    )
    client.messages.batches.results.return_value = [entry]
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    results, pending, durations = batch_check_and_collect(
        {"anthropic": "b-ant"}, KEYS)

    assert pending == {}
    assert results["profile:1:0:1:chunk"] == {
        "content": "PROFILE TEXT", "input_tokens": 100, "output_tokens": 50}
    assert durations["anthropic"] == 120.0


def test_collect_anthropic_still_pending(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="in_progress", request_counts="counts")
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    results, pending, _ = batch_check_and_collect({"anthropic": "b-ant"}, KEYS)

    assert results == {}
    assert pending == {"anthropic": "b-ant"}
    client.messages.batches.results.assert_not_called()


def test_collect_openai_completed(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="completed", request_counts="counts",
        created_at=1000.0, completed_at=1120.0, output_file_id="of-1")
    line = json.dumps({
        "custom_id": "profile:2:0:1:chunk",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": "P"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }},
    })
    client.files.content.return_value = SimpleNamespace(
        content=(line + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, pending, durations = batch_check_and_collect(
        {"openai:gpt-x": "b-oai"}, KEYS)

    assert pending == {}
    assert results["profile:2:0:1:chunk"] == {
        "content": "P", "input_tokens": 100, "output_tokens": 50}
    assert durations["openai:gpt-x"] == 120.0
