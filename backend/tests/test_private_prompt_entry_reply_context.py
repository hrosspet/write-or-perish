"""What an AI reply is built from in a public thread whose prompt entry
is private.

The thread: the owner's private prompt entry (Text mode), her public
first message under it, an AI reply. Her own replies read the prompt
entry and its pinned versions as before, with the same text as when the
entry was public (same position in the chain, same bytes). A reply
another member asks for in the thread starts below the prompt entry:
it reads the public messages and runs as a reply in a thread without a
Text-mode prompt (no system block of the owner's, no Text-mode tools).
When the prompt entry sits inside a public thread (Text mode continued
under a public entry of the owner's), such a reply also reads the
public messages above the prompt entry, and never the entry.

Runs the real task body through the test_retrieval_loop harness;
nothing reaches a real model.
"""
from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _ScriptedProvider,
)
from backend.tests.test_personal_context_owner import (  # noqa: F401
    BODY, _ask, _markers, _node, _sent_text, _system_block, _system_node,
    _llm, _seed_personal_data, fake_redis, people,
)
from backend.extensions import db as _db


def _thread(alice, privacy_level):
    system = _system_node(alice, privacy_level=privacy_level)
    entry = _node(alice, system, "alice writes in public")
    reply = _llm(entry, alice, "An earlier answer.", status="completed")
    return system, entry, reply


def test_the_owners_reply_reads_her_private_prompt_entry(app, people):  # noqa: F811
    alice, bob, *_ = people
    system, entry, reply = _thread(alice, "private")
    more = _node(alice, reply, "alice continues")

    _ask(more, alice)

    block = _system_block()
    assert BODY in block
    for marker in _markers("ALICE"):
        assert marker in _sent_text(), marker
    assert _ScriptedProvider.calls[-1].get("tools")


def test_the_owners_prompt_text_is_the_same_at_either_level(app, people):  # noqa: F811
    """The level changes nothing the model is sent for the owner: the
    same thread asked once with its prompt entry public and once with it
    private gives the same system block, first in the chain."""
    alice, bob, *_ = people
    system, entry, reply = _thread(alice, "public")
    tips = [_node(alice, reply, "alice continues", privacy_level="private")
            for _ in range(2)]
    blocks = []
    for level, tip in zip(("public", "private"), tips):
        system.set_privacy_level(level)
        _db.session.commit()
        _ask(tip, alice)
        assert BODY in _ScriptedProvider.calls[-1]["messages"][0]["text"]
        blocks.append(_system_block())
    assert "ALICE PROFILE" in blocks[0]
    assert blocks[0] == blocks[1]


def test_the_owners_cached_render_is_used_again(app, people, fake_redis):  # noqa: F811
    alice, bob, *_ = people
    system, entry, reply = _thread(alice, "private")
    _ask(_node(alice, reply, "alice continues"), alice)
    first = _system_block()
    stored = dict(fake_redis.store)
    assert stored

    _ask(_node(alice, reply, "alice once more"), alice)

    assert _system_block() == first
    for key, value in stored.items():
        assert fake_redis.store[key] == value


def test_another_members_reply_starts_below_the_private_prompt_entry(app, people):  # noqa: F811
    alice, bob, *_ = people
    system, entry, reply = _thread(alice, "private")
    question = _node(bob, reply, "bob asks a question")

    llm_node = _ask(question, bob)

    _db.session.refresh(llm_node)
    assert llm_node.llm_task_status == "completed"
    assert llm_node.get_content() == "An answer."
    sent = _sent_text()
    assert "alice writes in public" in sent
    assert "An earlier answer." in sent
    assert "bob asks a question" in sent
    assert BODY not in sent
    for marker in _markers("ALICE"):
        assert marker not in sent, marker
    # No Text-mode prompt in what he can read: the reply has no tools,
    # as in any thread that was not started in Text mode.
    assert not _ScriptedProvider.calls[-1].get("tools")


def test_another_members_reply_in_a_thread_with_a_public_prompt_entry_is_as_before(app, people):  # noqa: F811
    """An entry its owner has set public is read by another member's
    reply as before: her text with the personal parts empty."""
    alice, bob, *_ = people
    system, entry, reply = _thread(alice, "public")
    question = _node(bob, reply, "bob asks a question")

    _ask(question, bob)

    sent = _sent_text()
    assert BODY in sent
    for marker in _markers("ALICE"):
        assert marker not in sent, marker


def _thread_with_a_prompt_entry_inside(alice):
    """alice's public entry, her private prompt entry under it (Text
    mode continued there), her public message under that, an AI reply."""
    top = _node(alice, None, "alice's public entry above")
    system = _system_node(alice, privacy_level="private")
    system.parent_id = top.id
    _db.session.commit()
    entry = _node(alice, system, "alice writes in public")
    reply = _llm(entry, alice, "An earlier answer.", status="completed")
    return top, system, entry, reply


def test_another_members_reply_reads_the_messages_above_a_prompt_entry_inside_the_thread(app, people):  # noqa: F811
    alice, bob, *_ = people
    top, system, entry, reply = _thread_with_a_prompt_entry_inside(alice)
    question = _node(bob, reply, "bob asks a question")

    llm_node = _ask(question, bob)

    _db.session.refresh(llm_node)
    assert llm_node.llm_task_status == "completed"
    sent = _sent_text()
    order = [sent.index(text) for text in (
        "alice's public entry above", "alice writes in public",
        "An earlier answer.", "bob asks a question")]
    assert order == sorted(order)
    assert BODY not in sent
    for marker in _markers("ALICE"):
        assert marker not in sent, marker
    assert not _ScriptedProvider.calls[-1].get("tools")
    # The rule that refuses a reply over a node that keeps AI out walks
    # the same chain.
    from backend.utils.llm_nodes import reply_refusal
    assert reply_refusal(question, bob.id, "chat") is None
    top.ai_usage = "none"
    _db.session.commit()
    assert reply_refusal(question, bob.id, "chat") is not None


def test_a_private_node_that_is_no_prompt_entry_still_ends_the_chain(app, people):  # noqa: F811
    alice, bob, *_ = people
    top = _node(alice, None, "alice's public entry above")
    note = _node(alice, top, "alice's private note", privacy_level="private")
    entry = _node(alice, note, "alice writes in public")
    question = _node(bob, entry, "bob asks a question")

    _ask(question, bob)

    sent = _sent_text()
    assert "alice writes in public" in sent
    assert "alice's private note" not in sent
    assert "alice's public entry above" not in sent


def test_the_owners_reply_reads_her_prompt_entry_inside_the_thread(app, people):  # noqa: F811
    alice, bob, *_ = people
    top, system, entry, reply = _thread_with_a_prompt_entry_inside(alice)

    _ask(_node(alice, reply, "alice continues"), alice)

    sent = _sent_text()
    assert BODY in sent and "alice's public entry above" in sent
    assert "ALICE PROFILE" in sent
    assert _ScriptedProvider.calls[-1].get("tools")
