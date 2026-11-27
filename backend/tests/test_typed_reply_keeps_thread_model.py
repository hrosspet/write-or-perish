"""The thread page's reply requests, as the web and iPhone send them since
#791: no model unless the person picked one in the LLM Response picker on
that page.

Before, both thread pages sent the account's model with every reply
request unless that picker was on screen, so the automatic reply to a
typed message moved a thread on another model to the account's model,
and that turn read the whole thread without the prompt cache (the defect
#784 describes for Voice). Without a model the server takes the thread's
model by resolve_chat_model: the closest earlier chat reply Loore made for
this user, else the account's model. A reply under another member's AI
reply gets the account's model: that member's model is not this user's
choice (#784 review).

Routes run in the test_no_replies_for_none harness (in-memory sqlite, the
completion task stubbed), so nothing reaches a model.
"""
import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _client, _node, _user,
)
from backend.tests.test_voice_continues_thread_model import (  # noqa: F401
    models, _speaker, _reply_on, ACCOUNT, THREAD,
)
from backend.extensions import db as _db
from backend.models import Node

# The automatic reply to a typed message (web requestLlmFor, iPhone
# requestLLM with auto-generate on), and LLM Response or Try again with
# nothing picked.
AUTOMATIC = {"source_mode": "textmode", "auto_generate": True}
ASKED = {"source_mode": "textmode"}


def _reply_model(flask_app, user, parent, body):
    resp = _client(flask_app, user).post(
        f"/api/nodes/{parent.id}/llm", json=body)
    assert resp.status_code in (200, 202), resp.get_json()
    return Node.query.get(resp.get_json()["node_id"]).llm_model


def _own_thread(alice):
    """Alice's thread on GPT-6 Astra (her account's model is Opus 5.5) and
    the entry she has just typed under its reply."""
    first = _node(alice, content="a thought")
    reply = _reply_on(first, alice, THREAD)
    typed = _node(alice, reply, content="typed under the reply")
    _db.session.commit()
    return typed


class TestNothingPicked:
    def test_the_automatic_reply_continues_on_the_threads_model(
            self, models, task_mod):  # noqa: F811
        alice = _speaker()
        typed = _own_thread(alice)
        assert _reply_model(models, alice, typed, AUTOMATIC) == THREAD

    def test_llm_response_continues_on_the_threads_model(
            self, models, task_mod):  # noqa: F811
        alice = _speaker()
        typed = _own_thread(alice)
        assert _reply_model(models, alice, typed, ASKED) == THREAD

    def test_a_thread_with_no_ai_reply_yet_gets_the_accounts_model(
            self, models, task_mod):  # noqa: F811
        alice = _speaker()
        entry = _node(alice, content="a new thread")
        _db.session.commit()
        assert _reply_model(models, alice, entry, AUTOMATIC) == ACCOUNT

    def test_a_reply_typed_under_a_reply_names_no_model_in_text_mode(
            self, models, task_mod):  # noqa: F811
        # The reply box under a Glean reply sends its message here.
        alice = _speaker()
        first = _node(alice, content="a thought")
        reply = _reply_on(first, alice, THREAD)
        _db.session.commit()
        resp = _client(models, alice).post(
            f"/api/textmode/from-node/{reply.id}",
            json={"content": "tell me more", "ai_usage": "chat",
                  "auto_generate": True})
        assert resp.status_code == 202, resp.get_json()
        assert Node.query.get(resp.get_json()["llm_node_id"]).llm_model \
            == THREAD


class TestUnderAnotherMembersReply:
    def test_a_typed_reply_gets_the_accounts_model(
            self, models, task_mod):  # noqa: F811
        # Bob's thread, shared with his circles, with his AI reply on
        # GPT-6 Astra; Alice's private entry under it gets an automatic
        # reply. Bob's model is not Alice's choice.
        alice = _speaker()
        bob = _user("bob")
        bob.preferred_model = THREAD
        root = _node(bob, content="Bob's post", privacy_level="circles")
        bobs_reply = _reply_on(root, bob, THREAD, privacy_level="circles")
        typed = _node(alice, bobs_reply, content="Alice answers")
        _db.session.commit()
        assert _reply_model(models, alice, typed, AUTOMATIC) == ACCOUNT

    def test_llm_response_in_a_public_thread_gets_the_accounts_model(
            self, models, task_mod):  # noqa: F811
        alice = _speaker()
        bob = _user("bob")
        root = _node(bob, content="Bob's post", privacy_level="public")
        bobs_reply = _reply_on(root, bob, THREAD, privacy_level="public")
        typed = _node(alice, bobs_reply, content="Alice answers",
                      privacy_level="public")
        _db.session.commit()
        assert _reply_model(models, alice, typed, ASKED) == ACCOUNT


class TestAPickWins:
    def test_a_model_picked_on_the_page_is_used(
            self, models, task_mod):  # noqa: F811
        # The person picks their account's model to move the thread to
        # it: the same request an installed iPhone build sends unpicked,
        # which is why the server cannot tell them apart.
        alice = _speaker()
        typed = _own_thread(alice)
        assert _reply_model(models, alice, typed,
                            {**AUTOMATIC, "model": ACCOUNT}) == ACCOUNT


class TestWithTheFormsLevel:
    """The reply box under a Glean reply sends the form's privacy level
    (#794: the message keeps the level the form shows) and, with nothing
    picked, no model (#791). Both hold in one request: the message, its
    prompt entry and the automatic reply follow the level, and the reply
    runs on the thread's model, or on the model picked."""

    @pytest.mark.parametrize("sent, expected", [
        (None, THREAD), (ACCOUNT, ACCOUNT)])
    def test_a_private_reply_under_a_public_reply(
            self, models, task_mod, sent, expected):  # noqa: F811
        alice = _speaker()
        first = _node(alice, content="a thought", privacy_level="public")
        reply = _reply_on(first, alice, THREAD, privacy_level="public")
        _db.session.commit()
        body = {"content": "tell me more", "privacy_level": "private",
                "ai_usage": "chat", "auto_generate": True}
        if sent:
            body["model"] = sent
        resp = _client(models, alice).post(
            f"/api/textmode/from-node/{reply.id}", json=body)
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert Node.query.get(data["user_node_id"]).privacy_level \
            == "private"
        assert Node.query.get(data["prompt_node_id"]).privacy_level \
            == "private"
        answer = Node.query.get(data["llm_node_id"])
        assert answer.llm_model == expected
        assert answer.privacy_level == "private"
