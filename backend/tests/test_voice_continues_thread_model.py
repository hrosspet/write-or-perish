"""A Voice turn that continues a thread runs on the thread's model (#784).

Peter, 2026-11-24: "this should be fixed in Beta". Before, both apps sent
the account's model with every Voice turn (the web's VoicePage
user.preferred_model, the iPhone's AudioCenter user.preferredModel), and
the Voice routes kept a model the client sent. A Voice turn in a thread on
GPT-6 Astra by an account with Opus 5.5 saved ran on Opus 5.5, and the
thread's prompt cache, which is per model, was read cold.

Now every Voice route picks the model of a turn that continues a thread by
the rule a Text reply follows when it names no model (resolve_chat_model):
the thread's last chat reply's model, kept to its provider when that model
is no longer offered (#487), else the account's, else the server default.
The model the client sent is not used for such a turn. A new Voice thread
still runs on the sent model (the account's).

Routes run in the test_no_replies_for_none harness (in-memory sqlite, the
completion task stubbed), so nothing reaches a model.
"""
import sys
import types
import uuid
from unittest.mock import MagicMock

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user, _client, _node, _prompt_root,
)
from backend.extensions import db as _db
from backend.models import Draft, Node, User

ACCOUNT = "claude-opus-5.5"
# An offered model of the other provider (GPT-6 Astra, which this file
# used first, was retired on 2026-11-24 in favour of GPT-6.2 Astra).
THREAD = "gpt-6.2-astra"


@pytest.fixture
def models(app):  # noqa: F811
    from backend.config import Config
    app.config.update(
        SUPPORTED_MODELS=Config.SUPPORTED_MODELS,
        DEFAULT_LLM_MODEL="claude-opus-5.5",
        CHAT_MODEL_ANTHROPIC=Config.CHAT_MODEL_ANTHROPIC,
        CHAT_MODEL_OPENAI=Config.CHAT_MODEL_OPENAI)
    return app


@pytest.fixture
def finalize_task(monkeypatch):
    """The finalize task, stubbed: the route's call is only recorded."""
    fake = types.ModuleType("backend.tasks.streaming_transcription")
    fake.finalize_draft_streaming = MagicMock()
    fake.finalize_draft_streaming.delay.return_value.id = "task-1"
    monkeypatch.setitem(
        sys.modules, "backend.tasks.streaming_transcription", fake)
    return fake.finalize_draft_streaming


def _speaker(saved=ACCOUNT):
    user = _user()
    user.preferred_model = saved
    _db.session.flush()
    return user


def _reply_on(parent, owner, model, **kw):
    bot = User.query.filter_by(username=model).first()
    if bot is None:
        bot = User(username=model, twitter_id=f"llm-{model}")
        _db.session.add(bot)
        _db.session.flush()
    return _node(bot, parent, content="an answer", node_type="llm",
                 human_owner=owner, llm_model=model, **kw)


def _voice_thread(user, model=THREAD):
    """Voice prompt -> the user's entry -> a reply on *model*."""
    root = _prompt_root(user, "voice")
    entry = _node(user, root, content="a thought")
    reply = _reply_on(entry, user, model)
    _db.session.commit()
    return reply


def _new_reply():
    return Node.query.filter_by(node_type="llm").order_by(
        Node.id.desc()).first()


def _enqueued_model(tasks):
    """The model the reply's job was queued with (create_llm_placeholder:
    parent, reply, model, owner)."""
    return tasks.generate_llm_response.delay.call_args.args[2]


# ── The rule ─────────────────────────────────────────────────────────────

