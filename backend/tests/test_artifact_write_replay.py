"""Later turns see the thread's own artifact writes; every write names
its version (#604).

- A write is replayed in every later turn's history on the provider's
  tool channel: the model's call after its reply's text, and the
  system's answer to it ("'memory' updated to v2 (from v1)" and the diff)
  in the user message after it. The answer is built from the two stored
  version ids, so its bytes are the same on every later turn.
- A version the user keeps away from AI (AI usage None) shows no text.
- Inline copies, read_artifact results and write results carry the
  version, numbered as the artifacts page numbers it.
- update_artifact names the version it is based on. A stale or missing
  base_version is refused, the next round of the same turn gets the
  current text (fetched from the row, never stored), and the retry
  saves without dropping the change made elsewhere.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app); the provider
message formats are checked against the real backend.llm_providers
converters. Nothing reaches a real model.
"""
import json
import sys

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _mk_artifact, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import (
    Node, NodeContextArtifact, User, UserArtifact, UserPrompt,
)
from backend.utils.api_keys import PayloadLicence
from backend.utils.context_artifacts import attach_context_artifacts

PROMPT = "SYSTEM PROMPT\n<user_memory>\n{user_memory}\n</user_memory>"
MEMORY_V1 = "## Facts\n- has a dog\n- lives in Prague"


@pytest.fixture
def providers(monkeypatch):
    """The real provider module, imported fresh (it may be mocked
    globally); monkeypatch restores the sys.modules entry and the
    package attribute afterwards (as in test_provider_streaming)."""
    import backend
    monkeypatch.setattr(backend, "llm_providers",
                        getattr(backend, "llm_providers", None),
                        raising=False)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers as mod
    return mod


