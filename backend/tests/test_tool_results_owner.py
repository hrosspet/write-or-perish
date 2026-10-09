"""Tool results in an AI reply come only from the replying user's own
turns.

A reply run without a mode (single-shot) leaves its retrieval results
(read_artifact, read_todo, read_full, semantic_search) on the node for
the next turn to deliver, and its action outcomes (proposals, artifact
writes) for the next turn to report. The next turn delivers them only
when that node is the replying user's own: a reply asked for by someone
else under it in a public thread gets none of them, and leaves the node's
record untouched. The re-resolution of a retrieval result also checks
the rows against the user the reply is for.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app); nothing
reaches a real model.
"""
import json

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _mk_artifact, _mk_todo, _resp, _fresh,
    _mk_external_item,
)
from backend.extensions import db as _db
from backend.models import Node, NodeEmbedding, User, UserPrompt
from backend.utils.context_artifacts import attach_context_artifacts
from backend.utils.embeddings import pack_vector

ALICE_DATA = ("ALICE MEMORY", "ALICE TODO", "ALICE PRIVATE ENTRY",
              "ALICE SEARCH HIT", "ALICE SAVED REFERENCE")


def _node(user, parent, text, privacy_level="public"):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level=privacy_level, ai_usage="chat")
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _llm(parent, owner, text=None, status="pending"):
    llm_user = User.query.filter_by(username="gpt-5").first()
    n = Node(user_id=llm_user.id, human_owner_id=owner.id,
             parent_id=parent.id, node_type="llm", llm_model="gpt-5",
             llm_task_status=status, privacy_level=parent.privacy_level,
             ai_usage="chat")
    n.set_content(text or "[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _system_node(owner):
    """An agentic (Text mode) system node of *owner*'s, public."""
    prompt = UserPrompt(user_id=owner.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content("SYSTEM PROMPT BODY")
    _db.session.add(prompt)
    _db.session.flush()
    system = Node(user_id=owner.id, human_owner_id=owner.id,
                  node_type="user", privacy_level="public",
                  ai_usage="chat")
    _db.session.add(system)
    _db.session.flush()
    attach_context_artifacts(system.id, owner.id, prompt_record=prompt)
    _db.session.commit()
    return system


@pytest.fixture
def thread(app, monkeypatch):  # noqa: F811
    """alice's public Text-mode thread (system -> entry) with her private
    data (memory, todo, a private entry, a private searchable entry, a
    saved reference), and bob."""
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])
    alice = _mk_user("alice", approved=True, plan="alpha",
                     external_content_enabled=True)
    bob = _mk_user("bob", approved=True, plan="alpha",
                   external_content_enabled=True)
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    memory = _mk_artifact(alice.id, "memory", "ALICE MEMORY")
    todo = _mk_todo(alice.id, "- [ ] ALICE TODO")
    private = _node(alice, None, "ALICE PRIVATE ENTRY",
                    privacy_level="private")
    hit = _node(alice, None, "ALICE SEARCH HIT", privacy_level="private")
    _db.session.add(NodeEmbedding(
        node_id=hit.id, user_id=alice.id, model="test", content_hash="h",
        vector=pack_vector([1.0, 0.0])))
    ref = _mk_external_item(alice.id, "ALICE SAVED REFERENCE", [1.0, 0.0])
    _db.session.commit()
    system = _system_node(alice)
    entry = _node(alice, system, "alice writes in public")
    _db.session.commit()
    return {"alice": alice, "bob": bob, "system": system, "entry": entry,
            "memory": memory, "todo": todo, "private": private, "hit": hit,
            "ref": ref}