class TestRule:
    def test_a_continued_turn_takes_the_threads_model(self, models):
        from backend.utils.llm_nodes import resolve_chat_model, voice_turn_model
        user = _speaker()
        reply = _voice_thread(user)
        assert voice_turn_model(reply, user, ACCOUNT) == THREAD
        # The same model a Text reply that names none gets there.
        assert resolve_chat_model(reply, user) == (THREAD, "predecessor")

    def test_a_new_thread_takes_the_sent_model(self, models):
        from backend.utils.llm_nodes import voice_turn_model
        user = _speaker()
        _db.session.commit()
        assert voice_turn_model(None, user, "gpt-6.2-astra") == "gpt-6.2-astra"
        # Nothing sent: the account's model.
        assert voice_turn_model(None, user) == ACCOUNT

    def test_a_thread_with_no_reply_yet_takes_the_accounts_model(self, models):
        from backend.utils.llm_nodes import voice_turn_model
        user = _speaker()
        root = _prompt_root(user, "voice")
        _db.session.commit()
        assert voice_turn_model(root, user, "gpt-6.2-astra") == ACCOUNT


# ── Streaming finalize (web and iPhone Voice) ────────────────────────────

class TestStreamingFinalize:
    def _finalize(self, flask_app, user, finalize_task, parent=None,
                  model=ACCOUNT):
        session_id = str(uuid.uuid4())
        draft = Draft(user_id=user.id, session_id=session_id,
                      parent_id=parent.id if parent else None,
                      label="Voice", streaming_status="recording",
                      ai_usage="chat")
        draft.set_content("")
        _db.session.add(draft)
        _db.session.commit()
        body = {"total_chunks": 1, "label": "Voice"}
        if parent is not None:
            body["parent_id"] = parent.id
        if model is not None:
            body["model"] = model
        resp = _client(flask_app, user).post(
            f"/api/drafts/streaming/{session_id}/finalize", json=body)
        assert resp.status_code == 202, resp.get_json()
        return finalize_task.delay.call_args.kwargs["model"]

    def test_a_continued_turn_runs_on_the_threads_model(
            self, models, finalize_task):
        user = _speaker()
        reply = _voice_thread(user)
        # The cache pre-warm and the reply both get this model (the task).
        assert self._finalize(models, user, finalize_task, reply) == THREAD

    def test_a_new_thread_runs_on_the_sent_model(self, models, finalize_task):
        user = _speaker()
        _db.session.commit()
        assert self._finalize(models, user, finalize_task) == ACCOUNT

    def test_nothing_sent_continues_on_the_threads_model(
            self, models, finalize_task):
        user = _speaker()
        reply = _voice_thread(user)
        assert self._finalize(models, user, finalize_task, reply,
                              model=None) == THREAD

    @pytest.mark.parametrize("saved, sent, account", [
        # #775: a saved OpenAI model no longer offered stands for GPT-6.2
        # Astra, which both apps then send. Since GPT-6 Astra and GPT-6
        # Sol were retired (2026-11-24), a saved one stands for its 6.2
        # version; an app that loaded the account before the deploy still
        # sends the retired id.
        ("gpt-5.5", "gpt-6.2-astra", "gpt-6.2-astra"),
        ("gpt-6-astra", "gpt-6.2-astra", "gpt-6.2-astra"),
        ("gpt-6-sol", "gpt-6.2-sol", "gpt-6.2-sol"),
        ("gpt-6-astra", "gpt-6-astra", "gpt-6.2-astra"),
        ("gpt-6-sol", "gpt-6-sol", "gpt-6.2-sol"),
    ])
    def test_a_gpt_6_astra_thread_continues_on_gpt_6_2_astra(
            self, models, finalize_task, saved, sent, account):
        from backend.utils.llm_nodes import effective_preferred_model
        user = _speaker(saved)
        reply = _voice_thread(user, "gpt-6-astra")
        assert effective_preferred_model(user) == account
        assert self._finalize(models, user, finalize_task, reply,
                              model=sent) == "gpt-6.2-astra"
        assert _db.session.get(User, user.id).preferred_model == saved


# ── POST /voice (the apps' fallback after a recording) ───────────────────