def _thread():
    """alice's Text-mode thread: a system node whose prompt shows memory
    inline, memory v1 pinned on it at the start."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _mk_artifact(alice.id, "memory", MEMORY_V1, title="Memory")
    prompt = UserPrompt(user_id=alice.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content(PROMPT)
    _db.session.add(prompt)
    _db.session.flush()
    system = Node(user_id=alice.id, human_owner_id=alice.id,
                  node_type="user", privacy_level="private",
                  ai_usage="chat")
    system.set_content(PROMPT)
    _db.session.add(system)
    _db.session.flush()
    attach_context_artifacts(system.id, alice.id, prompt_record=prompt)
    _db.session.commit()
    return alice, system


def _turn(alice, parent, text, responses):
    """alice writes *text* under *parent*; the Text-mode reply runs
    against the scripted *responses*. Returns (the turn's first reply
    node, its last)."""
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


def _edit(base_version, old, new, call_id="w1"):
    tool_input = {"kind": "memory",
                  "edits": [{"old_text": old, "new_text": new}]}
    if base_version is not None:
        tool_input["base_version"] = base_version
    return {"id": call_id, "name": "update_artifact", "input": tool_input}


def _texts(call_index=0):
    return "\n".join(m["text"] for m in
                     _ScriptedProvider.calls[call_index]["messages"])


def _assistant_at(raw, text):
    """Index of the one assistant message whose text has *text*."""
    found = [i for i, m in enumerate(raw)
             if m["role"] == "assistant" and text in m["content"][0]["text"]]
    assert len(found) == 1
    return found[0]


TWO_DOGS = [_resp("Noting the second dog.", tool_calls=[
    _edit(1, "- has a dog", "- has two dogs")]), _resp("Noted.")]
V2_ANSWER = ("'memory' updated to v2 (from v1). Changes:\n"
             "@@ -1,3 +1,3 @@\n ## Facts\n-- has a dog\n+- has two dogs\n"
             " - lives in Prague")


# ── The replay ───────────────────────────────────────────────────────────

def test_a_later_turn_carries_the_earlier_write_and_its_diff(app, providers):  # noqa: F811
    alice, system = _thread()
    interim, final = _turn(alice, system, "I got a second dog.", TWO_DOGS)
    assert interim.get_content() == "Noting the second dog."

    _turn(alice, final, "How many dogs do I have?", [_resp("Two.")])
    raw = _ScriptedProvider.calls[0]["raw"]
    i = _assistant_at(raw, "Noting the second dog.")

    # The call after the reply's text, its content left out; the system's
    # answer in the user message after it; then the reply's continuation.
    call_id = f"loore_{interim.id}_0"
    assert raw[i]["content"][1:] == [{
        "type": "tool_use", "id": call_id, "name": "update_artifact",
        "input": {"kind": "memory", "base_version": 1}}]
    assert raw[i + 1] == {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": call_id,
        "content": V2_ANSWER}]}
    assert raw[i + 2]["role"] == "assistant"
    assert raw[i + 2]["content"][0]["text"].endswith("Noted.")

    # Anthropic takes the blocks as they are.
    _, ant = providers.LLMProvider._to_anthropic_params(raw)
    assert ant[i]["content"][1]["type"] == "tool_use"
    assert ant[i + 1]["content"] == raw[i + 1]["content"]

    # The Responses API takes them as function call items, in order.
    items = providers.responses_input(raw)
    at = next(k for k, item in enumerate(items)
              if item.get("type") == "function_call")
    assert items[at - 1]["role"] == "assistant"
    assert items[at - 1]["content"][0]["text"].endswith(
        "Noting the second dog.")
    assert items[at]["call_id"] == call_id
    assert items[at]["name"] == "update_artifact"
    assert json.loads(items[at]["arguments"]) == {
        "kind": "memory", "base_version": 1}
    assert items[at + 1] == {"type": "function_call_output",
                             "call_id": call_id, "output": V2_ANSWER}
    assert items[at + 2]["role"] == "assistant"

    # Never in plaintext meta, never in a node's text (nothing speaks it).
    assert "two dogs" not in interim.tool_calls_meta
    assert "two dogs" not in final.get_content()


def test_the_replayed_bytes_are_the_same_on_every_later_turn(app):  # noqa: F811
    alice, system = _thread()
    _, final1 = _turn(alice, system, "I got a second dog.", TWO_DOGS)
    _, final2 = _turn(alice, final1, "How many dogs?", [_resp("Two.")])
    raw2 = _ScriptedProvider.calls[0]["raw"]
    # Memory changes again elsewhere: the replay of the earlier write
    # does not depend on the latest version.
    _mk_artifact(alice.id, "memory", MEMORY_V1 + "\n- has a cat")
    _db.session.commit()
    _turn(alice, final2, "And cats?", [_resp("One.")])
    raw3 = _ScriptedProvider.calls[0]["raw"]

    end = _assistant_at(raw2, "Noted.") + 1
    assert end == _assistant_at(raw3, "Noted.") + 1
    assert json.dumps(raw2[:end]) == json.dumps(raw3[:end])
    assert any(m["content"][0].get("content") == V2_ANSWER
               for m in raw3[:end] if m["role"] == "user")


def test_a_version_kept_away_from_ai_is_never_replayed(app):  # noqa: F811
    alice, system = _thread()
    interim, final = _turn(alice, system, "I got a second dog.", TWO_DOGS)
    v2 = UserArtifact.latest_for(alice.id, "memory")
    v2.ai_usage = "none"
    _db.session.commit()

    _turn(alice, final, "How many dogs?", [_resp("I can't see that.")])
    raw = _ScriptedProvider.calls[0]["raw"]
    i = _assistant_at(raw, "Noting the second dog.")
    assert raw[i + 1]["content"][0]["content"] == (
        "'memory' updated to v2. That version is kept away from AI, so "
        "its text is not shown.")
    assert "two dogs" not in json.dumps(raw)


def test_a_previous_version_kept_away_from_ai_gives_no_diff(app):  # noqa: F811
    alice, system = _thread()
    _, final = _turn(alice, system, "I got a second dog.", TWO_DOGS)
    v1 = UserArtifact.query.filter_by(user_id=alice.id, kind="memory") \
        .order_by(UserArtifact.id.asc()).first()
    v1.ai_usage = "none"
    _db.session.commit()

    _turn(alice, final, "How many dogs?", [_resp("Two.")])
    payload = json.dumps(_ScriptedProvider.calls[0]["raw"])
    assert ("'memory' updated to v2. Its full new text:\\n## Facts\\n"
            "- has two dogs") in payload
    # v1's own line reaches no part of the prompt (nor its inline copy).
    assert "has a dog" not in payload


def test_the_replayed_rows_report_to_the_licence(app):  # noqa: F811
    """The answer's text is in the payload: the new version, and for a
    diff the previous one (its context and removed lines), each by its
    own ai_usage (#326)."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    old = _mk_artifact(alice.id, "memory", "line one", ai_usage="chat")
    new = _mk_artifact(alice.id, "memory", "line one\nline two",
                       ai_usage="train")
    _db.session.commit()
    entry = {"name": "update_artifact", "status": "success",
             "kind": "memory", "artifact_id": new.id,
             "previous_artifact_id": old.id}

    licence = PayloadLicence("train")
    report = _llm_task_mod._artifact_write_report(
        entry, alice.id, licence=licence)
    assert report.startswith("'memory' updated to v2 (from v1). Changes:")
    assert licence.key_type == "chat"

    old.ai_usage = "train"
    _db.session.commit()
    licence = PayloadLicence("train")
    _llm_task_mod._artifact_write_report(entry, alice.id, licence=licence)
    assert licence.key_type == "train"


def test_someone_elses_version_is_never_replayed(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    bobs = _mk_artifact(bob.id, "memory", "BOB SECRET")
    _db.session.commit()
    report = _llm_task_mod._artifact_write_report(
        {"name": "update_artifact", "status": "success", "kind": "memory",
         "artifact_id": bobs.id, "previous_artifact_id": None}, alice.id)
    assert "BOB SECRET" not in report


# ── A change made elsewhere stays in every later turn ────────────────────
# The version check only protects a change the model has seen: every
# version a later turn names must come with its text. A replayed write
# is a diff only against a version the prompt already shows (the inline
# copy, or one an earlier answer showed); otherwise its full new text.

LONG_V1 = "## Facts\n- has a dog\n- a1\n- a2\n- a3\n- a4\n- a5"
ELSEWHERE = ["- GARDEN ONE", "- GARDEN TWO", "- GARDEN THREE"]
LONG_V2 = LONG_V1 + "\n" + "\n".join(ELSEWHERE)


def _long_thread_changed_elsewhere():
    """Pinned v1; then v2 saved elsewhere, its lines far from the edit
    the model will make (outside a diff's context)."""
    alice, system = _thread()
    v1 = UserArtifact.latest_for(alice.id, "memory")
    v1.set_content(LONG_V1)
    _mk_artifact(alice.id, "memory", LONG_V2)
    _db.session.commit()
    return alice, system


def _answers(raw):
    """The tool results' texts in a prompt."""
    return "\n".join(b["content"] for m in raw if m["role"] == "user"
                     for b in m["content"] if b.get("type") == "tool_result")


def test_a_change_made_elsewhere_stays_in_every_later_turn(app):  # noqa: F811
    """The reviewer's case on #606: pinned v1, v2 saved elsewhere, a
    refused write and a retry that saves v3 on v2. Every later turn shows
    v2's lines, so a write naming v3 is based on a copy that has them."""
    alice, system = _long_thread_changed_elsewhere()
    _, final = _turn(alice, system, "Second dog.", [
        _resp("Noting.", tool_calls=[
            _edit(1, "- has a dog", "- has two dogs")]),
        _resp("Retrying.", tool_calls=[
            _edit(2, "- has a dog", "- has two dogs", call_id="w2")]),
        _resp("Noted."),
    ])
    assert UserArtifact.latest_for(alice.id, "memory").get_content() == (
        LONG_V2.replace("- has a dog", "- has two dogs"))

    _, final2 = _turn(alice, final, "Anything else?", [_resp("No.")])
    raw2 = _ScriptedProvider.calls[0]["raw"]
    answers = _answers(raw2)
    for line in ELSEWHERE:
        assert line in answers, line
    # The model's copy of v3 is complete: v2's text, then v3's diff on it.
    assert "'memory' updated to v3 (from v2). Changes:" in answers

    # The same bytes on the next turn.
    _turn(alice, final2, "Thanks.", [_resp("Sure.")])
    raw3 = _ScriptedProvider.calls[0]["raw"]
    end = _assistant_at(raw2, "Noted.") + 1
    assert json.dumps(raw2[:end]) == json.dumps(raw3[:end])


def test_a_refusal_alone_still_shows_the_version_it_names(app):  # noqa: F811
    """No retry in the turn: the next turn sees "now v2" with v2's text,
    so a write naming v2 is not based on the inline v1."""
    alice, system = _long_thread_changed_elsewhere()
    _, final = _turn(alice, system, "Second dog.", [
        _resp("Noting.", tool_calls=[
            _edit(1, "- has a dog", "- has two dogs")]),
        _resp("Memory changed elsewhere; I'll leave it."),
    ])
    _turn(alice, final, "Go on.", [_resp("Ok.")])
    answers = _answers(_ScriptedProvider.calls[0]["raw"])
    assert "now v2. Nothing was saved.\n\nCurrent text of 'memory' · v2:\n" \
        in answers
    for line in ELSEWHERE:
        assert line in answers, line


def test_a_write_on_a_version_the_history_never_showed_is_replayed_in_full(app):  # noqa: F811
    """A custom artifact read in an earlier turn: the read is not
    replayed, so the write made from it comes back as its full text."""
    alice, system = _thread()
    _mk_artifact(alice.id, "plans", "- trip in May\n- paint the fence")
    _db.session.commit()
    _, final = _turn(alice, system, "Add the dentist to my plans.", [
        _resp("Pulling up your plans.", tool_calls=[
            {"id": "r1", "name": "read_artifact",
             "input": {"kind": "plans"}}]),
        _resp("Adding it.", tool_calls=[
            {"id": "w1", "name": "update_artifact",
             "input": {"kind": "plans", "base_version": 1,
                       "edits": [{"old_text": "- paint the fence",
                                  "new_text": "- paint the fence\n"
                                              "- book the dentist"}]}}]),
        _resp("Added."),
    ])
    _turn(alice, final, "Thanks.", [_resp("Sure.")])
    answers = _answers(_ScriptedProvider.calls[0]["raw"])
    assert answers == ("'plans' updated to v2 (from v1). Its full new "
                       "text:\n- trip in May\n- paint the fence\n"
                       "- book the dentist")


def test_after_a_reply_is_deleted_the_next_write_is_replayed_in_full(app):  # noqa: F811
    alice, system = _thread()
    interim1, final1 = _turn(alice, system, "Second dog.", TWO_DOGS)
    _, final2 = _turn(alice, final1, "And I moved.", [
        _resp("Updating.", tool_calls=[
            _edit(2, "- lives in Prague", "- lives in Brno")]),
        _resp("Done."),
    ])
    # v2 came from the turn-1 write's diff: turn 2's write is a diff.
    _turn(alice, final2, "Ok.", [_resp("Ok.")])
    assert "'memory' updated to v3 (from v2). Changes:" in _answers(
        _ScriptedProvider.calls[0]["raw"])

    from datetime import datetime
    interim1.deleted_at = datetime.utcnow()
    _db.session.commit()
    _turn(alice, final2, "Ok again.", [_resp("Ok.")])
    answers = _answers(_ScriptedProvider.calls[0]["raw"])
    assert answers == ("'memory' updated to v3 (from v2). Its full new "
                       "text:\n## Facts\n- has two dogs\n- lives in Brno")


def test_a_deleted_artifact_is_never_replayed_labelled_or_counted(app):  # noqa: F811
    """#603: once the user deletes a custom artifact, a later turn shows
    none of its text, and a new artifact of that kind starts at v1."""
    from backend.utils.artifact_deletion import soft_delete_artifact
    alice, system = _thread()
    interim, final = _turn(alice, system, "Start a list.", [
        _resp("Starting it.", tool_calls=[
            {"id": "c1", "name": "update_artifact",
             "input": {"kind": "plans",
                       "updated_content": "- SECRET PLAN"}}]),
        _resp("Started."),
    ])
    assert soft_delete_artifact(alice.id, "plans") == 1
    _db.session.commit()

    _turn(alice, final, "And now?", [_resp("Gone.")])
    raw = _ScriptedProvider.calls[0]["raw"]
    assert "SECRET PLAN" not in json.dumps(raw)
    assert _answers(raw) == "'plans' was saved, but that version no longer exists."
    entry = json.loads(interim.tool_calls_meta)[0]
    report = _llm_task_mod._artifact_write_report(entry, alice.id)
    assert "SECRET PLAN" not in report and "v1" not in report

    again = _mk_artifact(alice.id, "plans", "- a new list")
    _db.session.commit()
    from backend.utils.artifact_versions import version_number
    assert version_number(again) == 1
    assert _llm_task_mod.inline_artifact_text(again).startswith(
        "[plans · v1,")


def test_a_write_recorded_before_versions_names_no_number(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    row = _mk_artifact(alice.id, "memory", "a fact")
    _db.session.commit()
    report = _llm_task_mod._artifact_write_report(
        {"name": "update_artifact", "status": "success", "kind": "memory",
         "artifact_id": row.id, "created": False}, alice.id, known=set())
    assert report == "'memory' was updated."


# ── Version labels ───────────────────────────────────────────────────────

def test_versions_on_the_inline_copy_reads_and_writes(app):  # noqa: F811
    alice, system = _thread()
    _mk_artifact(alice.id, "reading-list", "1. Dune", title="Reading List")
    _mk_artifact(alice.id, "reading-list", "1. Dune\n2. Solaris",
                 title="Reading List")
    _db.session.commit()

    _turn(alice, system, "What's on my list? Also, a second dog.", [
        _resp("Pulling up your list.", tool_calls=[
            {"id": "r1", "name": "read_artifact",
             "input": {"kind": "reading-list"}},
            _edit(1, "- has a dog", "- has two dogs", call_id="w1")]),
        _resp("Dune and Solaris; noted the dog."),
    ])
    first = _texts(0)
    assert ("<user_memory>\n[memory · v1, as of the start of this "
            "conversation]\n## Facts\n- has a dog") in first
    second = _texts(1)
    assert ("[Contents of artifact 'reading-list' · v2, which you "
            "requested:\n1. Dune\n2. Solaris]") in second
    assert f"[update_artifact: {V2_ANSWER}]" in second


# ── base_version ─────────────────────────────────────────────────────────

def test_a_stale_base_version_is_refused_and_the_retry_keeps_both_changes(app):  # noqa: F811
    alice, system = _thread()
    # Memory changes in another thread after this one pinned v1.
    garden = MEMORY_V1 + "\n- started a garden"
    v2 = _mk_artifact(alice.id, "memory", garden)
    _db.session.commit()

    interim, final = _turn(alice, system, "I got a second dog.", [
        _resp("Noting the second dog.", tool_calls=[
            _edit(1, "- has a dog", "- has two dogs")]),
        _resp("Memory changed; retrying.", tool_calls=[
            _edit(2, "- has a dog", "- has two dogs", call_id="w2")]),
        _resp("Noted."),
    ])

    assert len(_ScriptedProvider.calls) == 3
    second = _texts(1)
    assert ("[update_artifact failed — 'memory' changed since v1, now v2. "
            "Nothing was saved.]") in second
    assert ("[Current text of 'memory' · v2 — base your retry on this "
            f"version:\n{garden}]") in second

    # Only ids in the plaintext meta; the version shown is pinned.
    entry = json.loads(interim.tool_calls_meta)[0]
    assert entry["status"] == "error"
    assert (entry["current_artifact_id"], entry["current_version"]) == (
        v2.id, 2)
    assert "garden" not in interim.tool_calls_meta
    assert NodeContextArtifact.query.filter_by(
        node_id=interim.id, artifact_type="user_artifact",
        artifact_id=v2.id).count() == 1

    # The retry saved v3 on top of the change made elsewhere.
    latest = UserArtifact.latest_for(alice.id, "memory")
    assert latest.get_content() == (
        "## Facts\n- has two dogs\n- lives in Prague\n- started a garden")
    assert UserArtifact.query.filter_by(
        user_id=alice.id, kind="memory").count() == 3

    # A later turn replays the refusal with the text it was shown (v2 is
    # in no other part of the prompt), then the retry's diff against it.
    _turn(alice, final, "Thanks.", [_resp("Sure.")])
    raw = _ScriptedProvider.calls[0]["raw"]
    i = _assistant_at(raw, "Noting the second dog.")
    assert raw[i + 1]["content"] == [{
        "type": "tool_result", "tool_use_id": f"loore_{interim.id}_0",
        "content": ("update_artifact failed — 'memory' changed since v1, "
                    "now v2. Nothing was saved.\n\n"
                    f"Current text of 'memory' · v2:\n{garden}"),
        "is_error": True}]
    j = _assistant_at(raw, "Memory changed; retrying.")
    assert raw[j + 1]["content"][0]["content"].startswith(
        "'memory' updated to v3 (from v2). Changes:")


def test_edits_with_a_stale_base_are_refused_even_when_they_would_apply(app):  # noqa: F811
    alice, system = _thread()
    _mk_artifact(alice.id, "memory", MEMORY_V1 + "\n- started a garden")
    _db.session.commit()
    interim, _ = _turn(alice, system, "Second dog.", [
        _resp("Noting.", tool_calls=[
            _edit(1, "- has a dog", "- has two dogs")]),
        _resp("Couldn't save that."),
    ])
    assert UserArtifact.query.filter_by(
        user_id=alice.id, kind="memory").count() == 2
    assert json.loads(interim.tool_calls_meta)[0]["status"] == "error"


def test_a_missing_base_version_is_refused_with_the_current_text(app):  # noqa: F811
    alice, system = _thread()
    interim, _ = _turn(alice, system, "Second dog.", [
        _resp("Noting.", tool_calls=[
            _edit(None, "- has a dog", "- has two dogs")]),
        _resp("Couldn't save that."),
    ])
    second = _texts(1)
    assert ("[update_artifact failed — base_version is missing: name the "
            "version of 'memory' your change is based on. 'memory' is now "
            "v1. Nothing was saved.]") in second
    assert f"[Current text of 'memory' · v1 — base your retry on this " \
           f"version:\n{MEMORY_V1}]" in second
    assert json.loads(interim.tool_calls_meta)[0]["current_version"] == 1


def test_creation_needs_no_base_version(app):  # noqa: F811
    alice, system = _thread()
    interim, _ = _turn(alice, system, "Start a list.", [
        _resp("Starting it.", tool_calls=[
            {"id": "c1", "name": "update_artifact",
             "input": {"kind": "reading-list",
                       "updated_content": "1. Dune"}}]),
        _resp("Started."),
    ])
    entry = json.loads(interim.tool_calls_meta)[0]
    assert entry["status"] == "success"
    assert (entry["version"], entry["previous_version"]) == (1, None)
    assert ("[update_artifact: 'reading-list' created as v1. Its text:\n"
            "1. Dune]") in _texts(1)


def test_a_base_version_for_an_artifact_that_has_none_is_refused(app):  # noqa: F811
    """A stale copy can't bring back an artifact that is gone."""
    alice, system = _thread()
    interim, _ = _turn(alice, system, "Update my list.", [
        _resp("Updating.", tool_calls=[
            {"id": "c1", "name": "update_artifact",
             "input": {"kind": "reading-list", "base_version": 3,
                       "updated_content": "1. Dune"}}]),
        _resp("It's not there."),
    ])
    entry = json.loads(interim.tool_calls_meta)[0]
    assert entry["status"] == "error"
    assert "no versions, so there is no v3" in entry["error"]
    assert "current_artifact_id" not in entry


@pytest.mark.parametrize("raw, latest, expected", [
    (None, 0, None),
    (0, 0, None),
    (2, 0, "'memory' has no versions, so there is no v2."),
    (4, 4, None),
    ("v4", 4, None),
    (" 4 ", 4, None),
    (4.0, 4, None),
    (3, 4, "'memory' changed since v3, now v4. Nothing was saved."),
    (5, 4, "'memory' has no v5; it is at v4. Nothing was saved."),
    (True, 4, "base_version True is not a version number."),
    ("latest", 4, "base_version 'latest' is not a version number."),
    ("", 4, "base_version is missing"),
])
def test_base_version_check(raw, latest, expected):
    inp = {"kind": "memory"}
    if raw is not None:
        inp["base_version"] = raw
    error = _llm_task_mod._base_version_error(inp, "memory", latest)
    if expected is None:
        assert error is None
    else:
        assert error.startswith(expected)
