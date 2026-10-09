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
    BatchItemFailed,
    BatchItemCancelled,
    anthropic_batch_submit_one,
    anthropic_batch_collect_one,
    openai_batch_collect_one,
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


def test_batch_submit_reports_an_account_refusal(monkeypatch):
    """A background batch refused for an account reason (#369) has no user
    to tell: the submit fails as before (no batch id) and the admin is
    alerted through the same report a live call makes."""
    import anthropic as real_anthropic
    import httpx
    import backend
    # The real provider module, imported before the fake SDKs go in.
    monkeypatch.setattr(backend, "llm_providers",
                        getattr(backend, "llm_providers", None),
                        raising=False)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers  # noqa: F401
    import backend.utils.provider_alerts as alerts
    reported = []
    monkeypatch.setattr(alerts, "report_account_failure",
                        lambda err, exc: reported.append((err, exc)))
    body = {"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "You have reached your specified API usage limits. You "
                   "will regain access on 2026-11-01 at 00:00 UTC."}}
    raw = real_anthropic.Anthropic(api_key="k")._make_status_error(
        str(body), body=body, response=httpx.Response(
            400, request=httpx.Request(
                "POST", "https://api.anthropic.com/v1/messages/batches")))
    client = MagicMock()
    client.messages.batches.create.side_effect = raw
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    out = batch_submit({"anthropic": [{
        "custom_id": "digest:1", "model_id": "claude", "api_model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}], "max_tokens": 500,
    }]}, KEYS, "digest")

    assert out == {}
    assert [(e.provider, e.kind, x) for e, x in reported] == [
        ("Anthropic", "spend_limit", raw)]
    # The caller learns it was the account, not the requests (#406
    # review: profile_batch doesn't count it as a failed attempt).
    assert out.account_refused == {"anthropic"}


def test_batch_submit_names_an_openai_refusal_and_its_key(monkeypatch):
    """OpenAI, one batch per model: the refused model's provider key is in
    account_refused, and the report names the key by its role — the batch
    key here (apply_batch_key_override) — never the key itself."""
    import httpx
    import openai as real_openai
    import backend
    from flask import Flask
    monkeypatch.setattr(backend, "llm_providers",
                        getattr(backend, "llm_providers", None),
                        raising=False)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers  # noqa: F401
    import backend.utils.provider_alerts as alerts
    reported = []
    monkeypatch.setattr(alerts, "report_account_failure",
                        lambda err, exc: reported.append((err, exc)))
    body = {"error": {"message": "spend limit", "type": "insufficient_quota",
                      "param": None, "code": "project_spend_limit_exceeded"}}
    raw = real_openai.OpenAI(api_key="k")._make_status_error(
        f"Error code: 429 - {body}", body=body, response=httpx.Response(
            429, request=httpx.Request(
                "POST", "https://api.openai.com/v1/batches")))
    client = MagicMock()
    client.files.create.return_value = SimpleNamespace(id="file-1")
    client.batches.create.side_effect = raw
    _install_fake_sdks(monkeypatch, openai_client=client)
    app = Flask(__name__)
    app.config.update(OPENAI_API_KEY_CHAT="k-chat",
                      OPENAI_API_KEY_BATCH="k-oai")

    with app.app_context():
        out = batch_submit({"openai": [{
            "custom_id": "profile_2_0_chunk", "model_id": "gpt",
            "api_model": "gpt-x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 500,
        }]}, KEYS, "profile")

    assert out == {}
    assert out.account_refused == {"openai:gpt-x"}
    (err, exc), = reported
    assert (err.provider, err.kind, err.model, err.key_role, exc) == (
        "OpenAI", "spend_limit", "gpt-x", "batch", raw)


def test_batch_submit_other_failures_are_not_account_refusals(monkeypatch):
    client = MagicMock()
    client.messages.batches.create.side_effect = RuntimeError("boom")
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    out = batch_submit({"anthropic": [{
        "custom_id": "profile_1_0_chunk", "model_id": "claude",
        "api_model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}], "max_tokens": 500,
    }]}, KEYS, "profile")

    assert out == {}
    assert out.account_refused == set()


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
        "content": "PROFILE TEXT", "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "truncated": False, "batch": True}
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
        "content": "P", "input_tokens": 100, "output_tokens": 50,
        "cached_tokens": 0, "cache_write_subset_tokens": 0,
        "truncated": False, "batch": True}
    assert durations["openai:gpt-x"] == 120.0


def test_collect_anthropic_keeps_cache_counters(monkeypatch):
    """Batch results carry the same cache keys as live calls, so the
    profile/digest cost rows price cache reads and writes (#286)."""
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="c",
        created_at=None, ended_at=None)
    entry = SimpleNamespace(
        custom_id="c1",
        result=SimpleNamespace(type="succeeded", message=SimpleNamespace(
            content=[SimpleNamespace(text="T")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=900,
                                  cache_creation_input_tokens=90))))
    client.messages.batches.results.return_value = [entry]
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    results, _, _ = batch_check_and_collect({"anthropic": "b"}, KEYS)
    assert results["c1"]["cache_read_input_tokens"] == 900
    assert results["c1"]["cache_creation_input_tokens"] == 90
    assert results["c1"]["batch"] is True