class TestVoiceTurnRoute:
    def _turn(self, flask_app, user, parent=None, model=ACCOUNT):
        body = {"content": "and then?"}
        if parent is not None:
            body["parent_id"] = parent.id
        if model is not None:
            body["model"] = model
        resp = _client(flask_app, user).post("/api/voice/", json=body)
        assert resp.status_code == 202, resp.get_json()
        return Node.query.get(resp.get_json()["llm_node_id"])

    def test_a_continued_turn_runs_on_the_threads_model(self, models,
                                                        task_mod):  # noqa: F811
        user = _speaker()
        reply = _voice_thread(user)
        voice_reply = self._turn(models, user, reply)
        assert voice_reply.llm_model == THREAD
        assert _enqueued_model(task_mod) == THREAD

    def test_it_is_the_model_a_text_reply_gets_there(self, models, task_mod):  # noqa: F811
        # The Text reply route, with no model named, on the same thread:
        # the same model, so the same prompt and its cache.
        user = _speaker()
        reply = _voice_thread(user)
        voice_reply = self._turn(models, user, reply)
        entry = _node(user, reply, content="typed")
        _db.session.commit()
        resp = _client(models, user).post(
            f"/api/nodes/{entry.id}/llm", json={"source_mode": "textmode"})
        assert resp.status_code in (200, 202), resp.get_json()
        text_reply = Node.query.get(resp.get_json()["node_id"])
        assert text_reply.llm_model == voice_reply.llm_model == THREAD

    def test_the_next_turn_stays_on_it(self, models, task_mod):  # noqa: F811
        user = _speaker()
        reply = _voice_thread(user)
        first = self._turn(models, user, reply)
        second = self._turn(models, user, first)
        assert second.llm_model == THREAD

    def test_a_new_thread_runs_on_the_sent_model(self, models, task_mod):  # noqa: F811
        user = _speaker()
        _db.session.commit()
        assert self._turn(models, user).llm_model == ACCOUNT

    def test_a_thread_on_a_model_no_longer_offered_keeps_its_provider(
            self, models, task_mod):  # noqa: F811
        # Text's rule (#487): the thread stays with its provider, on the
        # account's model there, else that provider's default; never the
        # Anthropic model this account has saved.
        user = _speaker()
        reply = _voice_thread(user, "gpt-5.5")
        voice_reply = self._turn(models, user, reply)
        assert voice_reply.llm_model == "gpt-6.2-astra"

    def test_a_thread_of_the_accounts_provider_keeps_its_own_model(
            self, models, task_mod):  # noqa: F811
        # Not only across providers: Fable 5.2 in a thread of an account
        # with Opus 5.5 saved.
        user = _speaker()
        reply = _voice_thread(user, "claude-fable-5.2")
        assert self._turn(models, user, reply).llm_model == "claude-fable-5.2"


# ── POST /voice/from-node (Voice Mode on the thread page) ────────────────

class TestFromNode:
    def test_a_reply_under_an_entry_runs_on_the_threads_model(
            self, models, task_mod):  # noqa: F811
        user = _speaker()
        reply = _voice_thread(user)
        entry = _node(user, reply, content="a typed entry, no reply yet")
        _db.session.commit()
        resp = _client(models, user).post(
            f"/api/voice/from-node/{entry.id}", json={"model": ACCOUNT})
        assert resp.status_code == 202, resp.get_json()
        new_reply = Node.query.get(resp.get_json()["llm_node_id"])
        assert new_reply.llm_model == THREAD
        assert _enqueued_model(task_mod) == THREAD

    def test_under_an_entry_outside_an_agentic_thread(self, models, task_mod):  # noqa: F811
        # The branch that adds a Voice prompt under the entry first.
        user = _speaker()
        entry = _node(user, content="a plain entry")
        reply = _reply_on(entry, user, THREAD)
        plain = _node(user, reply, content="and another")
        _db.session.commit()
        resp = _client(models, user).post(
            f"/api/voice/from-node/{plain.id}", json={"model": ACCOUNT})
        assert resp.status_code == 202, resp.get_json()
        assert _new_reply().llm_model == THREAD


# ── Only replies Loore made for this user count (#784 review) ───────────
# Another member's AI reply in a shared thread, and a reply from an
# imported ChatGPT conversation, are not a model this account chose in
# Loore: following them would send its words to another provider. The
# rule (resolve_chat_model) skips them, for Voice and for a Text reply
# that names no model alike, and the account's model answers.

