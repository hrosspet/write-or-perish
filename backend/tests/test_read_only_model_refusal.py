"""A reply that is not a read, asked for on a read-only model, is refused.

Peter, 2026-10-02: "Changing providers is however never acceptable", not
even as a fallback. A read-only model ("chat": False) runs reads only, so
a chat reply sent with one (an iPhone build whose picker ignores "chat",
a web tab from before the deploy, a direct call) is refused with 400
``{"code": "model_read_only"}`` instead of running on the chat default,
which can be another provider. The routes that write the user's entry
first refuse before writing; a finished Voice recording and a saved
dictation keep the entry and say why there is no reply. A read turn still
runs on the read-only model (test_read_routes.TestReadOnlyModels).

Same harness as test_no_replies_for_none (minimal Flask app, completion
task module stubbed, the finalize task body run with the celery glue
stubbed), so nothing reaches a model.
"""
import uuid
from unittest.mock import MagicMock

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, st, _user, _client, _node, _reply, _prompt_root,
    _voice_draft, _llm_count,
)
from backend.extensions import db as _db
from backend.models import Draft, Node

CODE = "model_read_only"

MODELS = {
    "gpt-5": {"provider": "openai", "api_model": "gpt-5",
              "display_name": "GPT-5", "read": True},
    "gpt-6.1-sol": {"provider": "openai", "api_model": "gpt-6.1-sol",
                    "display_name": "GPT-6.1 Sol", "read": True,
                    "chat": False},
    "claude-sonnet-5.5": {"provider": "anthropic",
                          "api_model": "claude-sonnet-5-5",
                          "display_name": "Sonnet 5.5", "read": True,
                          "chat": False},
    "claude-test": {"provider": "anthropic", "api_model": "c",
                    "display_name": "Claude Test"},
}


@pytest.fixture
def ro(app):  # noqa: F811
    app.config["SUPPORTED_MODELS"] = MODELS
    return app


@pytest.fixture
def tasks(task_mod):  # noqa: F811
    """The stubbed completion task module (test_no_replies_for_none)."""
    return task_mod


@pytest.fixture
def no_provider(monkeypatch):
    """Fails the test if anything asks a provider for a completion."""
    from backend.llm_providers import LLMProvider
    spy = MagicMock(side_effect=AssertionError("a provider was called"))
    monkeypatch.setattr(LLMProvider, "get_completion", spy)
    return spy


def _refused(resp, model="GPT-6.1 Sol"):
    assert resp.status_code == 400, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["code"] == CODE
    assert data["error"] == (f"{model} is only for Read. Choose another "
                             "model for replies.")
    return data


def _read_prompt_with_reply(user):
    """read_thread prompt -> its read reply, with a pinned render: a
    finished read (ca_feed.read_reply_ids)."""
    from backend.models import FeedRender
    prompt = _prompt_root(user, "read_thread")
    reply = _reply(prompt, user, content="picks")
    _db.session.add(FeedRender(node_id=reply.id))
    _db.session.commit()
    return prompt, reply


# ── The rule ─────────────────────────────────────────────────────────────

class TestRefusalRule:
    def test_a_new_thread_is_never_a_read(self, ro):
        from backend.utils.llm_nodes import read_only_model_refusal
        assert read_only_model_refusal("gpt-6.1-sol").model_id == "gpt-6.1-sol"
        assert read_only_model_refusal("gpt-5") is None
        # Not active at all: left to create_llm_placeholder.
        assert read_only_model_refusal("nope") is None

    def test_a_new_entry_after_a_read_reply_is_chat(self, ro):
        from backend.utils.llm_nodes import read_only_model_refusal
        alice = _user()
        prompt, reply = _read_prompt_with_reply(alice)
        # A reply directly under the read reply is a read again ...
        assert read_only_model_refusal("gpt-6.1-sol", reply) is None
        # ... one under a new entry written there is a chat turn.
        assert read_only_model_refusal(
            "gpt-6.1-sol", reply, new_entry=True) is not None
        # Before any read reply, a new entry under the prompt is the read.
        assert read_only_model_refusal(
            "gpt-6.1-sol", prompt, new_entry=True) is None


# ── Text mode ────────────────────────────────────────────────────────────