def test_collect_openai_reads_chat_completions_cache_details(monkeypatch):
    """chat/completions spells the counters prompt_tokens_details; the
    write subset must come out under the same key the live Responses
    call uses, or OpenAI-model batch builds bill writes at 1.0x."""
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="completed", request_counts="c",
        created_at=None, completed_at=None, output_file_id="of-1")
    line = json.dumps({
        "custom_id": "c2",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": "P"}}],
            "usage": {"prompt_tokens": 6018, "completion_tokens": 50,
                      "prompt_tokens_details": {
                          "cached_tokens": 2815,
                          "cache_write_tokens": 3000}},
        }},
    })
    client.files.content.return_value = SimpleNamespace(
        content=(line + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, _, _ = batch_check_and_collect({"openai:gpt-x": "b"}, KEYS)
    assert results["c2"] == {
        "content": "P", "input_tokens": 6018, "output_tokens": 50,
        "cached_tokens": 2815, "cache_write_subset_tokens": 3000,
        "truncated": False, "batch": True}


def test_collect_marks_cut_off_results_truncated(monkeypatch):
    """#368: the collectors refuse an empty result cut off at the output
    limit, so the multi-item collect must carry the flag both providers
    report (Anthropic stop_reason, chat/completions finish_reason)."""
    ant = MagicMock()
    ant.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="c",
        created_at=None, ended_at=None)
    ant.messages.batches.results.return_value = [
        SimpleNamespace(custom_id="cut", result=SimpleNamespace(
            type="succeeded", message=SimpleNamespace(
                content=[], stop_reason="max_tokens",
                usage=SimpleNamespace(input_tokens=10, output_tokens=32000)))),
        SimpleNamespace(custom_id="done", result=SimpleNamespace(
            type="succeeded", message=SimpleNamespace(
                content=[SimpleNamespace(text="T")], stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5)))),
    ]
    oai = MagicMock()
    oai.batches.retrieve.return_value = SimpleNamespace(
        status="completed", request_counts="c",
        created_at=None, completed_at=None, output_file_id="of-1")
    line = json.dumps({
        "custom_id": "ocut",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": ""},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 32000},
        }},
    })
    oai.files.content.return_value = SimpleNamespace(
        content=(line + "\n").encode())
    _install_fake_sdks(monkeypatch, anthropic_client=ant, openai_client=oai)

    results, _, _ = batch_check_and_collect(
        {"anthropic": "b", "openai:gpt-x": "b2"}, KEYS)
    assert results["cut"]["truncated"] is True
    assert results["done"]["truncated"] is False
    assert results["ocut"]["truncated"] is True


# ── one-item collect: the provider's verdict vs. a transient error ───────
# (2026-09-18) The feed poll fails the node only on BatchItemFailed; any
# other error is polled again.

def test_collect_one_anthropic_errored_item_is_a_verdict(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="counts")
    client.messages.batches.results.return_value = [SimpleNamespace(
        custom_id="node-7",
        result=SimpleNamespace(type="expired", error=None))]
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    with pytest.raises(BatchItemFailed, match="expired"):
        anthropic_batch_collect_one("k", "b-ant", "node-7")


def test_collect_one_anthropic_missing_item_is_a_verdict(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="counts")
    client.messages.batches.results.return_value = []
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    with pytest.raises(BatchItemFailed, match="without item"):
        anthropic_batch_collect_one("k", "b-ant", "node-7")


def test_collect_one_anthropic_still_processing(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="in_progress", request_counts="counts")
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    assert anthropic_batch_collect_one("k", "b-ant", "node-7") == (
        "in_progress", None)
    client.messages.batches.results.assert_not_called()


def test_collect_one_openai_cancelled_batch_is_a_verdict(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="cancelled", request_counts="counts")
    _install_fake_sdks(monkeypatch, openai_client=client)

    with pytest.raises(BatchItemFailed, match="cancelled"):
        openai_batch_collect_one("k", "b-oai", "node-7")


