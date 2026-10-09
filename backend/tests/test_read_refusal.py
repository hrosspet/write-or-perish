"""#454: provider refusals reach the Read as `refused`, from the live
calls and from both providers' batch results. Mocked responses only."""
import json
from types import SimpleNamespace as NS

import pytest

from backend.utils import llm_batch

pytest.importorskip("anthropic")
pytest.importorskip("openai")


def _usage(**kw):
    base = dict(input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, cache_creation_input_tokens=0)
    base.update(kw)
    return NS(**base)


def _anthropic_message(stop_reason, text):
    return NS(content=[NS(type="text", text=text)], usage=_usage(),
              stop_reason=stop_reason)


class _FakeBatches:
    def __init__(self, message):
        self.message = message

    def retrieve(self, batch_id):
        return NS(processing_status="ended", request_counts=None)

    def results(self, batch_id):
        return [NS(custom_id="node-1",
                   result=NS(type="succeeded", message=self.message))]


@pytest.mark.parametrize("stop,refused", [("refusal", True),
                                          ("end_turn", False)])
def test_anthropic_batch_one_flags_a_refusal(monkeypatch, stop, refused):
    import anthropic
    client = NS(messages=NS(
        batches=_FakeBatches(_anthropic_message(stop, "x"))))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: client)
    status, resp = llm_batch.anthropic_batch_collect_one(
        "key", "batch_1", "node-1")
    assert status == "ended"
    assert resp["refused"] is refused


def _openai_batch_client(body):
    line = json.dumps({"custom_id": "node-1",
                       "response": {"status_code": 200, "body": body}})

    class _Files:
        def content(self, file_id):
            return NS(content=line.encode())
    return NS(
        batches=NS(retrieve=lambda bid: NS(
            status="completed", output_file_id="f1", request_counts=None)),
        files=_Files())


def _openai_body(block):
    return {"status": "completed",
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "output": [{"type": "message", "content": [block]}]}


def test_openai_batch_one_flags_a_refusal_block(monkeypatch):
    import openai
    body = _openai_body({"type": "refusal", "refusal": "I can't help."})
    monkeypatch.setattr(openai, "OpenAI",
                        lambda **kw: _openai_batch_client(body))
    status, resp = llm_batch.openai_batch_collect_one(
        "key", "batch_1", "node-1")
    assert resp["refused"] is True
    assert resp["content"] == ""


def test_openai_batch_one_normal_reply_not_refused(monkeypatch):
    import openai
    body = _openai_body({"type": "output_text", "text": '{"picks": []}'})
    monkeypatch.setattr(openai, "OpenAI",
                        lambda **kw: _openai_batch_client(body))
    status, resp = llm_batch.openai_batch_collect_one(
        "key", "batch_1", "node-1")
    assert resp["refused"] is False
    assert resp["content"] == '{"picks": []}'


def _live_openai_response(block):
    return NS(
        status="completed", incomplete_details=None, id="resp_1",
        output=[NS(type="message", content=[block])],
        usage=NS(input_tokens=4, output_tokens=2, total_tokens=6,
                 input_tokens_details=None))


def _call_live_openai(monkeypatch, response):
    from backend import llm_providers as lp
    monkeypatch.setattr(lp, "_retry_mid_stream",
                        lambda fn, *a, **kw: response)
    monkeypatch.setattr(lp, "OpenAI", lambda **kw: NS())
    return lp.LLMProvider._call_openai(
        "gpt-5", [{"role": "user", "content": "hi"}], "key")


def test_live_openai_flags_a_refusal_block(monkeypatch):
    resp = _call_live_openai(monkeypatch, _live_openai_response(
        NS(type="refusal", refusal="I can't help.")))
    assert resp["refused"] is True
    assert resp["content"] == ""


def test_live_openai_normal_reply_not_refused(monkeypatch):
    resp = _call_live_openai(monkeypatch, _live_openai_response(
        NS(type="output_text", text='{"picks": []}')))
    assert resp["refused"] is False
    assert resp["content"] == '{"picks": []}'


@pytest.mark.parametrize("stop,refused", [("refusal", True),
                                          ("end_turn", False)])
def test_live_anthropic_flags_a_refusal(monkeypatch, stop, refused):
    from backend import llm_providers as lp
    monkeypatch.setattr(lp, "_retry_mid_stream",
                        lambda fn, *a, **kw: _anthropic_message(stop, "x"))
    monkeypatch.setattr(lp, "Anthropic", lambda **kw: NS())
    resp = lp.LLMProvider._call_anthropic(
        "claude-haiku-5-5", [{"role": "user", "content": "hi"}], "key")
    assert resp["refused"] is refused
