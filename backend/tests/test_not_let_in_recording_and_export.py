"""Payments on (#438): what an account that is not let in still reaches.

Two exceptions to the gate (utils/subscriptions.refuse_not_let_in), both
decided on 2026-11-13:

- A recording in progress when the account stops being let in (a trial
  whose first payment fails while the user records). Peter: "User's
  recorded message should be stored on Loore's servers, possibly even
  transcribed (transcription is cheap). Just further AI use should be
  blocked." Only the calls of that recording pass: its remaining chunks,
  its finalize, its status and stream; for an upload, its chunks, its
  finalize and its transcription status. Nothing starts, no other
  account's session, nothing older than RECORDING_IN_PROGRESS_MAX_AGE,
  and a session left behind earlier takes no more audio.
- The download of the account's own data. Peter: "yes, Ben should be able
  to export". It makes no model call.

Every other route still answers 402 no_subscription
(test_payments_gate.test_state_none_is_refused_on_every_other_route, whose
open list now has the export). Same harness: the real app, sqlite in
memory, the task modules stubbed where a route queues one.
"""
import json
import sys
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from backend.tests.test_payments_gate import (  # noqa: F401 (fixture)
    real_app, _user, _client)
from backend.extensions import db as _db
from backend.models import APICostLog, Draft, Node, NodeTranscriptChunk

FRAGMENT = b"\x00\x00\x01\x10moofdata-no-init-here"


@pytest.fixture
def app(real_app, monkeypatch, tmp_path):  # noqa: F811
    import backend.routes.drafts as drafts_mod
    import backend.routes.nodes as nodes_mod
    import backend.utils.audio_storage as audio_storage
    for mod in (drafts_mod, nodes_mod, audio_storage):
        monkeypatch.setattr(mod, "AUDIO_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(nodes_mod, "get_openai_chat_key", lambda cfg: "key")
    streaming = MagicMock()
    streaming.finalize_draft_streaming.delay.return_value = MagicMock(
        id="fin")
    streaming.transcribe_chunk_batch.delay.return_value = MagicMock(
        id="batch")
    transcription = MagicMock()
    transcription.transcribe_audio.delay.return_value = MagicMock(id="tr")
    monkeypatch.setitem(sys.modules, "backend.tasks.streaming_transcription",
                        streaming)
    monkeypatch.setitem(sys.modules, "backend.tasks.transcription",
                        transcription)
    real_app._tasks = {"streaming": streaming, "transcription": transcription,
                       "root": tmp_path}
    return real_app


def _not_let_in(name="ben"):
    """A trial whose first payment failed: never paid, not cancelled by
    its owner, not an alpha tester."""
    from backend.utils.subscriptions import access_state
    u = _user(name, subscription_status="past_due",
              subscription_level="standard", subscription_trial_used=True)
    assert access_state(u) == "none"
    return u


def _session(app, user, age=timedelta(minutes=5),
             heartbeat=timedelta(seconds=10), status="recording",
             label="Voice"):
    now = datetime.utcnow()
    draft = Draft(user_id=user.id, session_id=str(uuid.uuid4()),
                  label=label, streaming_status=status, ai_usage="chat",
                  privacy_level="private", streaming_mime_type="audio/webm",
                  created_at=now - age,
                  streaming_heartbeat_at=(now - heartbeat
                                          if heartbeat is not None else None))
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    (app._tasks["root"] / "drafts" / str(user.id) / draft.session_id).mkdir(
        parents=True, exist_ok=True)
    return draft


def _chunk(client, sid, index=5):
    return client.post(f"/api/drafts/streaming/{sid}/audio-chunk", data={
        "chunk": (BytesIO(FRAGMENT), "chunk.webm"),
        "chunk_index": str(index), "mime_type": "audio/webm",
    }, content_type="multipart/form-data")


def _refused(resp):
    assert resp.status_code == 402, (resp.status_code,
                                     resp.get_data(as_text=True))
    assert resp.get_json()["error"] == "no_subscription"


def _passed(resp):
    body = resp.get_json(silent=True) or {}
    assert body.get("error") != "no_subscription", body
    assert resp.status_code < 400, (resp.status_code, body)
    return body


def _gate_lets(app, user, method, path):
    """The gate's answer for a request that the test client can't make
    (the transcription stream never ends)."""
    from backend.utils.subscriptions import finishes_recording_in_progress
    with app.test_request_context(path, method=method):
        from flask import request
        return finishes_recording_in_progress(user, request)


# ── A streaming recording (Voice or dictation) ──────────────────────────

class TestARecordingInProgress:
    @pytest.mark.parametrize("label", ["Voice", None])
    def test_its_chunks_finalize_and_status_pass(self, app, label):
        ben = _not_let_in()
        draft = _session(app, ben, label=label)
        client = _client(app, ben)

        _passed(_chunk(client, draft.session_id))
        assert NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id, chunk_index=5).one().status == \
            "stored"
        _passed(client.get(
            f"/api/drafts/streaming/{draft.session_id}/status"))
        body = {"total_chunks": 6}
        if label:
            body["label"] = label
        _passed(client.post(
            f"/api/drafts/streaming/{draft.session_id}/finalize", json=body))
        app._tasks["streaming"].finalize_draft_streaming.delay \
            .assert_called_once()
        _db.session.refresh(draft)
        assert draft.streaming_status == "finalizing"
        # The transcript and the turn's end are reported to the screen.
        _passed(client.get(
            f"/api/drafts/streaming/{draft.session_id}/status"))
        assert _gate_lets(
            app, ben, "GET",
            f"/api/sse/drafts/{draft.session_id}/transcription-stream")

    def test_the_stream_of_a_finished_recording_passes(self, app):
        ben = _not_let_in()
        draft = _session(app, ben, status="completed", heartbeat=None)
        assert _gate_lets(
            app, ben, "GET",
            f"/api/sse/drafts/{draft.session_id}/transcription-stream")
        _passed(_client(app, ben).get(
            f"/api/drafts/streaming/{draft.session_id}/status"))

    def test_the_tab_may_release_it(self, app):
        ben = _not_let_in()
        draft = _session(app, ben)
        _passed(_client(app, ben).post(
            f"/api/drafts/streaming/{draft.session_id}/release"))