class TestTextMode:
    def test_start_refused_before_anything_is_written(self, ro, tasks,
                                                      no_provider):
        alice = _user()
        _db.session.commit()
        _refused(_client(ro, alice).post("/api/textmode/start", json={
            "content": "hello", "model": "gpt-6.1-sol"}))
        assert Node.query.count() == 0
        tasks.generate_llm_response.delay.assert_not_called()

    def test_start_without_a_reply_still_saves(self, ro, tasks):
        alice = _user()
        _db.session.commit()
        resp = _client(ro, alice).post("/api/textmode/start", json={
            "content": "hello", "model": "gpt-6.1-sol",
            "auto_generate": False})
        assert resp.status_code == 202, resp.get_json()
        tasks.generate_llm_response.delay.assert_not_called()

    def test_message_refused_before_anything_is_written(self, ro, tasks,
                                                        no_provider):
        alice = _user()
        root = _prompt_root(alice, "textmode")
        msg = _node(alice, root)
        _db.session.commit()
        before = Node.query.count()
        _refused(_client(ro, alice).post(
            f"/api/textmode/{root.id}/message",
            json={"content": "hello", "parent_id": msg.id,
                  "model": "claude-sonnet-5.5"}), model="Sonnet 5.5")
        assert Node.query.count() == before
        tasks.generate_llm_response.delay.assert_not_called()

    def test_from_node_under_a_read_reply_refused(self, ro, tasks,
                                                  no_provider):
        # The reply box under a read reply (#323): a conversation about
        # the picks is a chat turn.
        alice = _user()
        _, reply = _read_prompt_with_reply(alice)
        before = Node.query.count()
        _refused(_client(ro, alice).post(
            f"/api/textmode/from-node/{reply.id}",
            json={"content": "why #2?", "model": "gpt-6.1-sol"}))
        assert Node.query.count() == before
        tasks.generate_llm_response.delay.assert_not_called()

    def test_a_chat_model_still_replies(self, ro, tasks):
        alice = _user()
        _, reply = _read_prompt_with_reply(alice)
        resp = _client(ro, alice).post(
            f"/api/textmode/from-node/{reply.id}",
            json={"content": "why #2?", "model": "gpt-5"})
        assert resp.status_code == 202, resp.get_json()
        tasks.generate_llm_response.delay.assert_called_once()


# ── Voice ────────────────────────────────────────────────────────────────

class TestVoiceRoutes:
    def test_fresh_turn_refused(self, ro, tasks, no_provider):
        alice = _user()
        _db.session.commit()
        _refused(_client(ro, alice).post("/api/voice/", json={
            "content": "spoken words", "model": "claude-sonnet-5.5"}),
            model="Sonnet 5.5")
        assert Node.query.count() == 0
        tasks.generate_llm_response.delay.assert_not_called()

    def test_continued_turn_refused_and_its_audio_stays_in_the_draft(
            self, ro, tasks, no_provider):
        alice = _user()
        root = _prompt_root(alice, "voice")
        reply = _reply(_node(alice, root), alice)
        session_id = str(uuid.uuid4())
        draft = Draft(user_id=alice.id, session_id=session_id,
                      streaming_status="completed", label="Voice",
                      privacy_level="private", ai_usage="chat")
        draft.set_content("spoken words")
        _db.session.add(draft)
        _db.session.commit()
        before = Node.query.count()
        _refused(_client(ro, alice).post("/api/voice/", json={
            "content": "spoken words", "parent_id": reply.id,
            "session_id": session_id, "model": "gpt-6.1-sol"}))
        assert Node.query.count() == before
        assert Draft.query.filter_by(session_id=session_id).count() == 1
        tasks.generate_llm_response.delay.assert_not_called()

    def test_from_node_refused(self, ro, tasks, no_provider):
        alice = _user()
        root = _prompt_root(alice, "voice")
        msg = _node(alice, root)
        _db.session.commit()
        before = Node.query.count()
        _refused(_client(ro, alice).post(
            f"/api/voice/from-node/{msg.id}", json={"model": "gpt-6.1-sol"}))
        assert Node.query.count() == before
        tasks.generate_llm_response.delay.assert_not_called()

    def test_from_node_without_a_prompt_refused(self, ro, tasks):
        # The branch that adds a Voice prompt under the node first.
        alice = _user()
        plain = _node(alice)
        _db.session.commit()
        before = Node.query.count()
        _refused(_client(ro, alice).post(
            f"/api/voice/from-node/{plain.id}", json={"model": "gpt-6.1-sol"}))
        assert Node.query.count() == before


