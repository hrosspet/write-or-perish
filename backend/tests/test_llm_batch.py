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
        "truncated": False, "refused": False, "batch": True}
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
        "truncated": False, "refused": False, "batch": True}
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
        "truncated": False, "refused": False, "batch": True}


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


# ── OpenAI batches that end without every result (#419) ──────────────────
# A batch where every request failed has no output file; one that expired
# or was cancelled partway has the billed results of the requests that
# finished. Both have ended: never pending, the billed results collected.

def _oai_chat_line(custom_id):
    return json.dumps({"custom_id": custom_id, "response": {
        "status_code": 200, "body": {
            "choices": [{"message": {"content": f"OUT {custom_id}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}}}})


def _oai_error_line(custom_id, code="batch_expired"):
    return json.dumps({"custom_id": custom_id, "response": None,
                       "error": {"code": code, "message": "not run"}})


def _oai_batch_client(files, **batch):
    """A fake OpenAI client whose batch ends as ``batch`` says, with the
    given files (file id -> JSONL lines)."""
    client = MagicMock()
    batch.setdefault("created_at", 1000)
    batch.setdefault("output_file_id", None)
    batch.setdefault("error_file_id", None)
    client.batches.retrieve.return_value = SimpleNamespace(**batch)
    client.files.content.side_effect = lambda fid: SimpleNamespace(
        content="".join(line + "\n" for line in files[fid]).encode())
    return client


def test_collect_openai_all_failed_batch_has_ended(monkeypatch, caplog):
    """Every request failed: no output file. Ended at once with no
    results (the callers fail the items now), not an unreadable batch
    that waits for the 25 h backstop. The log says why, loudly."""
    client = _oai_batch_client(
        {"ef-1": [_oai_error_line("c1", "context_length_exceeded"),
                  _oai_error_line("c2", "context_length_exceeded")]},
        status="completed", completed_at=1060, error_file_id="ef-1",
        request_counts=SimpleNamespace(total=2, completed=0, failed=2))
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, pending, durations = batch_check_and_collect(
        {"openai:gpt-x": "b-oai"}, KEYS)

    assert (results, pending) == ({}, {})
    assert durations == {"openai:gpt-x": 60}
    # Only the error file is read; never files.content(None).
    assert [c.args for c in client.files.content.call_args_list] == [
        ("ef-1",)]
    assert "context_length_exceeded" in caplog.text
    assert any(r.levelname == "ERROR" and "0 result(s)" in r.getMessage()
               for r in caplog.records)


def test_collect_openai_all_failed_without_any_file_has_ended(monkeypatch):
    client = _oai_batch_client(
        {}, status="completed", completed_at=1060,
        request_counts=SimpleNamespace(total=1, completed=0, failed=1))
    _install_fake_sdks(monkeypatch, openai_client=client)

    assert batch_check_and_collect({"openai:gpt-x": "b"}, KEYS)[:2] == (
        {}, {})
    client.files.content.assert_not_called()


def test_collect_openai_failed_validation_has_ended(monkeypatch):
    client = _oai_batch_client(
        {}, status="failed", failed_at=1005,
        errors=SimpleNamespace(data=[SimpleNamespace(
            code="invalid_json_line", message="bad line", line=1)]),
        request_counts=SimpleNamespace(total=0, completed=0, failed=0))
    _install_fake_sdks(monkeypatch, openai_client=client)

    assert batch_check_and_collect({"openai:gpt-x": "b"}, KEYS)[:2] == (
        {}, {})


def test_collect_openai_empty_output_file_has_ended(monkeypatch):
    client = _oai_batch_client(
        {"of-1": []}, status="completed", completed_at=1060,
        output_file_id="of-1", request_counts="c")
    _install_fake_sdks(monkeypatch, openai_client=client)

    assert batch_check_and_collect({"openai:gpt-x": "b"}, KEYS)[:2] == (
        {}, {})


@pytest.mark.parametrize("status", ["expired", "cancelled"])
def test_collect_openai_ended_early_collects_the_billed_results(
        monkeypatch, status):
    """Expired at 24 h or cancelled: the requests that finished are
    billed and in the output file, so they are collected like a
    completed batch's; the ones that never ran are in the error file and
    have no result."""
    client = _oai_batch_client(
        {"of-1": [_oai_chat_line("done")],
         "ef-1": [_oai_error_line("not-run", f"batch_{status}")]},
        status=status, **{f"{status}_at": 1000 + 86400},
        output_file_id="of-1", error_file_id="ef-1",
        request_counts=SimpleNamespace(total=2, completed=1, failed=1))
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, pending, durations = batch_check_and_collect(
        {"openai:gpt-x": "b-oai"}, KEYS)

    assert pending == {}
    assert results == {"done": {
        "content": "OUT done", "input_tokens": 100, "output_tokens": 50,
        "cached_tokens": 0, "cache_write_subset_tokens": 0,
        "truncated": False, "refused": False, "batch": True}}
    assert durations == {"openai:gpt-x": 86400}


def test_collect_openai_unreadable_error_file_still_collects(monkeypatch):
    """The error file only says why; the billed results don't wait on it."""
    client = _oai_batch_client(
        {"of-1": [_oai_chat_line("done")]},
        status="expired", expired_at=2000, output_file_id="of-1",
        error_file_id="ef-gone",
        request_counts=SimpleNamespace(total=2, completed=1, failed=1))
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, pending, _ = batch_check_and_collect({"openai:gpt-x": "b"}, KEYS)

    assert pending == {} and list(results) == ["done"]


def test_collect_names_the_failures_outside_the_request(monkeypatch):
    """transient_failures: the provider down or overloaded, a rate limit,
    or an account reason (#369). A request the provider rejects (a 400,
    an unknown model) or one the batch never ran counts against the
    request, as before."""
    def oai_line(cid, status, **error):
        return json.dumps({"custom_id": cid, "error": None, "response": {
            "status_code": status, "body": {"error": error}}})
    oai = _oai_batch_client(
        {"ef-1": [oai_line("o500", 500, type="server_error"),
                  oai_line("o429", 429, code="rate_limit_exceeded"),
                  oai_line("oquota", 429, code="insufficient_quota"),
                  oai_line("o401", 401, code="invalid_api_key"),
                  oai_line("o400", 400, code="context_length_exceeded"),
                  oai_line("omodel", 404, code="model_not_found"),
                  _oai_error_line("onotrun", "batch_expired")]},
        status="completed", completed_at=1060, error_file_id="ef-1",
        request_counts=SimpleNamespace(total=7, completed=0, failed=7))

    def ant_item(cid, rtype, etype=None, message=""):
        err = (SimpleNamespace(error=SimpleNamespace(
            type=etype, message=message)) if etype else None)
        return SimpleNamespace(custom_id=cid, result=SimpleNamespace(
            type=rtype, error=err))
    ant = MagicMock()
    ant.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended", request_counts="c",
        created_at=None, ended_at=None)
    ant.messages.batches.results.return_value = [
        ant_item("aover", "errored", "overloaded_error"),
        ant_item("aapi", "errored", "api_error"),
        ant_item("abill", "errored", "billing_error"),
        ant_item("aspend", "errored", "invalid_request_error",
                 "You have reached your specified API usage limits."),
        ant_item("ainvalid", "errored", "invalid_request_error",
                 "prompt is too long"),
        ant_item("aexpired", "expired"),
    ]
    _install_fake_sdks(monkeypatch, anthropic_client=ant, openai_client=oai)

    results, pending, _ = batch_check_and_collect(
        {"anthropic": "b-ant", "openai:gpt-x": "b-oai"}, KEYS)

    assert (dict(results), pending) == ({}, {})
    assert results.transient_failures == {
        "o500", "o429", "oquota", "o401", "aover", "aapi", "abill", "aspend"}


def test_collect_openai_succeeded_without_output_file_stays_pending(
        monkeypatch):
    """Counted (and billed) results whose file isn't there can't be read
    yet: pending, so the items are not failed while billed."""
    client = _oai_batch_client(
        {}, status="completed", completed_at=1060,
        request_counts=SimpleNamespace(total=2, completed=1, failed=1))
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, pending, _ = batch_check_and_collect(
        {"openai:gpt-x": "b-oai"}, KEYS)

    assert (results, pending) == ({}, {"openai:gpt-x": "b-oai"})


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


def test_collect_one_openai_all_failed_batch_is_a_verdict(monkeypatch):
    """#419: a one-item batch whose request failed has no output file; the
    verdict comes at once, without reading a file."""
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="completed", request_counts="counts", output_file_id=None,
        error_file_id="ef-1")
    _install_fake_sdks(monkeypatch, openai_client=client)

    with pytest.raises(BatchItemFailed) as info:
        openai_batch_collect_one("k", "b-oai", "node-7")
    assert not isinstance(info.value, BatchItemCancelled)
    client.files.content.assert_not_called()


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


# ── #545 / #419: large profile builds on OpenAI's Batch API ─────────────────

def _oai_line(cid, content="P"):
    return json.dumps({
        "custom_id": cid,
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }},
    })


def test_collect_openai_expired_batch_returns_its_finished_items(monkeypatch):
    """A batch that expired at 24 h still carries the results of the
    requests that finished (and were billed): they are collected, the
    rest are missing."""
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="expired", request_counts="counts", created_at=1.0,
        completed_at=None, output_file_id="of-partial")
    client.files.content.return_value = SimpleNamespace(
        content=(_oai_line("w1") + "\n" + _oai_line("w2") + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, pending, _ = batch_check_and_collect(
        {"openai:gpt-x": "b"}, KEYS)

    assert pending == {}
    assert set(results) == {"w1", "w2"}


def test_collect_openai_batch_without_output_returns_at_once(monkeypatch):
    for status in ("failed", "expired", "completed", "cancelled"):
        client = MagicMock()
        client.batches.retrieve.return_value = SimpleNamespace(
            status=status, request_counts="counts", created_at=1.0,
            completed_at=None, output_file_id=None)
        _install_fake_sdks(monkeypatch, openai_client=client)

        results, pending, _ = batch_check_and_collect(
            {"openai:gpt-x": "b"}, KEYS)

        assert (results, pending) == ({}, {})
        client.files.content.assert_not_called()


def test_batch_submit_openai_writes_utf8_and_the_effort(monkeypatch):
    """The JSONL keeps non-ASCII text as UTF-8 (2 bytes for a Czech letter,
    not a 6-byte escape) against the 200 MB input limit, and a request's
    reasoning effort reaches the body."""
    written = {}
    client = MagicMock()

    def create(file=None, purpose=None):
        written["bytes"] = file.read()
        return SimpleNamespace(id="file-1")
    client.files.create.side_effect = create
    client.batches.create.return_value = SimpleNamespace(id="b-oai")
    _install_fake_sdks(monkeypatch, openai_client=client)

    batch_submit({"openai": [
        {"custom_id": "m", "model_id": "sol", "api_model": "gpt-x",
         "messages": [{"role": "user", "content": "žluťoučký kůň 🐴"}],
         "max_tokens": 500, "reasoning_effort": "high"},
        {"custom_id": "v", "model_id": "luna", "api_model": "gpt-x",
         "messages": [{"role": "user", "content": "plain"}],
         "max_tokens": 500}]}, KEYS, "profile")

    raw = written["bytes"]
    assert "žluťoučký kůň 🐴".encode("utf-8") in raw
    assert b"\\u" not in raw
    lines = [json.loads(x) for x in raw.decode("utf-8").splitlines()]
    assert lines[0]["body"]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in lines[1]["body"]


def test_batch_submit_openai_responses_endpoint_and_effort(monkeypatch):
    """A request naming the Responses endpoint goes into its own batch on
    that endpoint, with the live call's body and the reasoning effort as
    the Responses API takes it."""
    written = []
    client = MagicMock()

    def create(file=None, purpose=None):
        written.append(file.read())
        return SimpleNamespace(id=f"file-{len(written)}")
    client.files.create.side_effect = create
    client.batches.create.side_effect = [SimpleNamespace(id="b-chat"),
                                         SimpleNamespace(id="b-resp")]
    _install_fake_sdks(monkeypatch, openai_client=client)

    out = batch_submit({"openai": [
        {"custom_id": "w", "model_id": "luna", "api_model": "gpt-x",
         "messages": [{"role": "user", "content": [
             {"type": "text", "text": "window"}]}], "max_tokens": 500},
        {"custom_id": "m", "model_id": "sol", "api_model": "gpt-x",
         "endpoint": "/v1/responses", "reasoning_effort": "high",
         "messages": [{"role": "user", "content": [
             {"type": "text", "text": "merge"}]}], "max_tokens": 700},
    ]}, KEYS, "profile")

    assert out == {"openai:gpt-x": "b-chat",
                   "openai:gpt-x:responses": "b-resp"}
    endpoints = [c.kwargs["endpoint"]
                 for c in client.batches.create.call_args_list]
    assert endpoints == ["/v1/chat/completions", "/v1/responses"]
    line = json.loads(written[1].decode("utf-8"))
    assert line["url"] == "/v1/responses"
    assert line["body"] == {
        "model": "gpt-x", "max_output_tokens": 700, "store": False,
        "reasoning": {"effort": "high"},
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "merge"}]}]}


def test_collect_openai_reads_a_responses_result(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = SimpleNamespace(
        status="completed", request_counts="counts", created_at=1.0,
        completed_at=2.0, output_file_id="of-1")
    line = json.dumps({"custom_id": "m", "response": {
        "status_code": 200, "body": {
            "object": "response", "status": "completed",
            "output": [{"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": "MERGED"}]}],
            "usage": {"input_tokens": 70, "output_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 5}}}}})
    client.files.content.return_value = SimpleNamespace(
        content=(line + "\n").encode())
    _install_fake_sdks(monkeypatch, openai_client=client)

    results, _, _ = batch_check_and_collect(
        {"openai:gpt-x:responses": "b"}, KEYS)

    assert results["m"] == {
        "content": "MERGED", "refused": False, "input_tokens": 70,
        "output_tokens": 30, "cached_tokens": 5,
        "cache_write_subset_tokens": 0, "truncated": False, "batch": True}
