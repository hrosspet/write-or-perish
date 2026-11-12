"""Write tool calls wait for the user's approval when the reply's history
holds text they did not write (Peter, 2026-11-12).

"direct writing to artifacts should be in the case when other user's text
is in the context, intercepted and via a dialog raised to the user. It
should [show] what tool call is proposed, and show the proposed diff if
it's about changing an artifact. The user would have to approve every
single tool call in such a thread. If more the one such possibly
destructive tool call is made in a single node, all of them should be
raised inside one dialog"

"let's keep the confirmation dialog only for nodes directly in history,
including quotes of public / circles nodes in private entries"

Runs the real task body through the test_retrieval_loop harness.
"""
import json
import sys

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _mk_artifact, _resp, _fresh,
)
from backend.tests.test_personal_context_owner import (  # noqa: F401
    _node, _llm, _system_node,
)
from backend.extensions import db as _db
from backend.models import Draft, ExternalItem, UserArtifact


@pytest.fixture
def task_module(monkeypatch):
    """The approval code imports the task module lazily: the harness's."""
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    return _llm_task_mod


@pytest.fixture
def people(app):  # noqa: F811
    ben = _mk_user("ben", approved=True, plan="alpha")
    anna = _mk_user("anna", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _db.session.commit()
    return ben, anna


def _memory(user, text="- likes tea"):
    row = _mk_artifact(user.id, "memory", text, title="Memory")
    _db.session.commit()
    return row


def _latest_memory(user):
    _db.session.expire_all()
    row = UserArtifact.latest_for(user.id, "memory")
    return row.get_content() if row else None


def _write_memory(text, base_version=1, call_id="w1"):
    return {"id": call_id, "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": text,
                      "base_version": base_version}}


def _ask(parent, user, responses):
    llm_node = _llm(parent, user)
    llm_node.privacy_level = "private"
    _db.session.commit()
    _ScriptedProvider.reset(responses)
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          user.id, source_mode="textmode")
    return _fresh(llm_node.id)


def _held(user):
    _db.session.expire_all()
    return Draft.query.filter_by(user_id=user.id,
                                 label="tool_pending").all()


def _entries(node, name="update_artifact"):
    meta = json.loads(_fresh(node.id).tool_calls_meta or "[]")
    return [e for e in meta if e.get("name") == name]


def _second_call_text():
    return "\n".join(m["text"] for m in _ScriptedProvider.calls[1]["messages"])


def _thread_with_annas_reply(ben, anna):
    """Ben's own public Text-mode thread with Anna's public reply and
    Ben's answer to her."""
    system = _system_node(ben)
    root = _node(ben, system, "ben thinks out loud")
    annas = _node(anna, root, "anna replies")
    bens = _node(ben, annas, "ben answers anna")
    _db.session.commit()
    return bens


# ── The reply with someone else's text in its history ──────────────────


def test_a_memory_write_with_someone_elses_reply_in_history_waits(app, people):
    """Anna's reply in Ben's thread, then Ben's private AI reply calls
    update_artifact on his memory: nothing is written, the call waits."""
    ben, anna = people
    _memory(ben)
    bens = _thread_with_annas_reply(ben, anna)

    reply = _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _write_memory("- likes tea\n- trusts anna")]),
        _resp("I proposed a change to your memory."),
    ])

    assert reply.llm_task_status == "completed"
    assert _latest_memory(ben) == "- likes tea"
    [held] = _held(ben)
    assert held.parent_id == reply.id
    [entry] = _entries(reply)
    assert entry["status"] == "pending_approval"
    assert entry["pending_id"] == held.id
    assert "trusts anna" not in reply.tool_calls_meta
    assert "waits for their approval" in _second_call_text()
    assert "stays at v1" in _second_call_text()


def test_approving_writes_it_then(app, people, task_module):
    from backend.utils.tool_approvals import decide
    ben, anna = people
    _memory(ben)
    bens = _thread_with_annas_reply(ben, anna)
    reply = _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _write_memory("- likes tea\n- trusts anna")]),
        _resp("Proposed."),
    ])
    [held] = _held(ben)

    outcome, result = decide(held, ben.id, approve=True)
    _db.session.commit()

    assert outcome == "approved"
    assert _latest_memory(ben) == "- likes tea\n- trusts anna"
    assert _held(ben) == []
    [entry] = _entries(reply)
    assert entry["status"] == "success"
    assert entry["approval"] == "approved"
    assert entry["status_reported"] is False


