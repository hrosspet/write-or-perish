"""Long threads (#648; Peter, 2026-11-12): a notice at 90 % of a hard
limit on the size of the next AI reply's prompt, and the limit itself.

The size is known before the model is called, with no call: the thread's
last measured prompt (APICostLog.input_tokens of the first call of the
newest AI turn on the path) plus what was written since, at the thread's
tokens per character (utils/thread_size). At the limit every path that
asks for a reply refuses it with ``thread_too_long`` and writing still
works; the thread page carries ``thread_length``.

Same harness as test_no_replies_for_none (minimal Flask app, completion
task module stubbed, the finalize task body run with the celery glue
stubbed), so nothing reaches a model. The task's own measurement is in
test_thread_size_task.py.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, st, _user, _client, _node, _reply, _prompt_root,
    _voice_draft, _llm_count,
)
from backend.extensions import db as _db
from backend.models import APICostLog, Draft, Node

CODE = "thread_too_long"
LIMIT = 10_000          # notice from 9_000


@pytest.fixture
def limited(app):  # noqa: F811
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    app.config["THREAD_PROMPT_NOTICE_FRACTION"] = 0.9
    return app


@pytest.fixture
def no_provider(monkeypatch):
    """Fails the test if anything asks a provider for a completion."""
    from backend.llm_providers import LLMProvider
    spy = MagicMock(side_effect=AssertionError("a provider was called"))
    monkeypatch.setattr(LLMProvider, "get_completion", spy)
    monkeypatch.setattr(LLMProvider, "count_tokens", spy)
    return spy


def _units(node, units):
    node.token_count = units
    _db.session.flush()
    return node


def _measure(reply, user, tokens, chars=None, model="gpt-5",
             request_type="conversation", at=None):
    row = APICostLog(
        user_id=user.id, model_id=model, request_type=request_type,
        input_tokens=tokens, output_tokens=10, prompt_chars=chars,
        cost_microdollars=1, request_ref=f"node:{reply.id}",
        created_at=at or datetime.utcnow())
    _db.session.add(row)
    _db.session.flush()
    return row


def _thread(user, measured, entry_units=200, reply_units=100):
    """prompt root -> ten earlier entries (holding most of what was
    measured, as a long thread's entries do) -> entry -> AI reply
    (measured: its first call's prompt held *measured* tokens, at 1 token
    per unit: 4 characters a token) -> the user's next entry, the node a
    reply is asked under."""
    root = _prompt_root(user, "textmode")
    earlier = root
    for i in range(10):
        earlier = _units(_node(user, earlier, content=f"earlier {i}"),
                         max(measured - 1_000, 0) // 10)
    first = _units(_node(user, earlier, content="first words"), 50)
    reply = _units(_reply(first, user, content="an answer"), reply_units)
    _measure(reply, user, measured, chars=measured * 4)
    entry = _units(_node(user, reply, content="more words"), entry_units)
    _db.session.commit()
    return root, reply, entry


def _refused(resp):
    assert resp.status_code == 409, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["code"] == CODE
    from backend.utils.thread_size import (
        CONTENT_TOO_LARGE_MESSAGE, THREAD_TOO_LONG_MESSAGE,
    )
    assert data["error"] in (THREAD_TOO_LONG_MESSAGE,
                             CONTENT_TOO_LARGE_MESSAGE)
    return data


# ── The measure ──────────────────────────────────────────────────────────

class TestEstimate:
    def _estimate(self, node, user, model="gpt-5"):
        from backend.utils.thread_size import chain_for, estimate_reply_tokens
        chain = chain_for(node)
        return estimate_reply_tokens(chain.nodes, user.id, model,
                                     reads=chain.reads)

    def test_last_prompt_plus_what_was_written_since(self, limited):
        from backend.utils.thread_size import MESSAGE_OVERHEAD_UNITS as O
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=5_000)
        # The reply's own text and the new entry, at the thread's measured
        # 1 token per 4 characters, each with its message overhead.
        assert self._estimate(entry, alice) == 5_000 + (100 + O) + (200 + O)

    @pytest.mark.parametrize("measured,state", [
        (5_000, None), (8_800, "notice"), (9_700, "limit")])
    def test_notice_from_90_percent_limit_at_the_limit(self, limited,
                                                       measured, state):
        from backend.utils.thread_size import thread_length
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=measured)
        tokens, found = thread_length(entry, alice.id, "gpt-5")
        assert found == state, tokens

    def test_off_when_the_setting_is_zero(self, app):  # noqa: F811
        from backend.utils.thread_size import thread_length
        app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=500_000)
        assert thread_length(entry, alice.id) == (None, None)

    def test_size_not_depth(self, limited):
        """A 3-entry thread holding one large document is over the limit
        before any reply measured it; a deep thread of small entries is
        not (Peter's data: a 6-entry thread was larger than a 280-entry
        one). With no measured reply the estimate is not certain: it shows
        the notice, and the completion task decides with a count."""
        from backend.utils.thread_size import SIZE, thread_size
        alice = _user()
        root = _prompt_root(alice, "textmode")
        doc = _units(_node(alice, root, content="a very large document"),
                     12_000)
        ask = _units(_node(alice, doc, content="what do you make of it?"), 8)
        deep = root
        for i in range(40):
            deep = _units(_node(alice, deep, content=f"entry {i}"), 20)
        _db.session.commit()
        size, state = thread_size(ask, alice.id, "gpt-5")
        assert size.tokens > LIMIT
        assert not size.certain
        assert state == "notice"
        assert size.reason == SIZE  # one entry is most of it
        assert thread_size(deep, alice.id, "gpt-5")[1] is None

    def test_a_tool_round_is_measured_from_the_turns_first_call(self, limited):
        """A continuation's prompt held the round's tool results, which
        the next turn does not send again."""
        from backend.utils.thread_size import MESSAGE_OVERHEAD_UNITS as O
        alice = _user()
        root = _prompt_root(alice, "textmode")
        first = _units(_node(alice, root), 50)
        interim = _units(_reply(first, alice, content="let me look"), 10)
        cont = _units(_reply(interim, alice, content="here it is"), 90)
        interim.continuation_node_id = cont.id
        _measure(interim, alice, 4_000, chars=16_000)
        _measure(cont, alice, 9_500, chars=38_000)  # + the injection
        entry = _units(_node(alice, cont), 100)
        _db.session.commit()
        assert self._estimate(entry, alice) == (
            4_000 + (10 + O) + (90 + O) + (100 + O))

    def test_a_read_reply_is_never_the_measurement(self, limited):
        """A Glean's prompt held a day of tweets that later turns do not
        send again."""
        from backend.models import FeedRender
        from backend.utils.thread_size import MESSAGE_OVERHEAD_UNITS as O
        alice = _user()
        _root, reply, entry = _thread(alice, measured=3_000, entry_units=10)
        prompt = _prompt_root(alice, "read_thread")
        prompt.parent_id = entry.id
        read = _units(_reply(prompt, alice, content="picks"), 300)
        _db.session.add(FeedRender(node_id=read.id))
        _measure(read, alice, 99_000, chars=99_000 * 4)
        after = _units(_node(alice, read), 20)
        _db.session.commit()
        estimate = self._estimate(after, alice)
        assert estimate < 5_000
        assert estimate >= 3_000 + (300 + O) + (20 + O)

    def test_only_the_users_own_calls_count(self, limited):
        alice = _user()
        bob = _user("bob")
        root = _node(alice, privacy_level="public")
        reply = _reply(root, bob)
        _measure(reply, bob, 9_990, chars=9_990 * 4)
        mine = _units(_node(alice, reply, privacy_level="public"), 10)
        _db.session.commit()
        assert self._estimate(mine, alice) < 1_000

    def test_a_deleted_entry_in_the_measured_part_drops_the_measurement(
            self, limited):
        """The user deleted the large document: the prompt is smaller
        now, so the measurement taken with it no longer stands."""
        alice = _user()
        root = _prompt_root(alice, "textmode")
        doc = _units(_node(alice, root, content="big"), 9_000)
        reply = _units(_reply(doc, alice), 10)
        _measure(reply, alice, 9_900, chars=9_900 * 4,
                 at=datetime.utcnow() - timedelta(hours=1))
        entry = _units(_node(alice, reply), 10)
        doc.deleted_at = datetime.utcnow()
        _db.session.commit()
        assert self._estimate(entry, alice) < 1_000

    def test_ratio_falls_back_to_the_users_latest_measured_call(self, limited):
        """A measurement from before prompt_chars existed: what was written
        since is converted at the user's latest measured tokens per
        character on the same tokenizer."""
        from backend.utils.thread_size import MESSAGE_OVERHEAD_UNITS as O
        alice = _user()
        root = _prompt_root(alice, "textmode")
        first = _units(_node(alice, root), 10)
        reply = _units(_reply(first, alice), 90)
        _measure(reply, alice, 2_000, chars=None)
        other = _reply(_node(alice), alice)
        _measure(other, alice, 1_000, chars=2_000)  # 2 tokens per unit
        entry = _units(_node(alice, reply), 100)
        _db.session.commit()
        assert self._estimate(entry, alice) == 2_000 + 2 * (90 + O + 100 + O)

    def test_ratio_prior_without_any_measurement(self, limited):
        from backend.utils.thread_size import MESSAGE_OVERHEAD_UNITS as O
        from backend.utils.tokens import TOKENS_PER_UNIT_PRIOR
        alice = _user()
        root = _node(alice)
        _units(root, 1_000)
        _db.session.commit()
        prior = TOKENS_PER_UNIT_PRIOR["threads"]["o200k"]
        assert self._estimate(root, alice) == int((1_000 + O) * prior)

    def test_no_content_is_read(self, limited, monkeypatch):
        """The measure reads stored sizes only: nobody decrypts users'
        content for it."""
        from backend.utils.thread_size import thread_length
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        monkeypatch.setattr(Node, "get_content", MagicMock(
            side_effect=AssertionError("content was read")))
        assert thread_length(entry, alice.id)[1] == "limit"


# ── The thread page ──────────────────────────────────────────────────────

class TestThreadPage:
    @pytest.mark.parametrize("measured,state", [
        (5_000, None), (8_800, "notice"), (9_700, "limit")])
    def test_the_thread_carries_its_length(self, limited, measured, state):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=measured)
        resp = _client(limited, alice).get(f"/api/nodes/{entry.id}")
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["thread_length"] == state

    def test_only_for_the_owner(self, limited):
        alice = _user()
        bob = _user("bob")
        root = _node(alice, privacy_level="public")
        reply = _reply(root, alice)
        reply.privacy_level = "public"
        _measure(reply, alice, 9_990, chars=9_990 * 4)
        root = _units(_node(alice, reply, privacy_level="public"), 100)
        _db.session.commit()
        assert _client(limited, alice).get(
            f"/api/nodes/{root.id}").get_json()["thread_length"] == "limit"
        assert _client(limited, bob).get(
            f"/api/nodes/{root.id}").get_json()["thread_length"] is None


# ── Every path that asks for a reply ─────────────────────────────────────

class TestRefusedEverywhere:
    def test_llm_response_refused_nothing_created(self, limited, task_mod,
                                                  no_provider):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        before = _llm_count()
        _refused(_client(limited, alice).post(
            f"/api/nodes/{entry.id}/llm", json={"model": "gpt-5"}))
        assert _llm_count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_auto_generate_refused_the_same_way(self, limited, task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        _refused(_client(limited, alice).post(
            f"/api/nodes/{entry.id}/llm", json={"auto_generate": True}))
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_llm_response_under_the_notice_size_goes_ahead(self, limited,
                                                           task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=8_800)
        resp = _client(limited, alice).post(
            f"/api/nodes/{entry.id}/llm", json={"model": "gpt-5"})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()

    def test_text_mode_message_kept_reply_skipped(self, limited, task_mod):
        alice = _user()
        root, _reply_, entry = _thread(alice, measured=9_700)
        resp = _client(limited, alice).post(
            f"/api/textmode/{root.id}/message",
            json={"content": "one more thing", "parent_id": entry.id})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert data["llm_error_code"] == CODE
        assert "llm_node_id" not in data
        assert Node.query.get(data["user_node_id"]).get_content() == (
            "one more thing")
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_text_mode_from_node_kept_reply_skipped(self, limited, task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        resp = _client(limited, alice).post(
            f"/api/textmode/from-node/{entry.id}",
            json={"content": "and this"})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert data["llm_error_code"] == CODE
        assert Node.query.get(data["user_node_id"]) is not None
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_a_new_thread_with_one_huge_document_goes_to_the_task(
            self, limited, task_mod):
        """The big-document case on a fresh thread: nothing measured it
        yet, so the route does not refuse on a guess; the completion task
        counts the prompt and decides (test_thread_size_task)."""
        alice = _user()
        _db.session.commit()
        resp = _client(limited, alice).post("/api/textmode/start", json={
            "content": "word " * 12_000, "ai_usage": "chat"})
        assert resp.status_code == 202, resp.get_json()
        assert resp.get_json()["llm_node_id"]
        task_mod.generate_llm_response.delay.assert_called_once()

    def test_a_counted_refusal_on_a_huge_document_says_what_to_do(
            self, limited, task_mod):
        """Once the task counted the entry over the limit, asking again is
        refused before any write, with the text for a large entry: a new
        thread with the same text would not help."""
        from backend.utils.thread_size import (
            CONTENT_TOO_LARGE_MESSAGE, record_refusal,
        )
        alice = _user()
        root = _prompt_root(alice, "textmode")
        doc = _units(_node(alice, root, content="the whole document"),
                     11_000)
        record_refusal(alice.id, "gpt-5", doc.id, 12_000, chars=44_000)
        _db.session.commit()
        data = _refused(_client(limited, alice).post(
            f"/api/nodes/{doc.id}/llm", json={"model": "gpt-5"}))
        assert data["error"] == CONTENT_TOO_LARGE_MESSAGE
        assert "split" in data["error"] and "new thread" not in data["error"]
        page = _client(limited, alice).get(f"/api/nodes/{doc.id}").get_json()
        assert page["thread_length"] == "limit"
        assert page["thread_length_message"] == CONTENT_TOO_LARGE_MESSAGE
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_saved_recording_kept_reply_skipped(self, limited, task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        draft = Draft(user_id=alice.id, parent_id=entry.id,
                      session_id="sess-long", streaming_status="completed",
                      privacy_level="private", ai_usage="chat")
        draft.set_content("recorded words")
        _db.session.add(draft)
        _db.session.commit()
        resp = _client(limited, alice).post(
            "/api/drafts/streaming/sess-long/save-as-node",
            json={"auto_generate": True})
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert data["llm_error_code"] == CODE
        assert "llm_node_id" not in data
        assert Node.query.get(data["id"]).get_content() == "recorded words"
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_upload_reply_skipped_with_the_reason(self, st):  # noqa: F811
        import backend.tasks.transcription as tr
        from backend.utils.thread_size import ENTRY_REPLY_SKIPPED_MESSAGE
        st.flask_app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        _db.session.commit()
        before = _llm_count()
        assert tr._start_upload_reply(entry, entry, "gpt-5") is None
        assert json.loads(Node.query.get(entry.id).llm_task_warnings) == [
            ENTRY_REPLY_SKIPPED_MESSAGE]
        assert _llm_count() == before


class TestVoice:
    def test_availability_says_too_long(self, limited):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        data = _client(limited, alice).get(
            f"/api/voice/availability?parent={entry.id}").get_json()
        assert data["allowed"] is False
        assert data["code"] == CODE
        assert data["error"]

    def test_availability_carries_the_notice(self, limited):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=8_800)
        data = _client(limited, alice).get(
            f"/api/voice/availability?parent={entry.id}").get_json()
        assert data == {"allowed": True, "thread_length": "notice"}

    def test_a_fresh_voice_thread_is_never_too_long(self, limited):
        alice = _user()
        _db.session.commit()
        data = _client(limited, alice).get("/api/voice/availability").get_json()
        assert data == {"allowed": True}

    def test_streaming_init_refused_no_draft(self, limited):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        _refused(_client(limited, alice).post(
            "/api/drafts/streaming/init",
            json={"label": "Voice", "ai_usage": "chat",
                  "parent_id": entry.id}))
        assert Draft.query.count() == 0

    def test_dictation_still_records_there(self, limited):
        # Writing in the thread still works: a dictation asks for no reply.
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        resp = _client(limited, alice).post(
            "/api/drafts/streaming/init",
            json={"ai_usage": "chat", "parent_id": entry.id})
        assert resp.status_code == 201, resp.get_json()

    def test_voice_from_node_refused_nothing_created(self, limited, task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        before = Node.query.count()
        _refused(_client(limited, alice).post(
            f"/api/voice/from-node/{entry.id}", json={}))
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_voice_post_refused_before_writing(self, limited, task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        before = Node.query.count()
        _refused(_client(limited, alice).post("/api/voice/", json={
            "content": "spoken", "parent_id": entry.id}))
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_a_turn_that_crosses_the_limit_keeps_what_was_said(
            self, limited, task_mod):
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_000)
        resp = _client(limited, alice).post("/api/voice/", json={
            "content": "word " * 1_200, "parent_id": entry.id})
        data = _refused(resp)
        kept = Node.query.get(data["user_node_id"])
        assert kept.get_content().startswith("word ")
        task_mod.generate_llm_response.delay.assert_not_called()


class TestVoiceFinalize:
    def _finalize(self, st, user, session_id, parent, model="gpt-5"):  # noqa: F811
        return st.finalize_draft_streaming(
            MagicMock(), session_id, 1, label="Voice", user_id=user.id,
            parent_id=parent.id, model=model)

    def test_recording_saved_reply_skipped_with_the_code(self, st):  # noqa: F811
        from backend.utils.thread_size import (
            RECORDING_REPLY_SKIPPED_MESSAGE, skipped_reply_code,
        )
        st.flask_app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        _voice_draft(alice, "sess-long", "chat", text="spoken words",
                     parent=entry)

        self._finalize(st, alice, "sess-long", entry)

        draft = Draft.query.filter_by(session_id="sess-long").one()
        assert draft.streaming_status == "completed"
        assert draft.llm_node_id is None
        assert draft.streaming_warning == RECORDING_REPLY_SKIPPED_MESSAGE
        assert skipped_reply_code(draft.streaming_warning) == CODE
        kept = Node.query.filter_by(parent_id=entry.id).one()
        assert kept.get_content() == "spoken words"
        st._fake_llm.generate_llm_response.si.assert_not_called()

    def test_no_prewarm_for_a_thread_too_long(self, st):  # noqa: F811
        st.flask_app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=9_700)
        _db.session.commit()
        assert st._voice_thread_too_long(alice.id, entry.id, "gpt-5")
        assert not st._voice_thread_too_long(alice.id, None, "gpt-5")

    def test_under_the_limit_the_turn_gets_its_reply(self, st):  # noqa: F811
        st.flask_app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
        alice = _user()
        _root, _reply_, entry = _thread(alice, measured=8_800)
        _voice_draft(alice, "sess-ok", "chat", text="spoken words",
                     parent=entry)

        self._finalize(st, alice, "sess-ok", entry)

        draft = Draft.query.filter_by(session_id="sess-ok").one()
        assert draft.streaming_warning is None
        assert draft.llm_node_id is not None
        st._fake_llm.generate_llm_response.si.assert_called_once()

    def test_streaming_status_carries_the_code(self, limited):
        from backend.utils.thread_size import RECORDING_REPLY_SKIPPED_MESSAGE
        alice = _user()
        draft = Draft(user_id=alice.id, session_id="sess-st",
                      streaming_status="completed", label="Voice",
                      privacy_level="private", ai_usage="chat",
                      streaming_warning=RECORDING_REPLY_SKIPPED_MESSAGE)
        draft.set_content("words")
        _db.session.add(draft)
        _db.session.commit()
        data = _client(limited, alice).get(
            "/api/drafts/streaming/sess-st/status").get_json()
        assert data["warning"] == RECORDING_REPLY_SKIPPED_MESSAGE
        assert data["warning_code"] == CODE


class TestLlmStatus:
    def test_a_refused_reply_carries_its_code_for_the_owner(self, limited):
        from backend.utils.thread_size import (
            REFUSED_MARKER, THREAD_TOO_LONG_MESSAGE,
        )
        alice = _user()
        bob = _user("bob")
        root = _node(alice, privacy_level="public")
        reply = _reply(root, alice)
        reply.llm_task_status = "failed"
        reply.llm_task_error = THREAD_TOO_LONG_MESSAGE
        reply.privacy_level = "public"
        reply.tool_calls_meta = json.dumps(
            [{"name": REFUSED_MARKER, "code": CODE}])
        _db.session.commit()
        data = _client(limited, alice).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()
        assert data["status"] == "failed"
        assert data["error_code"] == CODE
        assert data["error"] == THREAD_TOO_LONG_MESSAGE
        other = _client(limited, bob).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()
        assert "error_code" not in other


class TestAfterAReply:
    def test_a_finished_reply_brings_the_threads_length(self, limited):
        """llm-status of a finished reply carries thread_length (and at
        the limit its text), so the form and Voice show it on this turn."""
        from backend.utils.thread_size import THREAD_TOO_LONG_MESSAGE
        alice = _user()
        _root, reply, _entry = _thread(alice, measured=9_990)
        reply.llm_task_status = "completed"
        _db.session.commit()
        data = _client(limited, alice).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()
        assert data["thread_length"] == "limit"
        assert data["thread_length_message"] == THREAD_TOO_LONG_MESSAGE

    def test_not_on_an_interim_step(self, limited):
        alice = _user()
        _root, reply, entry = _thread(alice, measured=9_990)
        reply.llm_task_status = "completed"
        reply.continuation_node_id = entry.id
        _db.session.commit()
        data = _client(limited, alice).get(
            f"/api/nodes/{reply.id}/llm-status").get_json()
        assert "thread_length" not in data


class TestWording:
    def test_no_price_no_numbers(self):
        from backend.utils import thread_size as ts
        for text in (ts.THREAD_TOO_LONG_MESSAGE,
                     ts.RECORDING_REPLY_SKIPPED_MESSAGE,
                     ts.ENTRY_REPLY_SKIPPED_MESSAGE,
                     ts.CONTENT_TOO_LARGE_MESSAGE,
                     ts.RECORDING_REPLY_SKIPPED_SIZE_MESSAGE,
                     ts.ENTRY_REPLY_SKIPPED_SIZE_MESSAGE):
            assert not any(ch.isdigit() for ch in text)
            assert "$" not in text and "%" not in text
            assert "token" not in text.lower()

    def test_the_way_on_matches_the_reason(self):
        """A long thread: a new thread. An entry too large: shorten or
        split it (a new thread with the same text would be refused too)."""
        from backend.utils import thread_size as ts
        assert "start a new thread" in ts.THREAD_TOO_LONG_MESSAGE
        assert "new thread" not in ts.CONTENT_TOO_LARGE_MESSAGE
        assert "split" in ts.CONTENT_TOO_LARGE_MESSAGE
        assert "split" in ts.ENTRY_REPLY_SKIPPED_SIZE_MESSAGE
        assert ts.skipped_reply_code(
            ts.RECORDING_REPLY_SKIPPED_SIZE_MESSAGE) == "thread_too_long"