class TestALongGap:
    @pytest.mark.parametrize("gap", [timedelta(minutes=2), None])
    def test_a_long_pause_or_a_relaunch_loses_nothing(self, app, gap):
        # Nothing sent for longer than the 45 s liveness window: the iPhone
        # sends no chunk and keeps no transcription stream while a Voice
        # recording is paused (gap), and a killed app's relaunch releases
        # the session before it sends the rest (None: the stamp cleared).
        # The rest of the recording is still taken, and the turn finalized.
        ben = _not_let_in()
        draft = _session(app, ben, heartbeat=gap)
        client = _client(app, ben)
        _passed(_chunk(client, draft.session_id, index=5))
        _passed(_chunk(client, draft.session_id, index=6))
        assert _gate_lets(
            app, ben, "GET",
            f"/api/sse/drafts/{draft.session_id}/transcription-stream")
        _passed(client.post(
            f"/api/drafts/streaming/{draft.session_id}/finalize",
            json={"total_chunks": 7, "label": "Voice"}))
        assert NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id).count() == 2


class TestNothingElseOpens:
    def test_no_other_device_and_no_recovery_continues_it(self, app):
        ben = _not_let_in()
        draft = _session(app, ben, heartbeat=timedelta(minutes=2))
        client = _client(app, ben)
        _refused(client.post(
            f"/api/drafts/streaming/{draft.session_id}/claim", json={}))
        _refused(client.post(
            f"/api/drafts/streaming/{draft.session_id}/transcribe-remaining"))
        _refused(client.delete(
            f"/api/drafts/streaming/{draft.session_id}/discard"))

    def test_a_recording_older_than_the_limit(self, app):
        from backend.utils.subscriptions import RECORDING_IN_PROGRESS_MAX_AGE
        ben = _not_let_in()
        draft = _session(
            app, ben, age=RECORDING_IN_PROGRESS_MAX_AGE + timedelta(minutes=1))
        client = _client(app, ben)
        _refused(_chunk(client, draft.session_id))
        _refused(client.post(
            f"/api/drafts/streaming/{draft.session_id}/finalize",
            json={"total_chunks": 6}))
        _refused(client.get(
            f"/api/drafts/streaming/{draft.session_id}/status"))

    def test_the_turn_goes_where_the_recording_started(self, app):
        # Finalize names the thread the turn joins: under the exception it
        # must be the one the session was started in.
        ben = _not_let_in()
        own = Node(user_id=ben.id, human_owner_id=ben.id, node_type="user",
                   ai_usage="chat")
        own.set_content("my thread")
        other = Node(user_id=ben.id, human_owner_id=ben.id,
                     node_type="user", ai_usage="chat")
        other.set_content("elsewhere")
        _db.session.add_all([own, other])
        _db.session.commit()
        draft = _session(app, ben)
        draft.parent_id = own.id
        _db.session.commit()
        client = _client(app, ben)
        url = f"/api/drafts/streaming/{draft.session_id}/finalize"
        _refused(client.post(url, json={"total_chunks": 6, "label": "Voice",
                                        "parent_id": other.id}))
        _refused(client.post(url, json={"total_chunks": 6, "label": "Voice",
                                        "parent_id": "not-an-id"}))
        _passed(client.post(url, json={"total_chunks": 6, "label": "Voice",
                                       "parent_id": own.id}))

    def test_a_dictation_finalize_names_no_thread(self, app):
        ben = _not_let_in()
        own = Node(user_id=ben.id, human_owner_id=ben.id, node_type="user",
                   ai_usage="chat")
        own.set_content("replying to this")
        _db.session.add(own)
        _db.session.commit()
        draft = _session(app, ben, label=None)
        draft.parent_id = own.id
        _db.session.commit()
        _passed(_client(app, ben).post(
            f"/api/drafts/streaming/{draft.session_id}/finalize",
            json={"total_chunks": 6}))

    def test_another_accounts_session(self, app):
        ben = _not_let_in()
        anna = _user("anna", subscription_status="active",
                     subscription_level="standard")
        draft = _session(app, anna)
        client = _client(app, ben)
        _refused(_chunk(client, draft.session_id))
        _refused(client.post(
            f"/api/drafts/streaming/{draft.session_id}/finalize",
            json={"total_chunks": 6}))
        _refused(client.get(
            f"/api/drafts/streaming/{draft.session_id}/status"))
        assert not _gate_lets(
            app, ben, "GET",
            f"/api/sse/drafts/{draft.session_id}/transcription-stream")
        assert NodeTranscriptChunk.query.count() == 0

    def test_no_recording_or_upload_starts_after_the_flip(self, app):
        # The control: a session started after the account stopped being
        # let in does not exist, because every start is refused.
        ben = _not_let_in()
        client = _client(app, ben)
        for label in ("Voice", None):
            body = {"ai_usage": "chat"}
            if label:
                body["label"] = label
            _refused(client.post("/api/drafts/streaming/init", json=body))
        _refused(client.post("/api/nodes/upload/init", json={
            "filename": "talk.m4a", "filesize": 10, "total_chunks": 1,
            "upload_id": "up-1", "ai_usage": "chat"}))
        _refused(client.post("/api/nodes/", data={
            "audio_file": (BytesIO(b"x"), "talk.m4a"), "ai_usage": "chat"},
            content_type="multipart/form-data"))
        assert Draft.query.count() == 0
        assert Node.query.count() == 0

    def test_the_rest_stays_closed_while_it_records(self, app):
        ben = _not_let_in()
        draft = _session(app, ben)
        own = Node(user_id=ben.id, human_owner_id=ben.id, node_type="user",
                   ai_usage="chat")
        own.set_content("my words")
        _db.session.add(own)
        _db.session.commit()
        client = _client(app, ben)
        for method, url, body in (
                ("GET", f"/api/nodes/{own.id}", None),
                ("POST", "/api/voice/", {"content": "x",
                                         "session_id": draft.session_id}),
                ("POST", "/api/drafts/", {"content": "typed"}),
                ("GET", "/api/voice/refusal/subscription_required_past_due"
                        ".mp3", None),
                ("POST", f"/api/drafts/streaming/{draft.session_id}"
                         "/save-as-node", {}),
                ("POST", f"/api/drafts/streaming/{draft.session_id}/claim",
                 {}),
                ("GET", f"/api/nodes/{own.id}/transcription-status", None)):
            _refused(client.open(url, method=method, json=body))