def test_rejecting_changes_nothing(app, people, task_module):
    from backend.utils.tool_approvals import decide
    ben, anna = people
    _memory(ben)
    bens = _thread_with_annas_reply(ben, anna)
    reply = _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _write_memory("- likes tea\n- trusts anna")]),
        _resp("Proposed."),
    ])
    [held] = _held(ben)

    outcome, _ = decide(held, ben.id, approve=False)
    _db.session.commit()

    assert outcome == "rejected"
    assert _latest_memory(ben) == "- likes tea"
    assert UserArtifact.query.filter_by(user_id=ben.id).count() == 1
    assert _held(ben) == []
    [entry] = _entries(reply)
    assert entry["status"] == "rejected"


def test_the_next_turn_is_told_the_outcome_once(app, people, task_module):
    from backend.utils.tool_approvals import decide
    ben, anna = people
    _memory(ben)
    bens = _thread_with_annas_reply(ben, anna)
    reply = _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _write_memory("- likes tea\n- trusts anna")]),
        _resp("Proposed."),
    ])
    decide(_held(ben)[0], ben.id, approve=False)
    _db.session.commit()
    answer = _fresh(reply.continuation_node_id)
    again = _node(ben, answer, "did you note it?")
    _db.session.commit()

    _ask(again, ben, [_resp("No, you declined it.")])

    sent = "\n".join(m["text"] for m in _ScriptedProvider.calls[0]["messages"])
    assert "The user declined it; nothing was done." in sent


def test_a_write_approved_after_the_artifact_changed_saves_nothing(
        app, people, task_module):
    from backend.utils.tool_approvals import decide, serialize_pending
    ben, anna = people
    _memory(ben)
    bens = _thread_with_annas_reply(ben, anna)
    _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _write_memory("- likes tea\n- trusts anna")]),
        _resp("Proposed."),
    ])
    # Ben edits his memory by hand meanwhile.
    _mk_artifact(ben.id, "memory", "- likes coffee", title="Memory")
    _db.session.commit()
    [held] = _held(ben)
    assert serialize_pending(held, ben.id)["stale"] is True

    outcome, result = decide(held, ben.id, approve=True)
    _db.session.commit()

    assert outcome == "failed"
    assert "changed since" in result["error"]
    assert _latest_memory(ben) == "- likes coffee"


def test_several_writes_of_one_reply_wait_together(app, people, task_module):
    """Two writes in one reply: two items, each with its diff, grouped
    under the reply's first node (one dialog)."""
    from backend.utils.tool_approvals import pending_for_user
    ben, anna = people
    _memory(ben)
    _mk_artifact(ben.id, "scratchpad", "notes", title="Scratchpad")
    _db.session.commit()
    bens = _thread_with_annas_reply(ben, anna)

    reply = _ask(bens, ben, [
        _resp("Noting.", tool_calls=[
            _write_memory("- likes tea\n- trusts anna"),
            {"id": "w2", "name": "update_artifact",
             "input": {"kind": "scratchpad", "updated_content": "notes\nmore",
                       "base_version": 1}}]),
        _resp("Proposed both."),
    ])

    items = pending_for_user(ben.id, [reply.id, reply.continuation_node_id])
    assert [i["kind"] for i in items] == ["memory", "scratchpad"]
    assert {i["turn_node_id"] for i in items} == {reply.id}
    memory = items[0]
    assert memory["title"] == "Change your Memory"
    assert {"type": "add", "text": "- trusts anna"} in memory["lines"]
    assert memory["base_version"] == 1 and memory["stale"] is False


def test_an_apply_call_waits_too(app, people, task_module, monkeypatch):
    """apply_todo_changes applies a proposal without its button: it waits
    like a write. The proposal itself stays as it is."""
    from backend.routes import todo as todo_routes
    ben, anna = people
    bens = _thread_with_annas_reply(ben, anna)
    proposal = _llm(bens, ben, "### New Tasks\n- call mum", status="completed")
    proposal.privacy_level = "private"
    proposal.tool_calls_meta = json.dumps([{
        "name": "propose_todo", "status": "success",
        "apply_status": "pending_approval"}])
    draft = Draft(user_id=ben.id, parent_id=proposal.id, label="todo_pending")
    draft.set_content("")
    _db.session.add(draft)
    follow = _node(ben, proposal, "yes apply those")
    _db.session.commit()
    started = []
    monkeypatch.setattr(todo_routes, "_start_todo_merge",
                        lambda *a, **k: started.append(a) or "task-1")
    monkeypatch.setattr(todo_routes, "todo_merge_refusal",
                        lambda *a, **k: None)

    reply = _ask(follow, ben, [
        _resp("Applying.", tool_calls=[{
            "id": "a1", "name": "apply_todo_changes", "input": {}}]),
        _resp("Proposed."),
    ])

    assert started == []
    [entry] = _entries(reply, "apply_todo_changes")
    assert entry["status"] == "pending_approval"
    [held] = _held(ben)

    from backend.utils.tool_approvals import decide, serialize_pending
    item = serialize_pending(held, ben.id)
    assert item["title"] == "Apply the proposed changes to your todo list"
    assert "call mum" in item["proposal_text"]
    outcome, _ = decide(held, ben.id, approve=True)
    _db.session.commit()
    assert outcome == "approved"
    assert len(started) == 1


