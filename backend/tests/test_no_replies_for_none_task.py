"""The completion task asks the ai_usage rule again (2026-10-01).

A setting can change while a reply is queued or mid-turn, and some runs
are dispatched without create_llm_placeholder (a read rerun, a resumed
batch). The task refuses a run whose thread, or reply, keeps AI out
before anything is sent, and a continuation asks again before it sends
the thread a second time. The routes' side is in
test_no_replies_for_none.py.

Runs the real task body through the test_retrieval_loop harness (identity
@celery.task, scripted provider, test flask_app); nothing reaches a real
model.
"""
import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _build_chain, _mk_artifact, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import Node
from backend.utils.llm_nodes import REPLY_REFUSED_MESSAGE


def _set_ai_usage(node_id, value):
    """As the owner's edit would: behind the task's session."""
    _db.session.execute(Node.__table__.update()
                        .where(Node.__table__.c.id == node_id)
                        .values(ai_usage=value))
    _db.session.commit()


def test_a_queued_reply_is_refused_when_the_subscription_lapsed(app):  # noqa: F811
    # Payments (#438): a lapsed account makes no billed call. The task
    # asks again when it runs: a subscription can end while a reply is
    # queued, and a re-dispatch skips create_llm_placeholder.
    alice, system, user_node, llm_node = _build_chain("textmode")
    alice.plan = "free"
    alice.subscription_status = "ended"
    alice.subscription_level = "standard"
    alice.subscription_ever_paid = True
    _db.session.commit()
    _ScriptedProvider.reset([_resp("must not be sent")])
    app.config["PAYMENTS_ENABLED"] = True
    try:
        result = generate_llm_response(
            _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
            source_mode="textmode")
    finally:
        app.config["PAYMENTS_ENABLED"] = False
    assert result["status"] == "refused"
    assert result["reason"] == "subscription_required"
    assert _ScriptedProvider.calls == []
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "subscription has ended" in node.llm_task_error
    # A text reply carries no code: only the Voice screen says one aloud.
    assert "_refused" not in (node.tool_calls_meta or "")


def test_a_voice_reply_queued_when_access_ended_names_its_sentence(app):  # noqa: F811
    # Payments (#438): the access ends after a Voice turn's recording was
    # saved and its reply queued. The reply is refused before any call,
    # its text says the recording is saved, and its code (llm-status
    # ``error_code``) lets the Voice screen say it aloud instead of
    # sounding a failure.
    import json
    from backend.utils.voice_refusals import SUBSCRIPTION_CODES
    alice, system, user_node, llm_node = _build_chain("voice")
    alice.plan = "free"
    alice.subscription_status = "past_due"
    alice.subscription_level = "standard"
    alice.subscription_ever_paid = True
    _db.session.commit()
    _ScriptedProvider.reset([_resp("must not be sent")])
    app.config["PAYMENTS_ENABLED"] = True
    try:
        result = generate_llm_response(
            _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
            source_mode="voice")
    finally:
        app.config["PAYMENTS_ENABLED"] = False
    assert result["reason"] == "subscription_required"
    assert _ScriptedProvider.calls == []
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "failed"
    assert node.llm_task_error == (
        "Your last payment didn't go through, so Loore doesn't reply. "
        "Your recording is saved.")
    refused = [e for e in json.loads(node.tool_calls_meta)
               if e.get("name") == "_refused"]
    assert refused == [{"name": "_refused",
                        "code": SUBSCRIPTION_CODES["past_due"]}]
    # The recording itself stays as it was.
    assert _fresh(user_node.id).get_content() == "what's on my reading list?"


@pytest.mark.parametrize("which", ["system", "user_node", "llm_node"])
def test_a_queued_reply_is_refused_when_a_setting_changed(app, which):  # noqa: F811
    alice, system, user_node, llm_node = _build_chain("textmode")
    target = {"system": system, "user_node": user_node,
              "llm_node": llm_node}[which]
    target.ai_usage = "none"
    _db.session.commit()
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode")

    assert result["status"] == "refused"
    assert result["reason"] == "ai_usage_none"
    assert _ScriptedProvider.calls == []
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "failed"
    assert node.llm_task_error == REPLY_REFUSED_MESSAGE


def test_a_chat_thread_is_answered(app):  # noqa: F811
    alice, system, user_node, llm_node = _build_chain("textmode")
    _ScriptedProvider.reset([_resp("An answer.")])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode")

    assert len(_ScriptedProvider.calls) == 1
    assert _fresh(llm_node.id).get_content() == "An answer."


def test_a_continuation_asks_again(app, monkeypatch):  # noqa: F811
    """The owner marks the message 'none' while the first call runs: the
    interim step stays, the continuation is not sent and fails with the
    refusal."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list", "books", title="Reading List")
    _db.session.commit()
    real_execute = _llm_task_mod._execute_tool_calls

    def execute_then_switch(*args, **kwargs):
        out = real_execute(*args, **kwargs)
        _set_ai_usage(user_node.id, "none")
        return out
    monkeypatch.setattr(_llm_task_mod, "_execute_tool_calls",
                        execute_then_switch)
    _ScriptedProvider.reset([
        _resp("Let me pull that up.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        _resp("must not be sent"),
    ])

    from backend.utils.llm_nodes import AIUsageRefused
    with pytest.raises(AIUsageRefused):
        generate_llm_response(
            _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
            source_mode="textmode")

    assert len(_ScriptedProvider.calls) == 1
    interim = _fresh(llm_node.id)
    assert interim.llm_task_status == "completed"
    cont = Node.query.get(interim.continuation_node_id)
    assert cont.llm_task_status == "failed"
    assert cont.llm_task_error == REPLY_REFUSED_MESSAGE


def test_a_continuation_in_a_chat_thread_goes_on(app):  # noqa: F811
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list", "books", title="Reading List")
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Let me pull that up.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        _resp("Here it is."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode")

    assert len(_ScriptedProvider.calls) == 2
    interim = _fresh(llm_node.id)
    cont = Node.query.get(interim.continuation_node_id)
    assert cont.get_content() == "Here it is."
    assert cont.ai_usage == "chat"