def _single_shot_retrievals(t):
    """alice's reply run without a mode: the model calls the four
    retrieval tools; their results wait on the node for the next turn."""
    reply = _llm(t["entry"], t["alice"])
    _ScriptedProvider.reset([
        _resp("Looking those up.", tool_calls=[
            {"id": "t1", "name": "read_artifact",
             "input": {"kind": "memory"}},
            {"id": "t2", "name": "read_todo", "input": {}},
            {"id": "t3", "name": "read_full",
             "input": {"ref": str(t["private"].id)}},
            {"id": "t4", "name": "semantic_search",
             "input": {"query": "zen"}},
        ]),
    ])
    generate_llm_response(_FakeSelf(), t["entry"].id, reply.id, "gpt-5",
                          t["alice"].id, source_mode=None)
    reply = _fresh(reply.id)
    assert reply.llm_task_status == "completed"
    meta = json.loads(reply.tool_calls_meta)
    retrievals = [e for e in meta if e.get("name") in (
        "read_artifact", "read_todo", "read_full", "semantic_search")]
    assert len(retrievals) == 4
    assert all(e["status"] == "success" for e in retrievals)
    assert not any(e.get("status_reported") for e in retrievals)
    return reply


def _ask(parent, user, source_mode="textmode"):
    """*user* asks for a reply under *parent*: their own node, then the
    reply. Returns (the payload's text, the reply node)."""
    mine = _node(user, parent, f"{user.username} asks a question")
    llm_node = _llm(mine, user)
    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), mine.id, llm_node.id, "gpt-5",
                          user.id, source_mode=source_mode)
    assert _fresh(llm_node.id).llm_task_status == "completed"
    payload = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[0]["messages"])
    return payload, llm_node


def test_reply_under_someone_elses_ai_reply_gets_none_of_its_results(thread):
    t = thread
    alice_reply = _single_shot_retrievals(t)
    meta_before = alice_reply.tool_calls_meta

    payload, _ = _ask(alice_reply, t["bob"])

    for marker in ALICE_DATA:
        assert marker not in payload
    # bob's run left alice's record as it was: still hers to deliver.
    assert _fresh(alice_reply.id).tool_calls_meta == meta_before


def test_owner_still_gets_her_results_on_her_next_turn(thread):
    """The owner's own cross-turn delivery is unchanged, also after
    someone else replied under the same node."""
    t = thread
    alice_reply = _single_shot_retrievals(t)
    _ask(alice_reply, t["bob"])

    payload, _ = _ask(alice_reply, t["alice"])

    for marker in ALICE_DATA:
        assert marker in payload
    meta = json.loads(_fresh(alice_reply.id).tool_calls_meta)
    assert all(e.get("status_reported") for e in meta
               if e.get("name") in _llm_task_mod.RETRIEVAL_TOOLS)


def test_bobs_own_results_still_reach_him_in_alices_thread(thread):
    """bob's own single-shot reply in alice's public thread: his next
    turn there gets his results."""
    t = thread
    bob = t["bob"]
    bob_memory = _mk_artifact(bob.id, "memory", "BOB MEMORY")
    _db.session.commit()
    question = _node(bob, t["entry"], "bob asks")
    bob_reply = _llm(question, bob)
    _ScriptedProvider.reset([
        _resp("Looking.", tool_calls=[
            {"id": "t1", "name": "read_artifact",
             "input": {"kind": "memory"}}]),
    ])
    generate_llm_response(_FakeSelf(), question.id, bob_reply.id, "gpt-5",
                          bob.id, source_mode=None)
    assert json.loads(_fresh(bob_reply.id).tool_calls_meta)[0][
        "artifact_id"] == bob_memory.id

    payload, _ = _ask(bob_reply, bob)

    assert "BOB MEMORY" in payload
    assert "ALICE MEMORY" not in payload


def test_no_proposal_or_action_notes_from_someone_elses_node(thread):
    """The outcome notes of alice's proposals and artifact writes are
    hers: bob's reply under her node gets none, and does not mark them
    reported."""
    t = thread
    alice_reply = _llm(t["entry"], t["alice"], "A proposal.",
                       status="completed")
    alice_reply.tool_calls_meta = json.dumps([
        {"name": "propose_todo", "apply_status": "completed"},
        {"name": "propose_github_issue", "apply_status": "started"},
        {"name": "propose_share", "apply_status": "failed",
         "apply_error": "ALICE SHARE ERROR"},
        {"name": "propose_feedback", "apply_status": "completed"},
        {"name": "update_artifact", "status": "success",
         "kind": "alice-kind", "created": False},
    ])
    _db.session.commit()
    meta_before = alice_reply.tool_calls_meta

    payload, _ = _ask(alice_reply, t["bob"])

    assert "applied successfully" not in payload
    assert "merge in progress" not in payload
    assert "ALICE SHARE ERROR" not in payload
    assert "Artifact 'alice-kind' was updated." not in payload
    assert _fresh(alice_reply.id).tool_calls_meta == meta_before

    # alice's own next turn still gets them.
    payload, _ = _ask(alice_reply, t["alice"])
    assert "applied successfully" in payload
    assert "ALICE SHARE ERROR" in payload
    assert "Artifact 'alice-kind' was updated." in payload


