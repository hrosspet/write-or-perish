"""The completion task measures the prompt it assembled before its first
call (#648; Peter, 2026-11-12).

- Under the notice size, nothing about the call changes: the same bytes
  go out with the limit on as with it off (the prompt cache, #187).
- At or over THREAD_PROMPT_TOKEN_LIMIT the reply fails with the limit's
  message before any provider call; the measurement is recorded as a $0
  "thread_limit" row under the node it was asked under, so the thread
  page and the next request know it.
- Text a quote pulls in counts (a thread holding one large document).
- A reply that started under the limit finishes its tool rounds.
- An uncapped {user_export} is the one part that shrinks to fit.
- Every conversation call records its prompt's characters.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app); nothing
reaches a real model.
"""
import json

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _build_chain, _mk_artifact, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import APICostLog, Node, User
from backend.utils.thread_size import (
    REFUSED_MARKER, THREAD_LIMIT_REQUEST_TYPE, THREAD_TOO_LONG_MESSAGE,
)

LIMIT = 10_000


@pytest.fixture
def limited(app):  # noqa: F811
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    app.config["THREAD_PROMPT_NOTICE_FRACTION"] = 0.9
    yield app
    app.config.pop("THREAD_PROMPT_TOKEN_LIMIT", None)
    app.config.pop("THREAD_PROMPT_NOTICE_FRACTION", None)


@pytest.fixture
def fixed_clock(monkeypatch):
    """Every time stamp the prompt carries, fixed: two runs of one turn
    then send the same bytes."""
    monkeypatch.setattr(_llm_task_mod, "local_stamp",
                        lambda *_a, **_k: "[2026-11-12 10:00]")


def _placeholder(parent, owner):
    llm_user = User.query.filter_by(username="gpt-5").first()
    n = Node(user_id=llm_user.id, human_owner_id=owner.id,
             parent_id=parent.id, node_type="llm", llm_model="gpt-5",
             llm_task_status="pending", privacy_level="private",
             ai_usage="chat")
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _measured_thread(measured, chars_per_token=4):
    """system -> entry -> a finished reply whose first call measured
    *measured* tokens -> the user's next entry -> a pending reply."""
    alice, system, user_node, reply = _build_chain("textmode")
    reply.set_content("An earlier answer.")
    reply.llm_task_status = "completed"
    reply.token_count = 5
    _db.session.add(APICostLog(
        user_id=alice.id, model_id="gpt-5", request_type="conversation",
        input_tokens=measured, output_tokens=10,
        prompt_chars=measured * chars_per_token, cost_microdollars=1,
        request_ref=f"node:{reply.id}"))
    entry = Node(user_id=alice.id, human_owner_id=alice.id,
                 parent_id=reply.id, node_type="user",
                 privacy_level="private", ai_usage="chat", token_count=5)
    entry.set_content("And one more thing.")
    _db.session.add(entry)
    _db.session.commit()
    return alice, entry, _placeholder(entry, alice)


def _run(entry, pending, alice):
    return generate_llm_response(
        _FakeSelf(), entry.id, pending.id, "gpt-5", alice.id,
        source_mode="textmode")


def test_under_the_notice_size_the_bytes_sent_are_unchanged(  # noqa: F811
        app, fixed_clock):
    """The same turn, sent once with the limit off and once with the
    default limit on: identical messages and tools."""
    alice, entry, first = _measured_thread(1_000)
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
    _ScriptedProvider.reset([_resp("One.")])
    _run(entry, first, alice)
    off = _ScriptedProvider.calls[0]

    second = _placeholder(entry, alice)
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 250_000
    _ScriptedProvider.reset([_resp("Two.")])
    _run(entry, second, alice)
    on = _ScriptedProvider.calls[0]
    app.config.pop("THREAD_PROMPT_TOKEN_LIMIT", None)

    assert json.dumps(on["raw"], sort_keys=True) == json.dumps(
        off["raw"], sort_keys=True)
    assert on["tools"] == off["tools"]


def test_between_the_notice_and_the_limit_the_reply_goes_out(limited):
    alice, entry, pending = _measured_thread(9_200)
    _ScriptedProvider.reset([_resp("Still here.")])

    _run(entry, pending, alice)

    assert len(_ScriptedProvider.calls) == 1
    assert _fresh(pending.id).get_content() == "Still here."


