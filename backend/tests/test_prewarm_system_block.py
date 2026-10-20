"""The voice pre-warm and generation send the same system block, with no
trailing whitespace (#187).

The ongoing-thread warm sends the system block as the last content of its
request, and Anthropic drops trailing whitespace there; generation follows
the block with the first message, where it stays. A render ending in a
newline left the warm's cache entry one token short of the position
generation looks up, so every ongoing-thread warm missed (prod, 2026-10).
"""
import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _resp,
    _ScriptedProvider,
)
from backend.tests.test_training_key_payload import (  # noqa: F401
    keys, _train_chain,
)
from backend.tests.test_training_key_own_rows import (
    _FakeRedis, _set_prompt, _train_user,
)

BODY = "system prompt body"


def _setup(app, monkeypatch, prompt):  # noqa: F811
    """A train user's voice thread with *prompt*; returns the thread and
    the list the warm's request messages land in."""
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY_TRAIN", "ant-train")
    monkeypatch.setitem(app.config, "SUPPORTED_MODELS", {
        **app.config["SUPPORTED_MODELS"],
        "claude-test": {"provider": "anthropic", "api_model": "claude-x"},
    })
    sent = []

    def _call_anthropic(api_model, messages, api_key, **kw):
        sent.append(messages)
        raise RuntimeError("stop after capturing the request")
    monkeypatch.setattr(_ScriptedProvider, "_call_anthropic",
                        staticmethod(_call_anthropic), raising=False)

    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    _set_prompt(system, prompt)
    return alice, system, user_node, llm_node, sent


def _warm_system_block(sent):
    return sent[-1][0]["content"][0]["text"]


def _reply_system_block():
    return next(m["text"] for m in _ScriptedProvider.calls[-1]["messages"]
                if BODY in m["text"])


def _reply(alice, user_node, llm_node):
    _ScriptedProvider.reset([_resp("Reply.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="voice")


@pytest.mark.parametrize("prompt", [
    BODY, BODY + "\n", BODY + "\n\n  \n",
])
def test_a_fresh_thread_warm_and_its_reply_send_the_same_block(app, keys, monkeypatch, prompt):  # noqa: F811
    """The warm renders first and caches; the reply reuses its bytes."""
    alice, system, user_node, llm_node, sent = _setup(
        app, monkeypatch, prompt)

    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")
    _reply(alice, user_node, llm_node)

    assert _reply_system_block() == _warm_system_block(sent)
    assert _warm_system_block(sent).endswith(BODY)


def test_an_ongoing_thread_warm_sends_the_block_the_reply_rendered(app, keys, monkeypatch):  # noqa: F811
    """The reply renders first and caches; the later system-only warm
    reuses its bytes, which must not end in whitespace."""
    alice, system, user_node, llm_node, sent = _setup(
        app, monkeypatch, BODY + "\n")

    _reply(alice, user_node, llm_node)
    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")

    assert _warm_system_block(sent) == _reply_system_block()
    assert _warm_system_block(sent).endswith(BODY)
