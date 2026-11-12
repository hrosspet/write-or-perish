"""Markers in other people's text stay as written through the archive
placeholders of an AI reply's prompt ({user_export}, {user_recent_raw}),
and a cached system render carries the verdict of what its quote passes
resolved.

The export leaves a {quote:ID} / {quote_ext:ID} in text the user did not
write as written (test_quotes_in_others_nodes_export). The prompt then
runs its own quote passes over the message the archive was filled into;
those passes must not resolve such a marker with the user's access
either. And whatever the passes do resolve inside the system message
(the user's own markers) reports to the record the cached render's
verdict is stored from, so a later turn served from the cache goes out
on the key its content allows (#326).

Runs the real task body (test_retrieval_loop harness, scripted provider,
distinct typed keys from test_training_key_payload).
"""
from datetime import datetime, timedelta

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user,
    _resp, _fresh, _mk_artifact, _ScriptedProvider,
)
from backend.tests.test_training_key_payload import (  # noqa: F401
    keys, _keys_used, _payload, _train_chain, _reference, _entry,
)
from backend.tests.test_training_key_own_rows import (
    _FakeRedis, _next_turn, _set_prompt, _train_user,
)
from backend.extensions import db as _db

OLD = datetime(2020, 1, 1)


def _public_thread_with_bobs_reply(alice, bob_text, bob_usage="train"):
    """alice's old public post with bob's public reply *bob_text*."""
    root = _entry(alice, "ALICE POSTS IN PUBLIC", "train",
                  privacy_level="public")
    root.created_at = OLD
    bob = _mk_user("bob", approved=True)
    reply = _entry(bob, bob_text, bob_usage, privacy_level="public")
    reply.parent_id = root.id
    reply.created_at = OLD + timedelta(days=1)
    _db.session.commit()
    return root, reply


def _private_entry_after(alice, node, text):
    """A private entry of alice's written after *node*: outside any
    archive window cut at *node*."""
    private = _entry(alice, text, "train")
    private.created_at = (node.created_at or datetime.utcnow()) + timedelta(
        minutes=5)
    _db.session.commit()
    return private


def _ask(alice, user_node, llm_node, mode="textmode"):
    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=mode)
    assert _fresh(llm_node.id).llm_task_status == "completed"


@pytest.mark.parametrize("placeholder", [
    "{user_export?max_export_tokens=5000}", "{user_recent_raw}",
])
def test_a_reference_in_someone_elses_reply_stays_as_written_through_the_archive(app, keys, placeholder):  # noqa: F811,E501
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    ref = _reference(alice.id, "ALICES SAVED DOCUMENT")
    _, reply = _public_thread_with_bobs_reply(
        alice, "BOB REPLIES {quote_ext:%d}" % ref.id)
    if placeholder == "{user_recent_raw}":
        _set_prompt(system, "system prompt body\n{user_recent_raw}")
    else:
        user_node.set_content("Look back: " + placeholder)
    _db.session.commit()

    _ask(alice, user_node, llm_node)

    sent = _payload(0)
    assert "BOB REPLIES {quote_ext:%d}" % ref.id in sent
    assert "ALICES SAVED DOCUMENT" not in sent
    assert "" not in sent