# ── A chunked upload ─────────────────────────────────────────────────────

def _upload(app, user, age=timedelta(minutes=10), upload_id="up-1",
            assembled=False):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                ai_usage="chat", privacy_level="private",
                transcription_status="pending",
                created_at=datetime.utcnow() - age)
    node.set_content("[Voice note – upload in progress]")
    if assembled:
        node.audio_original_url = f"/media/user/{user.id}/node/1/original.m4a"
    _db.session.add(node)
    _db.session.commit()
    chunk_dir = app._tasks["root"] / "chunks" / str(user.id) / upload_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / "metadata.json").write_text(json.dumps({
        "node_id": node.id, "filename": "talk.m4a", "filesize": 10,
        "total_chunks": 1, "uploaded_chunks": []}))
    return node


def _upload_chunk(client, node, upload_id="up-1"):
    return client.post("/api/nodes/upload/chunk", data={
        "chunk": (BytesIO(b"0123456789"), "blob"), "chunk_index": "0",
        "upload_id": upload_id, "node_id": str(node.id),
    }, content_type="multipart/form-data")


class TestAnUploadInProgress:
    def test_its_chunks_finalize_and_status_pass(self, app):
        ben = _not_let_in()
        node = _upload(app, ben)
        client = _client(app, ben)

        _passed(_upload_chunk(client, node))
        _passed(client.post("/api/nodes/upload/finalize", json={
            "upload_id": "up-1", "node_id": node.id}))
        app._tasks["transcription"].transcribe_audio.delay \
            .assert_called_once()
        _db.session.refresh(node)
        assert node.audio_original_url
        _passed(client.get(f"/api/nodes/{node.id}/transcription-status"))

    def test_an_assembled_upload_takes_no_more_chunks(self, app):
        ben = _not_let_in()
        node = _upload(app, ben, assembled=True)
        client = _client(app, ben)
        _refused(_upload_chunk(client, node))
        _refused(client.post("/api/nodes/upload/finalize", json={
            "upload_id": "up-1", "node_id": node.id}))
        # Its transcription is still reported.
        _passed(client.get(f"/api/nodes/{node.id}/transcription-status"))

    def test_the_upload_and_its_entry_must_be_the_same_one(self, app):
        # The upload folder named by upload_id must be the one whose init
        # made the entry named by node_id; an upload folder of another
        # (older) upload, or none at all, does not pass with it.
        from backend.utils.subscriptions import RECORDING_IN_PROGRESS_MAX_AGE
        ben = _not_let_in()
        node = _upload(app, ben)
        old = _upload(app, ben, upload_id="up-old",
                      age=RECORDING_IN_PROGRESS_MAX_AGE + timedelta(hours=2))
        client = _client(app, ben)
        mixed = client.post("/api/nodes/upload/chunk", data={
            "chunk": (BytesIO(b"0123456789"), "blob"), "chunk_index": "0",
            "upload_id": "up-old", "node_id": str(node.id),
        }, content_type="multipart/form-data")
        _refused(mixed)
        _refused(client.post("/api/nodes/upload/finalize", json={
            "upload_id": "up-old", "node_id": node.id}))
        _refused(client.post("/api/nodes/upload/chunk", data={
            "chunk": (BytesIO(b"0123456789"), "blob"), "chunk_index": "0",
            "upload_id": "up-new", "node_id": str(node.id),
        }, content_type="multipart/form-data"))
        _refused(client.post("/api/nodes/upload/chunk", data={
            "chunk": (BytesIO(b"0123456789"), "blob"), "chunk_index": "0",
            "upload_id": "../up-1", "node_id": str(node.id),
        }, content_type="multipart/form-data"))
        old_folder = app._tasks["root"] / "chunks" / str(ben.id) / "up-old"
        assert not list(old_folder.glob("chunk_*"))
        assert old.audio_original_url is None
        app._tasks["transcription"].transcribe_audio.delay.assert_not_called()

    def test_only_the_chunks_its_init_announced(self, app):
        ben = _not_let_in()
        node = _upload(app, ben)            # total_chunks 1
        client = _client(app, ben)
        for index in ("1", "99", "-1", "x"):
            _refused(client.post("/api/nodes/upload/chunk", data={
                "chunk": (BytesIO(b"0123456789"), "blob"),
                "chunk_index": index, "upload_id": "up-1",
                "node_id": str(node.id),
            }, content_type="multipart/form-data"))
        folder = app._tasks["root"] / "chunks" / str(ben.id) / "up-1"
        assert not list(folder.glob("chunk_*"))
        _passed(_upload_chunk(client, node))

    def test_another_accounts_or_an_old_upload(self, app):
        from backend.utils.subscriptions import RECORDING_IN_PROGRESS_MAX_AGE
        ben = _not_let_in()
        anna = _user("anna", subscription_status="active",
                     subscription_level="standard")
        theirs = _upload(app, anna)
        old = _upload(app, ben, upload_id="up-2",
                      age=RECORDING_IN_PROGRESS_MAX_AGE + timedelta(minutes=1))
        client = _client(app, ben)
        _refused(_upload_chunk(client, theirs))
        _refused(client.post("/api/nodes/upload/finalize", json={
            "upload_id": "up-1", "node_id": theirs.id}))
        _refused(client.get(f"/api/nodes/{theirs.id}/transcription-status"))
        _refused(_upload_chunk(client, old, upload_id="up-2"))
        _refused(client.get(f"/api/nodes/{old.id}/transcription-status"))
        app._tasks["transcription"].transcribe_audio.delay.assert_not_called()


