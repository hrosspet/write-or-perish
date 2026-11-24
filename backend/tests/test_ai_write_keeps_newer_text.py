"""The AI's artifact write never puts back a text the person edited since
the AI read it.

update_artifact names the version it is based on (#604); a tick or a "+"
on the artifacts page edits the person's own latest version in place, so
the number stays. A write based on the text from before the tick passed
the number check and saved that text, without the tick, both at once and
after an approval. The turn now keeps the edit stamp of each artifact text
the model got (utils/artifact_copies): a write based on a version edited in
place since saves nothing, and the next round gets the current text to
retry on (one more round of the turn). A write kept for approval keeps the
stamp, so approving it after a tick saves nothing.

A version a session has pinned is not edited in place (a tick makes a new
version, which the number check catches). These tests use what is not
pinned: a conversation that started before the person had the artifact,
and a version read in the same round.

Runs the real task body through the test_retrieval_loop harness.
"""
import json
import sys
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.tests.test_personal_context_owner import (  # noqa: F401
    _node, _llm, _system_node,
)
from backend.extensions import db as _db
from backend.models import (
    Draft, Node, NodeContextArtifact, User, UserArtifact, UserPrompt,
)
from backend.utils.artifact_copies import ArtifactCopies
from backend.utils.context_artifacts import attach_context_artifacts

PROMPT = "SYSTEM PROMPT\n<user_memory>\n{user_memory}\n</user_memory>"
MEMORY = "- [ ] call the bank\n- [ ] book the room\n- has a dog"
TICKED = "- [x] call the bank\n- [ ] book the room\n- has a dog"


@pytest.fixture
def task_module(monkeypatch):
    """The approval code imports the task module lazily: the harness's."""
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    return _llm_task_mod