@pytest.mark.parametrize("placeholder", [
    "{user_export?max_export_tokens=5000}", "{user_recent_raw}",
])
def test_a_private_entry_outside_the_window_stays_as_written_through_the_archive(app, keys, placeholder):  # noqa: F811,E501
    """bob's reply quotes an entry of alice's that the archive window
    leaves out; a quote in alice's own message makes the prompt run its
    quote pass over the filled archive."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    _, reply = _public_thread_with_bobs_reply(alice, "placeholder")
    window_node = user_node if "user_export" in placeholder else system
    private = _private_entry_after(alice, window_node, "ALICES LATER DIARY")
    reply.set_content("BOB REPLIES {quote:%d}" % private.id)
    mine = _entry(alice, "ALICES OWN NOTE", "train")
    text = "Look back at {quote:%d}" % mine.id
    if placeholder == "{user_recent_raw}":
        _set_prompt(system, "system prompt body\n{user_recent_raw}")
    else:
        text += " " + placeholder
    user_node.set_content(text)
    _db.session.commit()

    _ask(alice, user_node, llm_node)

    sent = _payload(0)
    assert "BOB REPLIES {quote:%d}" % private.id in sent
    assert "ALICES LATER DIARY" not in sent
    assert "ALICES OWN NOTE" in sent
    assert "" not in sent


def test_my_own_reference_inside_my_archive_still_resolves(app, keys):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    ref = _reference(alice.id, "ALICES SAVED DOCUMENT")
    note = _entry(alice, "MY NOTE ON {quote_ext:%d}" % ref.id, "train")
    note.created_at = OLD
    user_node.set_content("Look back: {user_export?max_export_tokens=5000}")
    _db.session.commit()

    _ask(alice, user_node, llm_node)

    assert "ALICES SAVED DOCUMENT" in _payload(0)
    assert _keys_used() == ["sk-chat"]          # other people's writing


# ── the cached system render (#192) ──────────────────────────────────────

def _cache(monkeypatch):
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    return fake


def _two_turns(alice, user_node, llm_node, monkeypatch, fake):
    _ask(alice, user_node, llm_node, mode="voice")
    turn_one = _payload(0)
    assert len(fake.store) == 1                 # the render was cached

    def _no_render(*a, **kw):
        raise AssertionError("turn two re-rendered the system prompt")
    monkeypatch.setattr(_llm_task_mod, "get_user_recent_raw_content",
                        _no_render)
    monkeypatch.setattr(_llm_task_mod, "get_user_artifacts_context",
                        _no_render)
    follow, reply2 = _next_turn(alice, llm_node.id)
    _ask(alice, follow, reply2, mode="voice")
    return turn_one, _payload(0)


@pytest.mark.parametrize("bob_usage, expected", [
    ("chat", "sk-chat"),
    ("train", "sk-train"),
])
def test_turn_two_from_the_cache_sends_no_reference_from_someone_elses_reply(app, keys, monkeypatch, bob_usage, expected):  # noqa: F811,E501
    """The thread and the account are on 'train'; the Voice prompt holds
    {user_recent_raw}; bob's reply in alice's thread names a saved
    reference of alice's. The reference is never resolved, on either
    turn, and turn two (served from the cache) goes out on the key its
    content allows: chat with bob's 'chat' reply in it, train only when
    everything in it is licensed for training."""
    fake = _cache(monkeypatch)
    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    ref = _reference(alice.id, "ALICES SAVED DOCUMENT")
    _public_thread_with_bobs_reply(
        alice, "BOB REPLIES {quote_ext:%d}" % ref.id, bob_usage=bob_usage)
    _set_prompt(system, "system prompt body\n{user_recent_raw}")

    turn_one, turn_two = _two_turns(alice, user_node, llm_node,
                                    monkeypatch, fake)

    for sent in (turn_one, turn_two):
        assert "BOB REPLIES {quote_ext:%d}" % ref.id in sent
        assert "ALICES SAVED DOCUMENT" not in sent
    assert _keys_used() == [expected]


def test_turn_two_from_the_cache_keeps_the_verdict_of_what_the_passes_resolved(app, keys, monkeypatch):  # noqa: F811,E501
    """alice's own memory (licensed for training) names her saved
    reference, which the saved-reference pass resolves inside the system
    message: turn one goes out on the chat key, and so does turn two,
    served from the cache with the reference in it."""
    fake = _cache(monkeypatch)
    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    ref = _reference(alice.id, "ALICES SAVED DOCUMENT")
    _mk_artifact(alice.id, "memory", "MEMO {quote_ext:%d}" % ref.id,
                 ai_usage="train")
    _set_prompt(system, "system prompt body\n{user_memory}")
    _db.session.commit()

    _ask(alice, user_node, llm_node, mode="voice")
    assert "ALICES SAVED DOCUMENT" in _payload(0)
    assert _keys_used() == ["sk-chat"]
    assert len(fake.store) == 1

    def _no_render(*a, **kw):
        raise AssertionError("turn two re-rendered the system prompt")
    monkeypatch.setattr(_llm_task_mod, "get_user_artifacts_context",
                        _no_render)
    follow, reply2 = _next_turn(alice, llm_node.id)
    _ask(alice, follow, reply2, mode="voice")
    assert "ALICES SAVED DOCUMENT" in _payload(0)   # served from the cache
    assert _keys_used() == ["sk-chat"]
