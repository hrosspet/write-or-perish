"""An AI reply in someone else's thread that others can read works with
the thread alone: no tool that reads or writes the requester's own data.

#483 (Peter, 2026-10-09): an AI reply in someone else's public thread
uses no profile. The tools are the same door: semantic_search and
read_full read the requester's archive, read_artifact their artifacts
and todo list, update_artifact writes them, and the apply_* tools act on
the requester's proposals. In a thread whose root or prompt the
requester does not own, for a reply others can read, none of them is
offered, a tool call is not run, earlier results of the requester's own
tools are not delivered again, and the reply makes no proposal. In the
requester's own threads, and for a private reply, nothing changes.

Runs the real task body through the test_retrieval_loop harness; the
people fixture is alice's public Text-mode thread
(test_personal_context_owner).
"""
import json

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.tests.test_personal_context_owner import (  # noqa: F401
    _node, _llm, _system_node, PROMPT,
)
from backend.extensions import db as _db


@pytest.fixture
def thread(app):  # noqa: F811
    """alice's public Text-mode thread, bob's public reply in it, and a
    private entry of bob's elsewhere."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _db.session.commit()
    system = _system_node(alice)
    entry = _node(alice, system, "alice writes in public")
    bobs_private = _node(bob, None, "BOBS PRIVATE DIARY",
                         privacy_level="private")
    bobs_reply = _node(bob, entry, "bob replies")
    _db.session.commit()
    return alice, bob, system, entry, bobs_private, bobs_reply


def _ask(parent, user, responses, privacy=None):
    llm_node = _llm(parent, user)
    if privacy is not None:
        llm_node.privacy_level = privacy
        _db.session.commit()
    _ScriptedProvider.reset(responses)
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          user.id, source_mode="textmode")
    return _fresh(llm_node.id)


def _all_sent():
    return "\n".join(m["text"] for c in _ScriptedProvider.calls
                     for m in c["messages"])


def _offered():
    tools = _ScriptedProvider.calls[0]["tools"] or []
    return [t["name"] for t in tools]


def _read_full(ref):
    return _resp("Let me read that.", tool_calls=[{
        "id": "c1", "name": "read_full", "input": {"ref": str(ref)}}])


def test_a_reply_in_someone_elses_public_thread_is_offered_no_tool(app, thread):  # noqa: F811,E501
    alice, bob, system, entry, bobs_private, bobs_reply = thread

    reply = _ask(bobs_reply, bob, [_resp("An answer.")])

    assert reply.llm_task_status == "completed"
    assert _offered() == []


def test_a_tool_call_there_reads_nothing_of_mine(app, thread):  # noqa: F811
    """Even if the model calls read_full on an id of bob's private entry,
    nothing is read and no second call carries it."""
    alice, bob, system, entry, bobs_private, bobs_reply = thread

    reply = _ask(bobs_reply, bob,
                 [_read_full(bobs_private.id), _resp("Done.")])

    assert reply.llm_task_status == "completed"
    assert len(_ScriptedProvider.calls) == 1
    assert "BOBS PRIVATE DIARY" not in _all_sent()
    meta = json.loads(reply.tool_calls_meta or "[]")
    assert not [e for e in meta if e.get("name") == "read_full"]


def test_earlier_results_of_my_tools_there_are_not_delivered_again(app, thread):  # noqa: F811,E501
    """bob's earlier AI reply in alice's thread read his private entry
    (before this rule); its result is not delivered into his next reply."""
    alice, bob, system, entry, bobs_private, bobs_reply = thread
    earlier = _llm(bobs_reply, bob, "I read it.", status="completed")
    earlier.tool_calls_meta = json.dumps([{
        "name": "read_full", "status": "success", "kind": "node",
        "ref_id": bobs_private.id, "ref": str(bobs_private.id),
        "user_id": bob.id}])
    again = _node(bob, earlier, "bob asks again")
    _db.session.commit()

    reply = _ask(again, bob, [_resp("An answer.")])

    assert reply.llm_task_status == "completed"
    assert "BOBS PRIVATE DIARY" not in _all_sent()


def test_a_reply_there_makes_no_proposal(app, thread):  # noqa: F811
    alice, bob, system, entry, bobs_private, bobs_reply = thread

    reply = _ask(bobs_reply, bob, [_resp(
        "Here is what I'd send.\n\n### Feedback\nThe search is slow.")])

    meta = json.loads(reply.tool_calls_meta or "[]")
    assert not [e for e in meta if e.get("name", "").startswith("propose_")]


# ── where nothing changes ────────────────────────────────────────────────

def _gated_names():
    return [t["name"] for t in _llm_task_mod.gated_voice_tools(
        _llm_task_mod.flask_app.config)]


def test_my_own_public_thread_keeps_every_tool(app, thread):  # noqa: F811
    """bob's own public Text-mode thread, with alice's reply in it."""
    alice, bob, system, entry, bobs_private, bobs_reply = thread
    own_system = _system_node(bob, PROMPT)
    own_entry = _node(bob, own_system, "bob writes in public")
    alices = _node(alice, own_entry, "alice replies to bob")
    mine = _node(bob, alices, "bob answers alice")
    _db.session.commit()

    reply = _ask(mine, bob, [_read_full(bobs_private.id), _resp("Done.")])

    assert reply.llm_task_status == "completed"
    assert _offered() == _gated_names()
    assert "BOBS PRIVATE DIARY" in _all_sent()
    assert len(_ScriptedProvider.calls) == 2


def test_a_private_reply_in_someone_elses_thread_keeps_every_tool(app, thread):  # noqa: F811,E501
    """Only bob can read his private reply: his own data stays his."""
    alice, bob, system, entry, bobs_private, bobs_reply = thread

    reply = _ask(bobs_reply, bob, [_resp("An answer.")], privacy="private")

    assert reply.llm_task_status == "completed"
    assert _offered() == _gated_names()


# ── a prompt edited in its thread ────────────────────────────────────────

def _prompt_under(owner, parent, detach_text=None):
    """A public Text-mode prompt node of *owner*'s under *parent*, linked
    as /textmode/from-node links it. With *detach_text*, edited in its
    thread the way PUT /nodes/<id> with detach_prompt does it: the key
    stamped, the prompt link removed, the node's own text in its place."""
    from backend.models import NodeContextArtifact, UserPrompt
    from backend.utils.context_artifacts import attach_context_artifacts
    prompt = UserPrompt(user_id=owner.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content(PROMPT)
    _db.session.add(prompt)
    _db.session.flush()
    node = _node(owner, parent, "")
    attach_context_artifacts(node.id, owner.id, prompt_record=prompt)
    _db.session.flush()
    if detach_text is not None:
        if not node.prompt_key:
            node.prompt_key = node.get_artifact("prompt").prompt_key
        NodeContextArtifact.query.filter_by(
            node_id=node.id, artifact_type="prompt").delete()
        node.set_content(detach_text)
    _db.session.commit()
    _db.session.expire_all()
    return _fresh(node.id)


@pytest.mark.parametrize("detached", [True, False])
def test_someone_elses_prompt_in_my_thread_restricts_my_reply_below_it(app, thread, detached):  # noqa: F811,E501
    """bob's public entry (no prompt); alice replies and attaches her
    Text-mode prompt under her reply, edited in the thread (detached)
    or not; bob replies below it in public and asks for a reply."""
    alice, bob, system, entry, bobs_private, bobs_reply = thread
    bobs_root = _node(bob, None, "bob writes in public")
    alices = _node(alice, bobs_root, "alice replies")
    prompt = _prompt_under(alice, alices,
                           "ALICE'S OWN PROMPT" if detached else None)
    assert prompt.get_prompt_key() == "textmode"
    assert prompt.has_artifact("prompt") is not detached
    below = _node(bob, prompt, "bob replies below her prompt")
    _db.session.commit()

    reply = _ask(below, bob, [_read_full(bobs_private.id), _resp("Done.")])

    assert reply.llm_task_status == "completed"
    assert _offered() == []
    assert len(_ScriptedProvider.calls) == 1
    assert "BOBS PRIVATE DIARY" not in _all_sent()


# ── the closing note ─────────────────────────────────────────────────────

def _last_message():
    return _ScriptedProvider.calls[0]["messages"][-1]["text"]


def test_the_restricted_reply_is_told_it_has_no_tools(app, thread):  # noqa: F811
    alice, bob, system, entry, bobs_private, bobs_reply = thread

    _ask(bobs_reply, bob, [_resp("An answer.")])

    assert _llm_task_mod.OTHERS_THREAD_NOTE in _last_message()
    assert _all_sent().count(_llm_task_mod.OTHERS_THREAD_NOTE) == 1


def test_my_own_thread_and_a_private_reply_get_no_such_note(app, thread):  # noqa: F811,E501
    alice, bob, system, entry, bobs_private, bobs_reply = thread

    _ask(bobs_reply, bob, [_resp("An answer.")], privacy="private")
    assert _llm_task_mod.OTHERS_THREAD_NOTE not in _all_sent()

    own_system = _system_node(bob, PROMPT)
    own_entry = _node(bob, own_system, "bob writes in public")
    alices = _node(alice, own_entry, "alice replies to bob")
    mine = _node(bob, alices, "bob answers alice")
    _db.session.commit()
    _ask(mine, bob, [_resp("An answer.")])
    assert _llm_task_mod.OTHERS_THREAD_NOTE not in _all_sent()
