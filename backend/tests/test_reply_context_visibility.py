"""An AI reply's context holds only the thread content the requesting user
can see.

The reply task loads the requester's node and its ancestors and sends their
text to the model. If a node of bob's hangs below a private node of
alice's (e.g. created before parents were checked), the model must still
see only bob's part of the thread. A public thread keeps its full context.

Runs the real task body through the test_retrieval_loop harness (identity
@celery.task, scripted provider, test flask_app); nothing reaches a real
model.
"""
import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import Node


def _node(user, parent, text, privacy_level="private", human_owner=None,
          node_type="user"):
    n = Node(user_id=user.id, human_owner_id=(human_owner or user).id,
             parent_id=parent.id if parent else None, node_type=node_type,
             privacy_level=privacy_level, ai_usage="chat")
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _placeholder(llm_user, parent, owner):
    n = Node(user_id=llm_user.id, human_owner_id=owner.id,
             parent_id=parent.id, node_type="llm", llm_model="gpt-5",
             llm_task_status="pending", privacy_level=parent.privacy_level,
             ai_usage="chat")
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _sent_text():
    assert len(_ScriptedProvider.calls) == 1
    return "\n".join(m["text"] for m in _ScriptedProvider.calls[0]["messages"])


def test_reply_context_leaves_out_ancestors_the_requester_cannot_see(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    root = _node(alice, None, "ALICE ROOT ENTRY")
    mid = _node(alice, root, "ALICE SECOND ENTRY")
    bob_node = _node(bob, mid, "bob asks a question")
    llm_node = _placeholder(llm_user, bob_node, bob)

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), bob_node.id, llm_node.id, "gpt-5",
                          bob.id)

    sent = _sent_text()
    assert "bob asks a question" in sent
    assert "ALICE" not in sent
    assert _fresh(llm_node.id).get_content() == "An answer."


def test_reply_context_keeps_a_public_thread(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    post = _node(alice, None, "ALICE PUBLIC POST", privacy_level="public")
    bob_reply = _node(bob, post, "bob public reply", privacy_level="public")
    llm_node = _placeholder(llm_user, bob_reply, bob)

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), bob_reply.id, llm_node.id, "gpt-5",
                          bob.id)

    sent = _sent_text()
    assert "ALICE PUBLIC POST" in sent
    assert "bob public reply" in sent


def test_owner_reply_context_keeps_the_whole_private_thread(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    root = _node(alice, None, "ALICE ROOT ENTRY")
    earlier = _node(llm_user, root, "AN EARLIER REPLY", human_owner=alice,
                    node_type="llm")
    latest = _node(alice, earlier, "ALICE FOLLOW-UP")
    llm_node = _placeholder(llm_user, latest, alice)

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), latest.id, llm_node.id, "gpt-5",
                          alice.id)

    sent = _sent_text()
    for text in ("ALICE ROOT ENTRY", "AN EARLIER REPLY", "ALICE FOLLOW-UP"):
        assert text in sent