# ── What counts as text by others in the history ───────────────────────


def test_my_own_thread_writes_at_once(app, people):
    """Only Ben's text in the history: the write runs as before."""
    ben, _ = people
    _memory(ben)
    system = _system_node(ben, privacy_level="private")
    entry = _node(ben, system, "remember: tea", privacy_level="private")
    _db.session.commit()

    reply = _ask(entry, ben, [
        _resp("Noting.", tool_calls=[_write_memory("- likes tea\n- tea!")]),
        _resp("Done."),
    ])

    assert _latest_memory(ben) == "- likes tea\n- tea!"
    assert _held(ben) == []
    [entry] = _entries(reply)
    assert entry["status"] == "success"
    assert "approval" not in _second_call_text()


def test_a_quote_of_someone_elses_node_in_my_private_entry_counts(app, people):
    ben, anna = people
    _memory(ben)
    annas = _node(anna, None, "anna's public post")
    system = _system_node(ben, privacy_level="private")
    entry = _node(ben, system, f"look at this {{quote:{annas.id}}}",
                  privacy_level="private")
    _db.session.commit()

    _ask(entry, ben, [
        _resp("Noting.", tool_calls=[_write_memory("- likes tea\n- x")]),
        _resp("Proposed."),
    ])

    assert _latest_memory(ben) == "- likes tea"
    assert len(_held(ben)) == 1


def test_a_saved_reference_in_my_entry_counts(app, people):
    ben, _ = people
    _memory(ben)
    item = ExternalItem(user_id=ben.id, source="web_clip",
                        external_id="clip-1", url="https://example.org/a")
    item.set_content("a clipped page")
    _db.session.add(item)
    _db.session.flush()
    system = _system_node(ben, privacy_level="private")
    entry = _node(ben, system, f"saved this {{quote_ext:{item.id}}}",
                  privacy_level="private")
    _db.session.commit()

    _ask(entry, ben, [
        _resp("Noting.", tool_calls=[_write_memory("- likes tea\n- x")]),
        _resp("Proposed."),
    ])

    assert _latest_memory(ben) == "- likes tea"
    [held] = _held(ben)
    from backend.utils.tool_approvals import _payload
    assert _payload(held)["kinds"] == ["saved_reference"]


def test_what_counts_in_the_history(app, people):
    """The rule on its own: another person's node, a quote of theirs at
    depth, a saved reference and a Glean reply count; placeholders and a
    quote of my own entry do not."""
    from backend.utils.foreign_text import (
        GLEAN, OTHER_USER, history_by_others,
    )
    ben, anna = people
    system = _system_node(ben, prompt_text="{user_recent_raw} {user_export}",
                          privacy_level="private")
    mine = _node(ben, None, "my old entry", privacy_level="private")
    quoting_mine = _node(ben, system, f"see {{quote:{mine.id}}}",
                         privacy_level="private")
    _db.session.commit()
    assert history_by_others([system, quoting_mine], ben.id) == set()

    annas = _node(anna, None, "anna's public post")
    middle = _node(ben, None, f"quoting anna {{quote:{annas.id}}}",
                   privacy_level="private")
    deep = _node(ben, system, f"see {{quote:{middle.id}}}",
                 privacy_level="private")
    _db.session.commit()
    assert history_by_others([system, deep], ben.id) == {OTHER_USER}

    annas_private = _node(anna, None, "anna's private",
                          privacy_level="private")
    unreadable = _node(ben, system, f"see {{quote:{annas_private.id}}}",
                       privacy_level="private")
    _db.session.commit()
    assert history_by_others([system, unreadable], ben.id) == set()

    glean = _llm(quoting_mine, ben, "picks", status="completed")
    assert history_by_others([system, quoting_mine, glean], ben.id,
                             read_reply_ids={glean.id}) == {GLEAN}


def test_the_payload_is_the_same_until_a_write_is_held(app, people):
    """The tools offered and the first call's messages do not depend on
    the rule: a thread with Anna's reply sends what it sent before."""
    ben, anna = people
    _memory(ben)
    bens = _thread_with_annas_reply(ben, anna)
    from backend.utils import foreign_text
    _ask(bens, ben, [_resp("Hello.")])
    with_rule = _ScriptedProvider.calls[0]
    real = foreign_text.history_by_others
    try:
        foreign_text.history_by_others = lambda *a, **k: set()
        _ask(bens, ben, [_resp("Hello.")])
    finally:
        foreign_text.history_by_others = real
    without_rule = _ScriptedProvider.calls[0]
    assert with_rule["tools"] == without_rule["tools"]
    assert with_rule["raw"] == without_rule["raw"]