# ── Accounts the gate does not stop (controls) ──────────────────────────

@pytest.mark.parametrize("kw", [
    {"subscription_status": "active", "subscription_level": "standard"},
    {"subscription_status": "ended", "subscription_level": "standard",
     "subscription_ever_paid": True},
])
def test_other_accounts_record_as_before(app, kw):
    u = _user("someone", **kw)
    draft = _session(app, u, heartbeat=timedelta(minutes=2))
    # Not live, but these accounts are let in: the route decides.
    body = _passed(_chunk(_client(app, u), draft.session_id))
    assert body.get("error") is None


def test_payments_off_nobody_is_stopped(app):
    app.config["PAYMENTS_ENABLED"] = False
    try:
        ben = _user("ben", subscription_status="past_due",
                    subscription_level="standard")
        draft = _session(app, ben, heartbeat=timedelta(minutes=2))
        _passed(_chunk(_client(app, ben), draft.session_id))
    finally:
        app.config["PAYMENTS_ENABLED"] = True


# ── The download of the account's data ──────────────────────────────────

def _forbid_model_calls(monkeypatch):
    """A provider client made during the export fails the test."""
    import anthropic
    import openai

    def _no(*a, **k):
        raise AssertionError("the export made a provider client")
    monkeypatch.setattr(openai, "OpenAI", _no)
    monkeypatch.setattr(anthropic, "Anthropic", _no)


