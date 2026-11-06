"""A deleted artifact leaves the index the model receives (#603).

Peter, 2026-11-06: "deletion of an artifact needs to remove it also from
the index of artifacts that LLMs read". These tests build the session
instructions the way a reply and the voice pre-warm build them, with the
#192 render cache on (a fake Redis), and check that a delete takes the
artifact out of the very next payload, including when the render from
before the delete is still cached.

The harness is test_retrieval_loop.py's (real generation loop, scripted
provider) and test_prewarm_wait.py's (Anthropic pre-warm).
"""
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _build_chain,
    _mk_artifact, _resp, _ScriptedProvider,
)
from backend.tests.test_prewarm_wait import (  # noqa: F401 (fixture)
    _FakeRedis, anthropic_model,
)
from backend.extensions import db as _db
from backend.models import Node
from backend.utils.artifact_deletion import soft_delete_artifact
from backend.utils.context_artifacts import sync_context_artifacts
import backend.utils.prompt_cache as prompt_cache

PROMPT = "You are Loore.\n\nArtifacts:\n{user_artifacts_index}"


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    return fake


def _render_keys(redis):
    return sorted(k for k in redis.store
                  if k.startswith(prompt_cache._KEY_PREFIX))


def _session(source_mode):
    """An agentic session whose instructions carry the artifacts index,
    its artifacts pinned at the start as attach_context_artifacts pins
    them: a custom artifact the user will delete, and one they keep."""
    alice, system, user_node, llm_node = _build_chain(source_mode)
    prompt = system.get_artifact("prompt")
    prompt.set_content(PROMPT)
    system.set_content(PROMPT)
    _mk_artifact(alice.id, "landlord-case", "the landlord notes",
                 title="Landlord Dispute")
    _mk_artifact(alice.id, "reading-list", "1. Dune", title="Reading List")
    _db.session.flush()
    sync_context_artifacts(system.id, alice.id, PROMPT)
    _db.session.commit()
    return alice, system, user_node, llm_node


def _system_text(call):
    texts = [m["text"] for m in call["messages"] if "Artifacts:" in m["text"]]
    assert len(texts) == 1, call["messages"]
    return texts[0]


def _turn(alice, parent, source_mode):
    """One reply under *parent*; returns the system text it sent."""
    user_node = Node(user_id=alice.id, human_owner_id=alice.id,
                     parent_id=parent.id, node_type="user",
                     privacy_level="private", ai_usage="chat")
    user_node.set_content("and now?")
    _db.session.add(user_node)
    _db.session.flush()
    llm_user = _db.session.query(type(alice)).filter_by(
        username="gpt-5").first()
    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=user_node.id, node_type="llm",
                    llm_model="gpt-5", llm_task_status="pending",
                    privacy_level="private", ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.commit()
    _ScriptedProvider.reset([_resp("Noted.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=source_mode)
    return _system_text(_ScriptedProvider.calls[0]), llm_node


@pytest.mark.parametrize("source_mode", ["textmode", "voice"])
def test_deleted_artifact_leaves_the_index_of_the_next_reply(
        app, redis, source_mode):  # noqa: F811
    """The session's instructions (Text and Voice mode) list the
    artifact before the delete and not after it, although the render
    from before the delete is still in the cache."""
    alice, system, user_node, llm_node = _session(source_mode)

    _ScriptedProvider.reset([_resp("Hello.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=source_mode)
    before = _system_text(_ScriptedProvider.calls[0])
    assert "landlord-case" in before and "Landlord Dispute" in before
    assert "reading-list" in before
    cached_before = _render_keys(redis)
    assert len(cached_before) == 1  # the render is cached

    # Without a delete the next reply reuses the cached render.
    again, last = _turn(alice, llm_node, source_mode)
    assert again == before
    assert _render_keys(redis) == cached_before

    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()

    after, _ = _turn(alice, last, source_mode)
    assert "landlord-case" not in after
    assert "Landlord Dispute" not in after
    assert "reading-list" in after
    # A fresh key: the render from before the delete is never served.
    assert len(_render_keys(redis)) == 2


def test_render_variant_moves_on_a_delete(app):  # noqa: F811
    """The #192 cache key (the variant) of a user who never deleted an
    artifact is what it was; each delete moves it."""
    alice, *_ = _session("textmode")
    plain = _llm_task_mod._render_variant(alice.id)
    assert "d" not in plain.split("e")[-1]
    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime(2026, 11, 6, 9, 0, 0, 1))
    _db.session.commit()
    first = _llm_task_mod._render_variant(alice.id)
    assert first != plain and first.startswith(plain)
    soft_delete_artifact(alice.id, "reading-list",
                         now=datetime(2026, 11, 6, 9, 0, 0, 2))
    _db.session.commit()
    assert _llm_task_mod._render_variant(alice.id) not in (plain, first)


def test_render_system_message_leaves_it_out(app):  # noqa: F811
    """render_system_message is the pre-warm's render (byte-identical to
    the reply's): after the delete it lists the artifact no more."""
    alice, system, *_ = _session("voice")
    assert "landlord-case" in _llm_task_mod.render_system_message(
        system, alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    _db.session.expire_all()
    text = _llm_task_mod.render_system_message(
        Node.query.get(system.id), alice.id)
    assert "landlord-case" not in text
    assert "reading-list" in text


def _record_anthropic(monkeypatch):
    sent = []

    def _call_anthropic(api_model, messages, api_key, **kw):
        sent.append(messages)
        return {"content": "", "input_tokens": 3, "output_tokens": 1,
                "cache_creation_input_tokens": 1000}
    monkeypatch.setattr(_ScriptedProvider, "_call_anthropic",
                        staticmethod(_call_anthropic), raising=False)
    return sent


def _warmed_system_text(messages):
    for m in messages:
        content = m["content"]
        text = ("".join(b.get("text", "") for b in content)
                if isinstance(content, list) else content)
        if "Artifacts:" in text:
            return text
    raise AssertionError(messages)


def test_a_warm_from_before_the_delete_is_not_served(
        app, redis, anthropic_model, monkeypatch):  # noqa: F811
    """The voice pre-warm stores its render in the #192 cache for the
    reply to reuse. A warm from before the delete must not reach the
    reply after it, and a warm after the delete leaves the artifact out."""
    sent = _record_anthropic(monkeypatch)
    alice, system, user_node, llm_node = _session("voice")

    result = _llm_task_mod.prewarm_anthropic_cache(
        system.id, alice.id, "claude-test")
    assert result["status"] == "ok"
    assert "landlord-case" in _warmed_system_text(sent[-1])
    assert len(_render_keys(redis)) == 1

    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()

    # The reply after the delete renders afresh.
    _ScriptedProvider.reset([_resp("Hello.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="voice")
    reply_text = _system_text(_ScriptedProvider.calls[0])
    assert "landlord-case" not in reply_text
    assert "reading-list" in reply_text

    # And the next warm leaves it out too.
    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")
    assert "landlord-case" not in _warmed_system_text(sent[-1])