def _members_thread(alice):
    """Bob's public thread with his AI reply on GPT-6.2 Astra, and Alice's
    public entry under it."""
    bob = _user("bob")
    bob.preferred_model = THREAD
    root = _node(bob, content="Bob's post", privacy_level="public")
    bobs_reply = _reply_on(root, bob, THREAD, privacy_level="public")
    entry = _node(alice, bobs_reply, content="Alice answers",
                  privacy_level="public")
    _db.session.commit()
    return entry


def _imported_conversation(alice, slug="gpt-5"):
    """Alice's imported ChatGPT conversation: her message and the
    assistant's reply, labelled with the export's model slug (a key in
    SUPPORTED_MODELS, or the legacy id), both with origin 'chatgpt'."""
    msg = _node(alice, content="imported question")
    msg.origin = "chatgpt"
    reply = _reply_on(msg, alice, slug)
    reply.origin = "chatgpt"
    _db.session.commit()
    return reply


def _text_reply_model(flask_app, user, parent):
    """The Text reply route with no model named (as LLM Response, Try
    again and auto-generate send it for an account with no saved model)."""
    resp = _client(flask_app, user).post(
        f"/api/nodes/{parent.id}/llm", json={"source_mode": "textmode"})
    assert resp.status_code in (200, 202), resp.get_json()
    return Node.query.get(resp.get_json()["node_id"]).llm_model


def _from_node_model(flask_app, user, node):
    resp = _client(flask_app, user).post(
        f"/api/voice/from-node/{node.id}", json={"model": ACCOUNT})
    assert resp.status_code == 202, resp.get_json()
    return Node.query.get(resp.get_json()["llm_node_id"]).llm_model


class TestOnlyTheUsersOwnRepliesCount:
    def test_voice_under_another_members_reply(self, models, task_mod):  # noqa: F811
        alice = _speaker()
        entry = _members_thread(alice)
        assert _from_node_model(models, alice, entry) == ACCOUNT
        assert TestVoiceTurnRoute()._turn(models, alice, entry).llm_model \
            == ACCOUNT

    def test_voice_finalize_under_another_members_reply(
            self, models, finalize_task):
        alice = _speaker()
        entry = _members_thread(alice)
        assert TestStreamingFinalize()._finalize(
            models, alice, finalize_task, entry) == ACCOUNT

    def test_text_under_another_members_reply(self, models, task_mod):  # noqa: F811
        alice = _speaker()
        entry = _members_thread(alice)
        assert _text_reply_model(models, alice, entry) == ACCOUNT

    @pytest.mark.parametrize("slug", ["gpt-5", "gpt-4.5-preview"])
    def test_voice_in_an_imported_conversation(self, models, task_mod,  # noqa: F811
                                               slug):
        alice = _speaker()
        reply = _imported_conversation(alice, slug)
        assert TestVoiceTurnRoute()._turn(models, alice, reply).llm_model \
            == ACCOUNT
        entry = _node(alice, reply, content="typed under it")
        _db.session.commit()
        assert _from_node_model(models, alice, entry) == ACCOUNT

    def test_voice_finalize_in_an_imported_conversation(
            self, models, finalize_task):
        alice = _speaker()
        reply = _imported_conversation(alice)
        assert TestStreamingFinalize()._finalize(
            models, alice, finalize_task, reply) == ACCOUNT

    def test_text_in_an_imported_conversation(self, models, task_mod):  # noqa: F811
        alice = _speaker()
        reply = _imported_conversation(alice)
        entry = _node(alice, reply, content="typed under it")
        _db.session.commit()
        assert _text_reply_model(models, alice, entry) == ACCOUNT

    def test_the_users_own_reply_below_them_counts(self, models,
                                                   task_mod):  # noqa: F811
        # Once Loore has replied to Alice in the thread, that reply's
        # model is the thread's model for her.
        alice = _speaker()
        entry = _members_thread(alice)
        own = _reply_on(entry, alice, "claude-fable-5.2")
        _db.session.commit()
        assert TestVoiceTurnRoute()._turn(models, alice, own).llm_model \
            == "claude-fable-5.2"
