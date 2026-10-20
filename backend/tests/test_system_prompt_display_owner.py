"""A node's pinned personal context is shown to its owner only.

A Text-mode system prompt pins its owner's profile, memory, intentions
and the rest, and the thread page shows them where the placeholders sit.
In a public thread other people see the prompt; the pinned versions and
the recent-writing range stay the owner's. The same holds for an AI
reply that pinned the todo list it read.

Reuses the minimal app of test_node_detail_ownership (sqlite, real
flask_login, nodes blueprint).
"""
import json

from backend.tests.test_node_detail_ownership import (  # noqa: F401
    app, alice, bob, llm_user, _login, _make_node,
)
from backend.extensions import db as _db
from backend.models import (
    NodeContextArtifact, UserArtifact, UserProfile, UserPrompt, UserTodo,
)
from backend.utils.context_artifacts import attach_context_artifacts

PROMPT = "SYSTEM PROMPT BODY\n{user_profile}\n{user_memory}\n{user_recent_raw}"
ALICE_PRIVATE = ("ALICE PROFILE", "ALICE MEMORY", "ALICE TODO")


def _seed(user, name):
    profile = UserProfile(user_id=user.id, generated_by="test",
                          ai_usage="chat")
    profile.set_content(f"{name} PROFILE")
    memory = UserArtifact(user_id=user.id, kind="memory", title="Memory",
                          generated_by="test", ai_usage="chat")
    memory.set_content(f"{name} MEMORY")
    todo = UserTodo(user_id=user.id, generated_by="test", ai_usage="chat")
    todo.set_content(f"- [ ] {name} TODO")
    _db.session.add_all([profile, memory, todo])
    _db.session.commit()
    return todo


def _public_text_thread(owner):
    prompt = UserPrompt(user_id=owner.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content(PROMPT)
    _db.session.add(prompt)
    _db.session.flush()
    system = _make_node(owner, content="")
    attach_context_artifacts(system.id, owner.id, prompt_record=prompt)
    _db.session.commit()
    entry = _make_node(owner, parent=system, content="alice writes")
    return system, entry


def _get(app, user, node_id):  # noqa: F811
    client = app.test_client()
    _login(client, user)
    resp = client.get(f"/nodes/{node_id}")
    assert resp.status_code == 200
    return resp.json


def _assert_none_of_alices(payload):
    text = json.dumps(payload)
    for marker in ALICE_PRIVATE:
        assert marker not in text, marker


def test_someone_else_sees_the_prompt_but_not_the_owners_pinned_context(app, alice, bob):  # noqa: F811
    _seed(alice, "ALICE")
    system, entry = _public_text_thread(alice)

    data = _get(app, bob, system.id)

    assert "SYSTEM PROMPT BODY" in data["content"]
    artifacts = data["context_artifacts"]
    assert artifacts["prompt"]["title"] == "Text"
    for key in ("profile", "memory", "recent_raw"):
        assert key not in artifacts, key
    _assert_none_of_alices(data)


def test_the_owners_pinned_context_stays_off_ancestors_and_children(app, alice, bob, llm_user):  # noqa: F811
    todo = _seed(alice, "ALICE")
    system, entry = _public_text_thread(alice)
    bob_reply = _make_node(bob, parent=entry, content="bob replies")
    alice_back = _make_node(alice, parent=bob_reply, content="alice again")
    # alice's AI reply pinned the todo list it read (read_todo).
    alice_ai = _make_node(llm_user, parent=alice_back, node_type="llm",
                          human_owner_id=alice.id, content="her answer")
    _db.session.add(NodeContextArtifact(
        node_id=alice_ai.id, artifact_type="todo", artifact_id=todo.id))
    _db.session.commit()

    _assert_none_of_alices(_get(app, bob, bob_reply.id))
    _assert_none_of_alices(_get(app, bob, entry.id))


def test_the_owner_still_sees_their_pinned_context(app, alice, bob):  # noqa: F811
    _seed(alice, "ALICE")
    system, entry = _public_text_thread(alice)

    data = _get(app, alice, system.id)

    artifacts = data["context_artifacts"]
    assert artifacts["profile"]["content"] == "ALICE PROFILE"
    assert artifacts["memory"]["content"] == "ALICE MEMORY"
    assert "prompt" in artifacts