def _entry(user, text="my words"):
    root = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                ai_usage="none", privacy_level="private")
    root.set_content(text)
    _db.session.add(root)
    _db.session.commit()
    return root


class TestTheExport:
    def test_an_account_not_let_in_downloads_its_data(self, app,
                                                      monkeypatch):
        _forbid_model_calls(monkeypatch)
        ben = _not_let_in()
        _entry(ben, "what I wrote before the trial ended")
        costs = APICostLog.query.count()
        client = _client(app, ben)

        head = client.head("/api/export/threads")
        assert head.status_code == 200, head.status_code
        resp = client.get("/api/export/threads")

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert "attachment" in resp.headers["Content-Disposition"]
        text = resp.get_data(as_text=True)
        assert "what I wrote before the trial ended" in text
        assert text.rstrip().endswith("*End of Export*")
        assert APICostLog.query.count() == costs

    def test_only_the_download_opens(self, app):
        # The rest of the app stays closed to it (the route walk in
        # test_payments_gate covers every route).
        from backend.utils.subscriptions import open_without_subscription
        assert open_without_subscription("GET", "/api/export/threads")
        assert open_without_subscription("HEAD", "/api/export/threads")
        assert not open_without_subscription("POST", "/api/export/threads")
        assert not open_without_subscription(
            "GET", "/api/export/profile-progress")
        ben = _not_let_in()
        own = _entry(ben)
        client = _client(app, ben)
        _refused(client.get(f"/api/nodes/{own.id}"))
        _refused(client.get("/api/export/profile-progress"))

    def test_payments_off_the_export_is_as_before(self, app):
        app.config["PAYMENTS_ENABLED"] = False
        try:
            u = _user("free-one")
            _entry(u)
            resp = _client(app, u).get("/api/export/threads")
            assert resp.status_code == 200
            assert "my words" in resp.get_data(as_text=True)
        finally:
            app.config["PAYMENTS_ENABLED"] = True