def test_collect_one_openai_failed_item_is_a_verdict(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="completed", request_counts="counts", output_file_id="of-1")
    line = json.dumps({"custom_id": "node-7", "error": {"message": "boom"},
                       "response": {"status_code": 500, "body": {}}})
    client.files.content.return_value = SimpleNamespace(
        content=(line + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    with pytest.raises(BatchItemFailed, match="boom"):
        openai_batch_collect_one("k", "b-oai", "node-7")


def test_collect_one_openai_still_processing(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="in_progress", request_counts="counts")
    _install_fake_sdks(monkeypatch, openai_client=client)

    assert openai_batch_collect_one("k", "b-oai", "node-7") == (
        "in_progress", None)
    client.files.content.assert_not_called()


def test_collect_one_transport_error_is_not_a_verdict(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.side_effect = ConnectionError("reset by peer")
    _install_fake_sdks(monkeypatch, openai_client=client)

    with pytest.raises(ConnectionError):
        openai_batch_collect_one("k", "b-oai", "node-7")


# ── one-item collect after a cancel: what the provider reports decides ───
# (2026-09-18) The feed poll withdraws a batch when its user hits the
# spend cap. An item processed before the cancel took effect is billed
# and collected; one that never ran raises BatchItemCancelled.

def _oai_output_line(custom_id="node-7"):
    return json.dumps({"custom_id": custom_id, "response": {
        "status_code": 200, "body": {
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "P"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50}}}})


def test_collect_one_openai_cancelling_is_pending(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="cancelling", request_counts="counts")
    _install_fake_sdks(monkeypatch, openai_client=client)

    assert openai_batch_collect_one("k", "b-oai", "node-7") == (
        "cancelling", None)
    client.files.content.assert_not_called()


def test_collect_one_openai_cancelled_with_the_item_done_is_collected(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="cancelled", request_counts="counts", output_file_id="of-1")
    client.files.content.return_value = SimpleNamespace(
        content=(_oai_output_line() + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    status, resp = openai_batch_collect_one("k", "b-oai", "node-7")

    assert status == "cancelled"
    assert resp["content"] == "P"
    assert resp["total_tokens"] == 150
    assert resp["batch"] is True


def test_collect_one_openai_cancelled_without_the_item_is_cancelled(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="cancelled", request_counts="counts", output_file_id="of-1")
    client.files.content.return_value = SimpleNamespace(
        content=(_oai_output_line("someone-else") + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    with pytest.raises(BatchItemCancelled):
        openai_batch_collect_one("k", "b-oai", "node-7")

    client.batches.retrieve.return_value = SimpleNamespace(
        status="cancelled", request_counts="counts", output_file_id=None)
    with pytest.raises(BatchItemCancelled):
        openai_batch_collect_one("k", "b-oai", "node-7")


def test_collect_one_openai_expired_with_the_item_done_is_collected(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="expired", request_counts="counts", output_file_id="of-1")
    client.files.content.return_value = SimpleNamespace(
        content=(_oai_output_line() + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    status, resp = openai_batch_collect_one("k", "b-oai", "node-7")
    assert status == "expired"
    assert resp["content"] == "P"


def test_collect_one_openai_expired_without_the_item_is_a_verdict_not_a_cancel(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="expired", request_counts="counts", output_file_id=None)
    _install_fake_sdks(monkeypatch, openai_client=client)

    with pytest.raises(BatchItemFailed, match="expired") as info:
        openai_batch_collect_one("k", "b-oai", "node-7")
    assert not isinstance(info.value, BatchItemCancelled)


def test_collect_one_anthropic_canceling_is_pending(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="canceling", request_counts="counts")
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    assert anthropic_batch_collect_one("k", "b-ant", "node-7") == (
        "canceling", None)


def test_collect_one_anthropic_canceled_item_is_cancelled(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="counts")
    client.messages.batches.results.return_value = [SimpleNamespace(
        custom_id="node-7",
        result=SimpleNamespace(type="canceled", error=None))]
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    with pytest.raises(BatchItemCancelled, match="before it ran"):
        anthropic_batch_collect_one("k", "b-ant", "node-7")


# ── a Read on Haiku 5.5 (Peter, 2026-10-08) ──────────────────────────────
# Haiku 5.5 refuses non-default sampling parameters and a `fallbacks`
# list with a 400, and thinks (adaptive, effort medium) unless told
# otherwise; its reply can start with a thinking block whose text is
# empty. The Read's one-item batch sends none of those settings and keeps
# only the text.

def test_read_batch_on_haiku_5_5_sends_only_supported_params(monkeypatch):
    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="b-ant")
    _install_fake_sdks(monkeypatch, anthropic_client=client)
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}},
              "required": ["verdict"], "additionalProperties": False}

    assert anthropic_batch_submit_one(
        "k", "node-7", "claude-haiku-5-5",
        [{"role": "system", "content": "read prompt"},
         {"role": "user", "content": "tweets"}],
        32000, output_schema=schema) == "b-ant"

    params = client.messages.batches.create.call_args.kwargs[
        "requests"][0]["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}}
    assert set(params) == {"model", "max_tokens", "messages", "system",
                           "output_config"}


def test_read_batch_collect_skips_a_thinking_block(monkeypatch):
    client = MagicMock()
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="counts")
    client.messages.batches.results.return_value = [SimpleNamespace(
        custom_id="node-7",
        result=SimpleNamespace(type="succeeded", message=SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="",
                                     signature="sig"),
                     SimpleNamespace(type="text", text='{"verdict": "ok"}')],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=270_000,
                                  output_tokens=3_000))))]
    _install_fake_sdks(monkeypatch, anthropic_client=client)

    status, resp = anthropic_batch_collect_one("k", "b-ant", "node-7")
    assert status == "ended"
    assert resp["content"] == '{"verdict": "ok"}'
    assert (resp["input_tokens"], resp["output_tokens"]) == (270_000, 3_000)
    assert resp["truncated"] is False and resp["batch"] is True
