"""An issue filed during a reply turn gets the app the turn was asked from.

When the user confirms a pending issue by voice or text ("yes, file it"),
the model calls apply_github_issue inside generate_llm_response. The turn
runs with no request, so its app is the one create_llm_placeholder
stamped on the reply placeholder (CLIENT_MARKER); an unstamped turn files
the issue with no platform label. The request side is in
test_issue_platform.py.

Runs the real task body through the test_retrieval_loop harness (identity
@celery.task, scripted provider, test flask_app); nothing reaches a real
model, and the GitHub call is replaced by a recorder.
"""
import json
from unittest.mock import MagicMock

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, generate_llm_response, _FakeSelf, _ScriptedProvider, _build_chain,
    _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import Draft, Node
from backend.utils.client_platform import CLIENT_MARKER


def _confirm_turn(alice, user_node, llm_user_id, client):
    """Under the harness chain's first message: an issue proposal (with
    its pending draft), the user's confirmation, and the placeholder for
    the reply to it, stamped with *client* as create_llm_placeholder
    would."""
    proposal = Node(user_id=llm_user_id, human_owner_id=alice.id,
                    parent_id=user_node.id, node_type="llm",
                    llm_model="gpt-5", llm_task_status="completed",
                    privacy_level="private", ai_usage="chat",
                    tool_calls_meta=json.dumps([
                        {"name": "propose_github_issue",
                         "status": "success"}]))
    proposal.set_content(
        "### Issue Title\nRecord button does nothing\n"
        "### Description\nTapping record has no effect.\n"
        "### Category\nbug")
    _db.session.add(proposal)
    _db.session.flush()
    draft = Draft(user_id=alice.id, parent_id=proposal.id,
                  label="github_issue_pending")
    draft.set_content("")
    _db.session.add(draft)

    confirm = Node(user_id=alice.id, human_owner_id=alice.id,
                   parent_id=proposal.id, node_type="user",
                   privacy_level="private", ai_usage="chat")
    confirm.set_content("yes, file it")
    _db.session.add(confirm)
    _db.session.flush()

    placeholder = Node(user_id=llm_user_id, human_owner_id=alice.id,
                       parent_id=confirm.id, node_type="llm",
                       llm_model="gpt-5", llm_task_status="pending",
                       privacy_level="private", ai_usage="chat")
    placeholder.set_content("[LLM response generation pending...]")
    if client:
        placeholder.tool_calls_meta = json.dumps(
            [{"name": CLIENT_MARKER, "client": client}])
    _db.session.add(placeholder)
    _db.session.commit()
    return confirm, placeholder


@pytest.mark.parametrize("client", ["ios", "web", None])
def test_apply_github_issue_labels_the_turns_app(
        app, monkeypatch, client):  # noqa: F811
    import backend.utils.github as github
    create = MagicMock(return_value={
        "url": "https://github.com/owner/repo/issues/9", "number": 9})
    monkeypatch.setattr(github, "create_github_issue", create)

    alice, _system, user_node, llm_node = _build_chain("voice")
    confirm, placeholder = _confirm_turn(
        alice, user_node, llm_node.user_id, client)
    _ScriptedProvider.reset([
        _resp("Filing it now.", tool_calls=[{
            "id": "t1", "name": "apply_github_issue", "input": {}}]),
        _resp("Done, it's filed."),
    ])

    generate_llm_response(
        _FakeSelf(), confirm.id, placeholder.id, "gpt-5", alice.id,
        source_mode="voice")

    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["platform"] == client
    assert kwargs["title"] == "Record button does nothing"
    assert kwargs["category"] == "bug"
    # The proposal was filed: its draft is gone.
    assert Draft.query.filter_by(label="github_issue_pending").count() == 0
    interim = _fresh(placeholder.id)
    applied = [m for m in json.loads(interim.tool_calls_meta)
               if m.get("name") == "apply_github_issue"]
    assert applied and applied[0]["status"] == "success"
