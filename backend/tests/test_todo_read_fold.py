"""One read verb for the artifacts index (#218): the todo list is read with
read_artifact on kind 'todo', and read_todo, the todo's own tool before, is
no longer offered.

An older thread still replays. Its prompt names read_todo and its replies
carry read_todo results in tool_calls_meta, but a provider gets tool calls
from earlier turns only as text, never as tool_use / function_call items,
so neither API sees a reference to a tool it was not given. Checked on the
Anthropic and the OpenAI request through the real provider code, with the
SDK clients mocked.
"""
import copy
import json

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user,
    _resp, _fresh, _mk_todo, _build_chain,
)
from backend.tests.test_provider_streaming import (  # noqa: F401 (fixture)
    providers, _client, _message, _oai_client, _oai_response, _event,
)
from backend.extensions import db as _db
from backend.models import Node, User
import backend.utils.prompt_cache as prompt_cache
from backend.utils.prompts import load_default_prompt

# What an older thread's prompt said, before #218.
OLD_PROMPT = (
    "system prompt body\n"
    "The todo list is not shown inline — pull it up with `read_todo` "
    "before proposing changes.\n"
    "<user_artifacts_index>\n"
    "Pull one into context with read_artifact (and the todo list, listed "
    "here as `todo`, with read_todo).\n"
    "{user_artifacts_index}\n"
    "</user_artifacts_index>")

MODELS = {
    "anthropic": {"provider": "anthropic", "api_model": "claude-x"},
    "openai": {"provider": "openai", "api_model": "gpt-5"},
}


def _names(tools):
    return [t["name"] for t in tools]


def test_the_tools_offer_one_read_verb():
    assert "read_artifact" in _names(_llm_task_mod.VOICE_TOOLS)
    assert "read_todo" not in _names(_llm_task_mod.VOICE_TOOLS)
    for config in ({}, {"SHARE_V1": True}, {"SEMANTIC_SEARCH_AGENTIC": False}):
        assert "read_todo" not in _names(
            _llm_task_mod.gated_voice_tools(config))
    read = next(t for t in _llm_task_mod.VOICE_TOOLS
                if t["name"] == "read_artifact")
    assert "the todo list (as 'todo') included" in read["description"]


@pytest.mark.parametrize("prompt_key", ["textmode", "voice"])
def test_the_default_prompt_names_one_read_verb(prompt_key):
    prompt = load_default_prompt(prompt_key)
    assert "read_todo" not in prompt
    assert "`read_artifact` (kind `todo`)" in prompt


class _NoRenderCache:
    """The #192 render cache, empty and kept in memory."""
    def get(self, key):
        return None

    def setex(self, key, ttl, value):
        pass


class _Capture:
    """LLMProvider stand-in that keeps the raw messages and tools of each
    call, for replay through the real provider code."""
    calls = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       **kwargs):
        cls.calls.append({"model_id": model_id,
                          "messages": copy.deepcopy(messages),
                          "tools": copy.deepcopy(tools)})
        reply = _resp("Shipping the fold comes first.")
        reply["cache_comparison_sent"] = False
        return reply


def _old_thread(model_id):
    """A thread from before #218: its prompt names read_todo, and its
    reply called read_todo in a run that left the result for the next
    turn (as a single-shot run does). Returns (alice, follow-up, the
    new reply's placeholder, the old reply, the todo)."""
    alice, system, user_node, old_reply = _build_chain("textmode")
    system.get_artifact("prompt").set_content(OLD_PROMPT)
    todo = _mk_todo(alice.id, "1. ship the fold\n2. water the plants")
    old_reply.set_content("Let me pull up your todo.")
    old_reply.llm_task_status = "completed"
    old_reply.tool_calls_meta = json.dumps([
        {"name": "read_todo", "input": {}, "status": "success",
         "todo_id": todo.id}])
    llm_user = (User.query.filter_by(username=model_id).first()
                or _mk_user(model_id, twitter_id=f"llm-{model_id}"))
    follow = Node(user_id=alice.id, human_owner_id=alice.id,
                  parent_id=old_reply.id, node_type="user",
                  privacy_level="private", ai_usage="chat")
    follow.set_content("and what comes first?")
    _db.session.add(follow)
    _db.session.flush()
    reply = Node(user_id=llm_user.id, human_owner_id=alice.id,
                 parent_id=follow.id, node_type="llm", llm_model=model_id,
                 llm_task_status="pending", privacy_level="private",
                 ai_usage="chat")
    reply.set_content("[LLM response generation pending...]")
    _db.session.add(reply)
    _db.session.commit()
    return alice, follow, reply, old_reply, todo


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_an_older_thread_replays_on_both_providers(app, monkeypatch, providers, provider):  # noqa: F811
    model_id = "claude-x" if provider == "anthropic" else "gpt-5"
    monkeypatch.setitem(app.config["SUPPORTED_MODELS"], model_id,
                        MODELS[provider])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _Capture)
    monkeypatch.setattr(prompt_cache, "_client",
                        lambda config: _NoRenderCache())
    _Capture.calls = []
    alice, follow, reply, old_reply, todo = _old_thread(model_id)

    generate_llm_response(_FakeSelf(), follow.id, reply.id, model_id,
                          alice.id, source_mode="textmode")

    assert _fresh(reply.id).llm_task_status == "completed"
    assert len(_Capture.calls) == 1
    call = _Capture.calls[0]
    assert "read_todo" not in _names(call["tools"])
    texts = "\n".join(b["text"] for m in call["messages"]
                      for b in m["content"])
    # The old reply replays as text, and the todo it read is delivered.
    assert "Let me pull up your todo." in texts
    assert "[Your current todo list:\n1. ship the fold" in texts
    # The fresh index line names no tool of its own.
    assert "- todo — the user's running task list (~" in texts
    # Delivered once: the old result is marked reported.
    meta = json.loads(_fresh(old_reply.id).tool_calls_meta)
    assert meta[0]["status_reported"] is True

    # The same request through the real provider code.
    sent = []
    keys = {"anthropic": "sk-ant", "openai": "sk-oai"}
    if provider == "anthropic":
        monkeypatch.setattr(providers, "Anthropic",
                            _client([_message()], sent))
    else:
        monkeypatch.setattr(providers, "OpenAI", _oai_client([[
            _event("response.completed", response=_oai_response())]], sent))
    providers.LLMProvider.get_completion(
        model_id, call["messages"], keys, tools=call["tools"])
    assert len(sent) == 1
    request = sent[0]
    assert "read_artifact" in _names(request["tools"])
    assert "read_todo" not in _names(request["tools"])
    if provider == "anthropic":
        # Every block is text: no tool_use / tool_result for the API to
        # match against the tool list.
        blocks = [b for m in request["messages"] for b in m["content"]]
        blocks += request["system"]
        assert {b["type"] for b in blocks} == {"text"}
    else:
        # Every input item is a message with text content: no
        # function_call / function_call_output items.
        assert all(set(item) == {"role", "content"}
                   for item in request["input"])
        blocks = [b for item in request["input"] for b in item["content"]]
        assert {b["type"] for b in blocks} <= {"input_text", "output_text"}
        assert all(t["type"] == "function" for t in request["tools"])