def _users():
    alice = _mk_user("alice", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    return alice


def _memory(user, text=MEMORY):
    """The person's own memory (written on the artifacts page)."""
    row = UserArtifact(user_id=user.id, kind="memory", title="Memory",
                       generated_by="user", ai_usage="chat", tokens_used=0)
    row.set_content(text)
    _db.session.add(row)
    _db.session.commit()
    return row


def _thread_started_before_memory():
    """alice's Text-mode thread, started before she had any artifact: no
    artifact is pinned on it, so its prompt shows memory's latest version.
    She then writes her memory on the artifacts page."""
    alice = _users()
    prompt = UserPrompt(user_id=alice.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content(PROMPT)
    _db.session.add(prompt)
    _db.session.flush()
    system = Node(user_id=alice.id, human_owner_id=alice.id,
                  node_type="user", privacy_level="private", ai_usage="chat")
    system.set_content(PROMPT)
    _db.session.add(system)
    _db.session.flush()
    attach_context_artifacts(system.id, alice.id, prompt_record=prompt)
    _db.session.commit()
    _memory(alice)
    return alice, system


def _tick_on_the_artifacts_page(user_id, text=TICKED):
    """What PATCH /api/artifacts/memory does with a tick on the person's
    own latest version that no session pinned: an edit in place (the
    version and its number stay)."""
    row = UserArtifact.latest_for(user_id, "memory")
    assert row.generated_by == "user"
    assert NodeContextArtifact.query.filter_by(
        artifact_type="user_artifact", artifact_id=row.id).first() is None
    row.set_content(text)
    row.updated_at = datetime.utcnow()
    _db.session.commit()


def _tick_while_the_model_answers(monkeypatch, call_index, user_id):
    """The person ticks a box while model call *call_index* is running."""
    original = _ScriptedProvider.get_completion.__func__

    def hooked(cls, *args, **kwargs):
        if len(cls.calls) == call_index:
            _tick_on_the_artifacts_page(user_id)
        return original(cls, *args, **kwargs)
    monkeypatch.setattr(_ScriptedProvider, "get_completion",
                        classmethod(hooked))


def _turn(alice, parent, text, responses):
    mine = Node(user_id=alice.id, human_owner_id=alice.id,
                parent_id=parent.id, node_type="user",
                privacy_level="private", ai_usage="chat")
    mine.set_content(text)
    _db.session.add(mine)
    _db.session.flush()
    llm_user = User.query.filter_by(username="gpt-5").first()
    reply = Node(user_id=llm_user.id, human_owner_id=alice.id,
                 parent_id=mine.id, node_type="llm", llm_model="gpt-5",
                 llm_task_status="pending", privacy_level="private",
                 ai_usage="chat")
    reply.set_content("[LLM response generation pending...]")
    _db.session.add(reply)
    _db.session.commit()
    _ScriptedProvider.reset(responses)
    result = generate_llm_response(_FakeSelf(), mine.id, reply.id, "gpt-5",
                                   alice.id, source_mode="textmode")
    return _fresh(reply.id), _fresh(result["llm_node_id"])


def _rewrite(text, base_version=1, call_id="w1"):
    return {"id": call_id, "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": text,
                      "base_version": base_version}}


def _texts(call_index):
    return "\n".join(m["text"] for m in
                     _ScriptedProvider.calls[call_index]["messages"])


def _latest_memory(user_id):
    _db.session.expire_all()
    return UserArtifact.latest_for(user_id, "memory")


# ── In the turn ──────────────────────────────────────────────────────────

def test_a_rewrite_after_a_tick_during_the_turn_is_refused_and_retried(
        app, monkeypatch):
    alice, system = _thread_started_before_memory()
    _tick_while_the_model_answers(monkeypatch, 0, alice.id)
    added = MEMORY + "\n- started a garden"
    retried = TICKED + "\n- started a garden"

    interim, final = _turn(alice, system, "I started a garden.", [
        _resp("Noting the garden.", tool_calls=[_rewrite(added)]),
        _resp("Memory changed; again.", tool_calls=[
            _rewrite(retried, call_id="w2")]),
        _resp("Noted."),
    ])

    # The model's first copy had no tick: its write saved nothing.
    first = _texts(0)
    assert "- [ ] call the bank" in first
    entry = json.loads(interim.tool_calls_meta)[0]
    assert entry["status"] == "error"
    assert "was edited by the user since you read it" in entry["error"]
    second = _texts(1)
    assert ("[Current text of 'memory' · v1 — base your retry on this "
            f"version:\n{TICKED}]") in second
    # The retry, on the ticked text, saved v2 with both changes.
    latest = _latest_memory(alice.id)
    assert latest.get_content() == retried
    assert UserArtifact.version_count(alice.id, "memory") == 2
    assert len(_ScriptedProvider.calls) == 3


def test_targeted_edits_after_a_tick_are_refused_too(app, monkeypatch):
    """As for a changed version number (Peter, 2026-11-06): edits are
    refused even when they would still apply."""
    alice, system = _thread_started_before_memory()
    _tick_while_the_model_answers(monkeypatch, 0, alice.id)
    interim, _ = _turn(alice, system, "Two dogs.", [
        _resp("Noting.", tool_calls=[{
            "id": "w1", "name": "update_artifact",
            "input": {"kind": "memory", "base_version": 1, "edits": [
                {"old_text": "- has a dog", "new_text": "- has two dogs"}]},
        }]),
        _resp("Couldn't save that."),
    ])
    assert json.loads(interim.tool_calls_meta)[0]["status"] == "error"
    assert _latest_memory(alice.id).get_content() == TICKED


def test_a_tick_made_before_the_turn_is_in_its_copy_and_no_refusal(app):
    """No false refusal: the prompt shows the ticked text."""
    alice, system = _thread_started_before_memory()
    _tick_on_the_artifacts_page(alice.id)
    retried = TICKED + "\n- started a garden"
    interim, _ = _turn(alice, system, "I started a garden.", [
        _resp("Noting the garden.", tool_calls=[_rewrite(retried)]),
        _resp("Noted."),
    ])
    assert json.loads(interim.tool_calls_meta)[0]["status"] == "success"
    assert _latest_memory(alice.id).get_content() == retried
    assert len(_ScriptedProvider.calls) == 2


def test_two_writes_in_one_turn_each_rest_on_the_last(app):
    """The model's own write is a text it was given: the next write may
    rest on it."""
    alice, system = _thread_started_before_memory()
    interim, final = _turn(alice, system, "A garden, and a cat.", [
        _resp("Noting.", tool_calls=[
            _rewrite(MEMORY + "\n- started a garden")]),
        _resp("And the cat.", tool_calls=[
            _rewrite(MEMORY + "\n- started a garden\n- has a cat",
                     base_version=2, call_id="w2")]),
        _resp("Noted."),
    ])
    assert _latest_memory(alice.id).get_content().endswith("- has a cat")
    assert UserArtifact.version_count(alice.id, "memory") == 3


def test_a_version_read_in_the_round_then_ticked_is_refused(app):
    """Read in a round, ticked before that round's pin is committed (the
    tick is then an edit in place), then written on: refused."""
    alice = _users()
    row = _memory(alice)
    _db.session.commit()
    node = Node(user_id=alice.id, node_type="user")
    node.set_content("turn")
    _db.session.add(node)
    _db.session.commit()
    copies = ArtifactCopies()
    copies.given(row)          # the read's text went to the model
    _tick_on_the_artifacts_page(alice.id)

    [result] = _llm_task_mod._execute_tool_calls(
        [_rewrite(MEMORY + "\n- started a garden")], node, [node],
        alice.id, copies=copies)
    assert result["status"] == "error"
    assert result["current_artifact_id"] == row.id
    assert _latest_memory(alice.id).get_content() == TICKED


def test_a_version_the_model_was_never_given_is_not_written_on(app):
    alice = _users()
    _memory(alice)
    node = Node(user_id=alice.id, node_type="user")
    node.set_content("turn")
    _db.session.add(node)
    _db.session.commit()
    [result] = _llm_task_mod._execute_tool_calls(
        [_rewrite("- guessed")], node, [node], alice.id,
        copies=ArtifactCopies())
    assert result["status"] == "error"
    assert "have not been given the text" in result["error"]
    assert _latest_memory(alice.id).get_content() == MEMORY


# ── A write that waits for approval ──────────────────────────────────────

def _thread_with_annas_reply_before_memory():
    """Ben's public Text-mode thread with Anna's reply in it (his AI's
    writes wait for his approval), started before his memory existed."""
    ben = _mk_user("ben", approved=True, plan="alpha")
    anna = _mk_user("anna", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _db.session.commit()
    system = _system_node(ben)
    root = _node(ben, system, "ben thinks out loud")
    annas = _node(anna, root, "anna replies")
    bens = _node(ben, annas, "ben answers anna")
    _db.session.commit()
    _memory(ben)
    return ben, bens


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


def test_approving_a_write_after_a_tick_meanwhile_saves_nothing(
        app, task_module):
    from backend.utils.tool_approvals import decide, serialize_pending
    ben, bens = _thread_with_annas_reply_before_memory()
    _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _rewrite(MEMORY + "\n- trusts anna")]),
        _resp("Proposed."),
    ])
    [held] = _held(ben)
    assert serialize_pending(held, ben.id)["stale"] is False

    # Ben ticks a box in his memory while the dialog waits.
    _tick_on_the_artifacts_page(ben.id)
    [held] = _held(ben)
    assert serialize_pending(held, ben.id)["stale"] is True

    outcome, result = decide(held, ben.id, approve=True)
    _db.session.commit()
    assert outcome == "failed"
    assert "was edited by the user since you read it" in result["error"]
    assert _latest_memory(ben.id).get_content() == TICKED
    assert UserArtifact.version_count(ben.id, "memory") == 1


def test_approving_a_write_with_no_tick_meanwhile_saves_it(
        app, task_module):
    from backend.utils.tool_approvals import decide
    ben, bens = _thread_with_annas_reply_before_memory()
    _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _rewrite(MEMORY + "\n- trusts anna")]),
        _resp("Proposed."),
    ])
    [held] = _held(ben)
    outcome, _ = decide(held, ben.id, approve=True)
    _db.session.commit()
    assert outcome == "approved"
    assert _latest_memory(ben.id).get_content() == MEMORY + "\n- trusts anna"


def test_a_write_held_before_stamps_is_approved_on_its_number(
        app, task_module):
    """A call kept before this change has no stamp: approved as before."""
    from backend.utils.tool_approvals import decide
    ben, bens = _thread_with_annas_reply_before_memory()
    _ask(bens, ben, [
        _resp("Noting that.", tool_calls=[
            _rewrite(MEMORY + "\n- trusts anna")]),
        _resp("Proposed."),
    ])
    [held] = _held(ben)
    payload = json.loads(held.get_content())
    payload.pop("base_edited_at")
    held.set_content(json.dumps(payload))
    _db.session.commit()

    outcome, _ = decide(held, ben.id, approve=True)
    _db.session.commit()
    assert outcome == "approved"