class TestVoiceFinalize:
    def _finalize(self, st, user, session_id, model, parent=None, chunks=1):  # noqa: F811
        st.flask_app.config["SUPPORTED_MODELS"] = MODELS
        return st.finalize_draft_streaming(
            MagicMock(), session_id, chunks, label="Voice", user_id=user.id,
            parent_id=parent.id if parent else None, model=model)

    def test_recording_is_kept_without_a_reply(self, st):  # noqa: F811
        user = _user()
        _voice_draft(user, "sess-ro", "chat", text="spoken words")

        result = self._finalize(st, user, "sess-ro", "claude-sonnet-5.5")

        assert result["status"] == "completed"
        draft = Draft.query.filter_by(session_id="sess-ro").one()
        assert draft.streaming_status == "completed"
        assert draft.llm_node_id is None
        assert draft.streaming_warning == (
            "Your recording is saved. Loore didn't reply because Sonnet 5.5 "
            "is only for Read. Choose another model for replies.")
        entry = Node.query.filter(Node.parent_id.isnot(None)).one()
        assert entry.get_content() == "spoken words"
        assert _llm_count() == 0
        st._fake_llm.generate_llm_response.si.assert_not_called()

    def test_continued_thread_keeps_the_turn_under_it(self, st):  # noqa: F811
        user = _user()
        root = _prompt_root(user, "voice")
        reply = _reply(_node(user, root), user)
        _voice_draft(user, "sess-ro2", "chat", text="more words",
                     parent=reply)

        self._finalize(st, user, "sess-ro2", "gpt-6.1-sol", parent=reply)

        draft = Draft.query.filter_by(session_id="sess-ro2").one()
        assert "GPT-6.1 Sol is only for Read" in draft.streaming_warning
        entry = Node.query.filter_by(parent_id=reply.id).one()
        assert entry.get_content() == "more words"
        assert _llm_count() == 1  # only the reply that was there
        st._fake_llm.generate_llm_response.si.assert_not_called()

    @pytest.mark.parametrize("model, warmed", [
        ("claude-sonnet-5.5", False),   # read only: no warm
        ("claude-test", True),          # the control: a chat model warms
    ])
    def test_the_cache_warm_runs_only_on_a_chat_model(
            self, st, model, warmed):  # noqa: F811
        # The warm sends the prompt and the transcript so far to the
        # model; a read-only one writes no reply here.
        user = _user()
        draft = _voice_draft(user, f"sess-warm-{warmed}", "chat")
        draft.set_content("x" * 600)
        _db.session.commit()

        self._finalize(st, user, f"sess-warm-{warmed}", model, chunks=0)

        assert st._fake_llm.prewarm_anthropic_cache.delay.called is warmed


# ── Saved dictation and uploads ──────────────────────────────────────────

class TestSaveAsNode:
    def test_reply_skipped_entry_kept(self, ro, tasks):
        alice = _user()
        draft = Draft(user_id=alice.id, session_id="sess-save-ro",
                      streaming_status="completed",
                      privacy_level="private", ai_usage="chat")
        draft.set_content("recorded words")
        _db.session.add(draft)
        _db.session.commit()
        resp = _client(ro, alice).post(
            "/api/drafts/streaming/sess-save-ro/save-as-node",
            json={"auto_generate": True, "model": "gpt-6.1-sol"})
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert data["llm_error_code"] == CODE
        assert "GPT-6.1 Sol is only for Read" in data["llm_error"]
        assert "llm_node_id" not in data
        assert Node.query.get(data["id"]).get_content() == "recorded words"
        tasks.generate_llm_response.delay.assert_not_called()


class TestUploadReply:
    def test_refused_before_the_upload_is_stored(self, ro):
        from backend.routes.nodes import _upload_reply_options
        alice = _user()
        _db.session.commit()
        with ro.test_request_context():
            agentic, model, err = _upload_reply_options(
                {"auto_generate": "true", "model": "gpt-6.1-sol"}, None,
                "chat")
            assert model is None
            resp, status = err
            assert status == 400
            assert resp.get_json()["code"] == CODE
            assert _upload_reply_options(
                {"auto_generate": "true", "model": "gpt-5"}, None,
                "chat") == (False, "gpt-5", None)
        assert alice.id  # nothing else to check: no node exists yet

    def test_the_transcription_task_names_the_reason(self, st):  # noqa: F811
        # The task's own check (a request accepted before this rule).
        import json
        import backend.tasks.transcription as tr
        st.flask_app.config["SUPPORTED_MODELS"] = MODELS
        user = _user()
        entry = _node(user, content="transcribed words")
        _db.session.commit()
        assert tr._start_upload_reply(entry, entry, "gpt-6.1-sol") is None
        assert json.loads(Node.query.get(entry.id).llm_task_warnings) == [
            "Your entry is saved. GPT-6.1 Sol is only for Read. Choose "
            "another model for replies."]
        assert _llm_count() == 0