# ── The download's "Drafts" part (decided 2026-11-13) ────────────────────
# A dictation finished while the account is not let in stays a draft;
# the download is the only way such a person reaches it. So the download
# ends with every draft of the account that holds text, for every
# account. Never in {user_export}, which goes to a model.

def _draft(user, text, **kw):
    d = Draft(user_id=user.id, privacy_level="private", ai_usage="none",
              **kw)
    d.set_content(text)
    _db.session.add(d)
    _db.session.commit()
    return d


class TestTheDraftsPart:
    def test_the_dictation_and_typed_drafts_are_in_it(self, app,
                                                      monkeypatch):
        _forbid_model_calls(monkeypatch)
        ben = _not_let_in()
        entry = _entry(ben, "an entry")
        _draft(ben, "the dictated words after the trial ended",
               session_id=str(uuid.uuid4()), streaming_status="completed")
        _draft(ben, "a reply I was typing", parent_id=entry.id)
        _draft(ben, "a Voice turn whose reply never started",
               session_id=str(uuid.uuid4()), label="Voice",
               streaming_status="completed")
        # Left out: an empty one, a recording already saved as an entry,
        # and a change the AI proposed (not the user's writing).
        _draft(ben, "   ")
        _draft(ben, "saved already", session_id=str(uuid.uuid4()),
               label="Voice", streaming_status="completed",
               streaming_warning="Your recording is saved.")
        _draft(ben, '{"a": "proposal"}', parent_id=entry.id,
               label="tool_pending")
        costs = APICostLog.query.count()

        text = _client(app, ben).get("/api/export/threads").get_data(
            as_text=True)

        threads, drafts = text.split("# Drafts\n", 1)
        assert "an entry" in threads
        assert "## Draft 1\n" in drafts and "## Draft 3\n" in drafts
        assert "## Draft 4" not in drafts
        assert "the dictated words after the trial ended" in drafts
        assert "Kind:** a dictation's transcript, for a new entry" in drafts
        assert "a reply I was typing" in drafts
        assert "Kind:** typed, for a reply in a thread" in drafts
        assert "a Voice turn whose reply never started" in drafts
        assert "saved already" not in text and "proposal" not in text
        assert text.rstrip().endswith("*End of Export*")
        assert APICostLog.query.count() == costs

    def test_drafts_without_any_thread(self, app):
        from backend.routes.export_data import NO_THREADS_TEXT
        ben = _not_let_in()
        _draft(ben, "only a dictation", session_id=str(uuid.uuid4()),
               streaming_status="completed")
        text = _client(app, ben).get("/api/export/threads").get_data(
            as_text=True)
        assert text.startswith(NO_THREADS_TEXT)
        assert "# Drafts" in text and "only a dictation" in text

    def test_without_drafts_the_download_is_as_before(self, app):
        u = _user("plain", subscription_status="active",
                  subscription_level="standard")
        _entry(u)
        text = _client(app, u).get("/api/export/threads").get_data(
            as_text=True)
        assert "# Drafts" not in text
        assert text.rstrip().endswith("*End of Export*")

    def test_another_accounts_drafts_are_not_in_it(self, app):
        ben = _not_let_in()
        _entry(ben)
        anna = _user("anna", subscription_status="active",
                     subscription_level="standard")
        _draft(anna, "anna's draft")
        text = _client(app, ben).get("/api/export/threads").get_data(
            as_text=True)
        assert "anna's draft" not in text and "# Drafts" not in text

    def test_drafts_never_go_to_a_model(self, app):
        # {user_export} (the archive a reply may quote) has no drafts.
        from backend.routes.export_data import build_user_export_content
        u = _user("plain", subscription_status="active",
                  subscription_level="standard")
        _entry(u)
        _draft(u, "private draft text")
        content = build_user_export_content(u, filter_ai_usage=False)
        assert content and "private draft text" not in content
        assert "# Drafts" not in content