# ── Re-resolution checks the rows against the reply's user ──────────────

def test_injection_resolves_rows_only_for_their_owner(thread):
    t = thread
    alice, bob = t["alice"], t["bob"]
    inject = _llm_task_mod._retrieval_injection_text
    entries = {
        "ALICE MEMORY": {"name": "read_artifact", "kind": "memory",
                         "artifact_id": t["memory"].id},
        "ALICE TODO": {"name": "read_todo", "todo_id": t["todo"].id},
        "ALICE PRIVATE ENTRY": {
            "name": "read_full", "kind": "node", "ref_id": t["private"].id,
            "ref": str(t["private"].id), "user_id": alice.id},
        "ALICE SEARCH HIT": {
            "name": "semantic_search", "query": "zen",
            "matches": [{"node_id": t["hit"].id, "score": 0.9}],
            "ext_matches": []},
        "ALICE SAVED REFERENCE": {
            "name": "read_full", "kind": "external", "ref_id": t["ref"].id,
            "ref": "A", "user_id": alice.id},
    }
    for marker, tr in entries.items():
        assert marker in (inject(tr, alice.id) or ""), marker
        assert inject(tr, bob.id) is None, marker
    ext_search = {"name": "semantic_search", "query": "zen", "matches": [],
                  "ext_matches": [{"item_id": t["ref"].id, "score": 0.9}]}
    assert "ALICE SAVED REFERENCE" in inject(ext_search, alice.id)
    assert inject(ext_search, bob.id) is None


def test_search_preview_shows_a_node_the_user_may_open(thread):
    """A match bob may open himself (alice's public entry) still renders
    for him; her private one does not."""
    t = thread
    tr = {"name": "semantic_search", "query": "q", "matches": [
        {"node_id": t["entry"].id, "score": 0.9},
        {"node_id": t["hit"].id, "score": 0.8}], "ext_matches": []}
    text = _llm_task_mod._retrieval_injection_text(tr, t["bob"].id)
    assert "alice writes in public" in text
    assert "ALICE SEARCH HIT" not in text


# ── The history lines of someone else's AI reply ────────────────────────

def test_someone_elses_reply_adds_no_line_from_its_record(thread):
    """alice's AI reply sits in bob's chain with its text only: no outcome
    line for her artifact write, and her reply's mode does not stand in
    for bob's. Proposal tags stay: the proposals are in the reply's text.
    Her own next turn keeps every line."""
    t = thread
    alice_reply = _llm(t["entry"], t["alice"], "Noted. I updated a list.",
                       status="completed")
    alice_reply.tool_calls_meta = json.dumps([
        {"name": "propose_todo", "status": "success",
         "apply_status": "completed", "status_reported": True},
        {"name": "update_artifact", "status": "success",
         "kind": "alice-kind", "created": False, "status_reported": True},
        {"name": "_mode", "source_mode": "textmode"},
    ])
    _db.session.commit()

    payload, _ = _ask(alice_reply, t["bob"])

    assert "alice-kind" not in payload
    assert f"[todo-proposal:{alice_reply.id}]" in payload
    # bob's first Text-mode turn here: the model is told his mode.
    assert "[Mode: Text." in payload

    payload, _ = _ask(alice_reply, t["alice"])
    assert "[update_artifact: artifact 'alice-kind' updated.]" in payload
    assert f"[todo-proposal:{alice_reply.id}]" in payload
    # alice was in Text mode already.
    assert "[Mode: Text." not in payload