def test_at_the_limit_the_reply_is_refused_before_any_call(limited):
    alice, entry, pending = _measured_thread(9_990)
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(entry, pending, alice)

    assert result["status"] == "refused"
    assert result["reason"] == "thread_too_long"
    assert _ScriptedProvider.calls == []
    node = _fresh(pending.id)
    assert node.llm_task_status == "failed"
    assert node.llm_task_error == THREAD_TOO_LONG_MESSAGE
    meta = json.loads(node.tool_calls_meta)
    assert {"name": REFUSED_MARKER, "code": "thread_too_long"} in meta
    # Recorded, at no cost, under the node the reply was asked under.
    row = APICostLog.query.filter_by(
        request_type=THREAD_LIMIT_REQUEST_TYPE).one()
    assert row.request_ref == f"node:{entry.id}"
    assert row.cost_microdollars == 0
    assert row.input_tokens >= LIMIT
    # ...so the thread page and the next request know without the task.
    from backend.utils.thread_size import thread_length
    assert thread_length(entry, alice.id)[1] == "limit"


def test_a_quoted_large_document_counts(limited):
    """A thread with one large document in it: here quoted, so no stored
    size shows it before the prompt is assembled. The task's own measure
    refuses the first reply, and the message says what it quotes counts."""
    alice, system, user_node, pending = _build_chain("textmode")
    doc = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
               privacy_level="private", ai_usage="chat")
    doc.set_content("A long chapter. " * 3_500)   # 56k characters
    _db.session.add(doc)
    _db.session.flush()
    user_node.set_content(f"What do you think of this? {{quote:{doc.id}}}")
    user_node.token_count = 10
    _db.session.commit()
    from backend.utils.thread_size import thread_length
    assert thread_length(user_node, alice.id)[1] is None  # cannot see it
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(user_node, pending, alice)

    assert result["reason"] == "thread_too_long"
    assert _ScriptedProvider.calls == []
    assert "including what it quotes" in _fresh(pending.id).llm_task_error
    assert thread_length(user_node, alice.id)[1] == "limit"


def test_tool_rounds_of_a_reply_that_started_under_the_limit_finish(limited):
    """The first call is just under the limit; the round's injection takes
    the continuation over it. The reply still finishes."""
    alice, entry, pending = _measured_thread(9_850)
    _mk_artifact(alice.id, "reading-list", "book " * 2_000,
                 title="Reading List")
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Let me look.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        _resp("Here it is."),
    ])

    _run(entry, pending, alice)

    assert len(_ScriptedProvider.calls) == 2
    interim = _fresh(pending.id)
    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Here it is."


def test_every_call_records_its_prompt_characters(app):  # noqa: F811
    alice, system, user_node, pending = _build_chain("textmode")
    _ScriptedProvider.reset([_resp("An answer.")])

    _run(user_node, pending, alice)

    row = APICostLog.query.filter_by(request_type="conversation").one()
    sent = _ScriptedProvider.calls[0]
    from backend.utils.thread_size import prompt_chars
    assert row.prompt_chars == prompt_chars(sent["raw"], sent["tools"])
    assert row.prompt_chars > 0


def test_an_uncapped_archive_shrinks_to_fit(limited, monkeypatch):
    """{user_export} without a limit loads as much of the archive as fits:
    under the thread limit now, as under the model's window before."""
    alice, system, user_node, pending = _build_chain("textmode")
    alice.plan = "pro"
    user_node.set_content("Read my archive: {user_export}")
    _db.session.commit()
    budgets = []

    def fake_export(user, max_tokens=None, **_kwargs):
        budgets.append(max_tokens)
        return "e" * (4 * (max_tokens if max_tokens else 20_000))
    monkeypatch.setattr(_llm_task_mod, "build_user_export_content",
                        fake_export)
    _ScriptedProvider.reset([_resp("A letter.")])

    _run(user_node, pending, alice)

    assert budgets[0] is None
    assert len(budgets) >= 2 and budgets[-1] < 20_000
    assert len(_ScriptedProvider.calls) == 1
    sent = _ScriptedProvider.calls[0]
    from backend.utils.thread_size import prompt_chars
    # Under the limit at the prior (no measurement yet: 1.15 tokens/unit).
    assert prompt_chars(sent["raw"], sent["tools"]) / 4 * 1.15 < LIMIT
