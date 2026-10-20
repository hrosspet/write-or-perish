"""Background ChatGPT/Claude imports (2026-10-20: a 43k-message import
ran an hour inside the confirm request and was pressed five times).

Covers:
- analyze stashes the analyzed conversations on the server, encrypted,
  and returns a token; confirm takes the token and starts the job;
- the job (run_conversation_import) commits in chunks, saves its place,
  and a run after a crash continues there without duplicates;
- one running conversation import per user: the same import's second
  confirm gets the running job's id, a different one is refused;
- one KMS data key per committed chunk, not per node, in the envelope
  the normal decrypt path reads;
- stash expiry (TTL sweep, expired token, deleted on success);
- the profile hand-off runs once, at the end;
- the Claude path.
"""
import io
import json
import os
import time

import pytest

from backend.tests.test_chatgpt_import import (  # noqa: F401
    app, _db, _login, _make_user, _conv, _simple_mapping, _stashed,
    _JobResponse, _analyzed_conv)
from backend.models import Node, User
from backend.utils import conversation_stash as cs
from backend.utils import encryption


# ── helpers ──────────────────────────────────────────────────────────────

def _analyze(client, raw, path="/api/import/chatgpt/analyze",
             name="conversations.json"):
    return client.post(path, data={
        "conversations_file": (io.BytesIO(json.dumps(raw).encode()), name),
    }, content_type="multipart/form-data")


def _confirm(client, token, source="chatgpt", **extra):
    from flask import current_app
    results = current_app.import_results
    before = len(results)
    resp = client.post(f"/api/import/{source}/confirm",
                       json=dict({"import_token": token}, **extra))
    return resp, (results[-1] if len(results) > before else None)


def _user_and_client(app, name="alice"):
    client = app.test_client()
    user = _make_user(name)
    _db.session.commit()
    _login(client, user.id)
    return user, client


def _chat(n_messages, conv_id, start_ts=1700000000, title=None):
    """A raw ChatGPT conversation of *n_messages* alternating turns."""
    mapping = {"root": {"parent": None, "message": None}}
    parent = "root"
    for i in range(n_messages):
        node_id = f"{conv_id}-m{i}"
        mapping[node_id] = {
            "parent": parent,
            "message": {
                "author": {"role": "user" if i % 2 == 0 else "assistant"},
                "content": {"parts": [f"message {i} of {conv_id}"]},
                "create_time": start_ts + i,
                "metadata": {"model_slug": "gpt-4o"} if i % 2 else {},
            },
        }
        parent = node_id
    return {"id": conv_id, "title": title or conv_id,
            "create_time": start_ts, "current_node": parent,
            "mapping": mapping}


def _analyzed(n_convs, per_conv, prefix="c"):
    """Confirm-shaped conversations (what analyze stashes)."""
    convs = []
    for c in range(n_convs):
        messages = [{
            "text": f"{prefix}{c} message {i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "created_at": f"2024-01-{1 + c % 28:02d}T10:00:{i % 60:02d}",
            "model": "gpt-4o" if i % 2 else "",
            "mapping_id": f"{prefix}{c}-m{i}",
        } for i in range(per_conv)]
        convs.append({"name": f"{prefix}{c}",
                      "created_at": f"2024-01-{1 + c % 28:02d}T10:00:00",
                      "messages": messages,
                      "message_count": per_conv})
    return convs


def _stash(user_id, convs, source="chatgpt"):
    from backend.routes.import_data import _stash_conversations
    return _stash_conversations(user_id, source, convs,
                                sum(len(c["messages"]) for c in convs))


OPTIONS = {"privacy_level": "private", "ai_usage": "none", "on_deleted": None}


def _keys(user_id):
    return [k for (k,) in _db.session.query(Node.source_key).filter(
        Node.human_owner_id == user_id)]


@pytest.fixture
def kms(monkeypatch):
    """Encryption on, KMS replaced by a reversible fake that counts calls."""
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    calls = {"encrypt": 0, "decrypt": 0}

    class _Resp:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def fake_kms_call(op, request):
        calls[op] += 1
        if op == "encrypt":
            return _Resp(ciphertext=b"wrapped:" + request["plaintext"])
        return _Resp(plaintext=request["ciphertext"][len(b"wrapped:"):])

    monkeypatch.setattr(encryption, "_kms_call", fake_kms_call)
    encryption._dek_cache.clear()
    encryption._dek_cache_order.clear()
    assert encryption.is_encryption_enabled()
    yield calls
    encryption._dek_cache.clear()
    encryption._dek_cache_order.clear()


# ── token → job → nodes ──────────────────────────────────────────────────

def test_analyze_stashes_and_confirm_runs_the_job(app):
    alice, client = _user_and_client(app)
    resp = _analyze(client, [_chat(4, "a"), _chat(2, "b", start_ts=1600000000)])
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert set(body) == {"import_token", "total_conversations",
                         "total_messages", "total_tokens", "total_size",
                         "notices"}
    assert body["total_messages"] == 6
    # Stashed oldest first: the order the import creates threads in.
    assert [c["name"] for c in _stashed(body)] == ["b", "a"]

    resp, result = _confirm(client, body["import_token"])
    assert resp.status_code == 202, resp.get_json()
    assert resp.get_json()["total"] == 6 and resp.get_json()["task_id"]
    assert result["created"] == 6 and result["thread_count"] == 2
    assert result["skipped"] == 0 and result["total"] == 6
    nodes = Node.query.filter_by(human_owner_id=alice.id).all()
    assert len(nodes) == 6
    assert {n.origin for n in nodes} == {"chatgpt"}
    assert all(n.provenance == "archive_upload" for n in nodes)
    # The chain of conversation "a": root, then each reply under the last.
    chain = sorted((n for n in nodes if n.source_key.startswith("chatgpt:a-")),
                   key=lambda n: n.created_at)
    assert chain[0].parent_id is None
    assert [n.parent_id for n in chain[1:]] == [n.id for n in chain[:-1]]
    assert chain[0].get_content().startswith("# a\n\n")
    assert chain[1].llm_model == "gpt-4o" and chain[1].node_type == "llm"
    # Finished: the stash and the lock are gone.
    assert not cs.stash_path(alice.id, body["import_token"]).exists()
    assert cs.read_lock(alice.id) is None


def test_confirm_needs_a_live_token_of_the_same_importer(app):
    alice, client = _user_and_client(app)
    bob = _make_user("bob")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(1, 2))
    bobs = _stash(bob.id, _analyzed(1, 2))
    for bad in ["../x", bobs, "x" * 40]:
        resp, _ = _confirm(client, bad)
        assert resp.status_code == 400
        assert "expired" in resp.get_json()["error"]
    # A ChatGPT token on the Claude confirm.
    resp, _ = _confirm(client, token, source="claude")
    assert resp.status_code == 400
    assert Node.query.count() == 0


def test_a_client_from_before_the_change_is_told_to_reload(app):
    _alice, client = _user_and_client(app)
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"conversations": [_analyzed_conv()]})
    assert resp.status_code == 400
    assert "latest version" in resp.get_json()["error"]


def test_claude_path_end_to_end(app):
    alice, client = _user_and_client(app)
    raw = [{
        "name": "Claude chat", "created_at": "2024-03-01T10:00:00Z",
        "chat_messages": [
            {"text": "question", "sender": "human",
             "created_at": "2024-03-01T10:00:00Z", "uuid": "q1"},
            {"text": "  ", "sender": "assistant",
             "created_at": "2024-03-01T10:00:01Z", "uuid": "blank"},
            {"text": "answer", "sender": "assistant",
             "created_at": "2024-03-01T10:00:02Z", "uuid": "a1"},
        ],
    }]
    body = _analyze(client, raw, path="/api/import/claude/analyze").get_json()
    assert "conversations" not in body and body["total_messages"] == 2
    resp, result = _confirm(client, body["import_token"], source="claude")
    assert resp.status_code == 202
    assert result["created"] == 2 and result["thread_count"] == 1
    q = Node.query.filter_by(source_key="claude:q1").one()
    a = Node.query.filter_by(source_key="claude:a1").one()
    assert q.get_content() == "# Claude chat\n\nquestion"
    assert a.parent_id == q.id and a.llm_model == "claude-web"
    assert User.query.get(a.user_id).username == "claude-web"
    assert a.created_at.isoformat() == "2024-03-01T10:00:02"  # Z stripped
    # Re-import: everything skipped.
    body = _analyze(client, raw, path="/api/import/claude/analyze").get_json()
    _resp, again = _confirm(client, body["import_token"], source="claude")
    assert again["created"] == 0 and again["skipped"] == 2
    assert Node.query.filter_by(human_owner_id=alice.id).count() == 2


# ── chunked commits and resume ───────────────────────────────────────────

class _Crash(Exception):
    pass


def _crash_after(monkeypatch, n_nodes):
    """Make node creation raise after *n_nodes* nodes, like a worker
    dying mid-job."""
    from backend.routes import import_data
    real = import_data._add_imported_message_nodes
    made = {"n": 0}

    def flaky(*args, **kwargs):
        if made["n"] >= n_nodes:
            raise _Crash()
        made["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(import_data, "_add_imported_message_nodes", flaky)
    return lambda: monkeypatch.setattr(
        import_data, "_add_imported_message_nodes", real)


def test_chunked_commits_and_a_resume_after_a_crash_make_no_duplicates(
        app, monkeypatch):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    convs = _analyzed(10, 4)  # 40 messages
    token = _stash(alice.id, convs)
    restore = _crash_after(monkeypatch, 22)
    assert cs.acquire_lock(alice.id, "t1", token, "chatgpt")[0]
    with pytest.raises(_Crash):
        run_conversation_import(alice.id, token, "t1", OPTIONS, batch_size=8)

    # Commits fall on conversation boundaries, one per 8 nodes (two
    # conversations): the crash in the 6th conversation rolls back the
    # 5th and 6th and keeps the first four.
    assert len(_keys(alice.id)) == 16
    state = cs.read_state(alice.id, token)
    assert state["next_conv"] == 4 and state["created"] == 16
    assert state["done"] == 16 and state["thread_count"] == 4
    # A failed run frees the user's lock and keeps the stash.
    assert cs.read_lock(alice.id) is None
    assert cs.stash_path(alice.id, token).exists()

    # The user confirms again: a new run continues from the saved place.
    restore()
    _db.session.rollback()
    result = run_conversation_import(alice.id, token, "t2", OPTIONS,
                                     batch_size=8)
    keys = _keys(alice.id)
    assert len(keys) == 40 and len(set(keys)) == 40
    assert result["created"] == 40 and result["skipped"] == 0
    assert result["thread_count"] == 10


def test_a_commit_the_saved_state_missed_is_skipped_not_duplicated(
        app, monkeypatch):
    """A crash between a commit and the state write: the next run redoes
    that chunk from the older state, and dedup skips its nodes."""
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(4, 3))
    real_write = cs.write_state

    def die_after_the_commit(*args):
        raise _Crash()

    monkeypatch.setattr(cs, "write_state", die_after_the_commit)
    with pytest.raises(_Crash):
        run_conversation_import(alice.id, token, "t1", OPTIONS, batch_size=6)
    assert len(_keys(alice.id)) == 6  # the first chunk was committed
    assert cs.read_state(alice.id, token) is None

    monkeypatch.setattr(cs, "write_state", real_write)
    result = run_conversation_import(alice.id, token, "t2", OPTIONS,
                                     batch_size=6)
    keys = _keys(alice.id)
    assert len(keys) == 12 and len(set(keys)) == 12
    # The redone chunk counts as skipped: off by that chunk, never doubled.
    assert result["created"] == 6 and result["skipped"] == 6


def test_a_resumable_stop_keeps_the_lock_and_the_same_task_continues(
        app, monkeypatch):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(6, 2))
    restore = _crash_after(monkeypatch, 5)
    with pytest.raises(_Crash):
        run_conversation_import(alice.id, token, "t1", OPTIONS,
                                batch_size=2, resumable=(_Crash,))
    assert cs.read_lock(alice.id)["task_id"] == "t1"  # the time limit retry
    restore()
    _db.session.rollback()
    result = run_conversation_import(alice.id, token, "t1", OPTIONS,
                                     batch_size=2)
    assert result["created"] == 12
    assert len(set(_keys(alice.id))) == 12 == len(_keys(alice.id))
    assert cs.read_lock(alice.id) is None


def test_a_repeated_run_of_a_finished_job_returns_its_result(app):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(2, 2))
    first = run_conversation_import(alice.id, token, "t1", OPTIONS)
    again = run_conversation_import(alice.id, token, "t1", OPTIONS)
    assert again == first and again["created"] == 4
    assert len(_keys(alice.id)) == 4


def test_a_run_with_other_settings_starts_over_and_updates(app, monkeypatch):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(4, 2))
    restore = _crash_after(monkeypatch, 4)
    with pytest.raises(_Crash):
        run_conversation_import(alice.id, token, "t1", OPTIONS, batch_size=2)
    restore()
    _db.session.rollback()
    public = dict(OPTIONS, privacy_level="public")
    result = run_conversation_import(alice.id, token, "t2", public)
    assert result["created"] == 4 and result["updated"] == 4
    assert all(n.privacy_level == "public"
               for n in Node.query.filter_by(human_owner_id=alice.id))


def test_a_superseded_run_does_nothing(app):
    from backend.routes.import_data import run_conversation_import
    from backend.utils.task_owner import UserFacingTaskError
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(1, 2))
    assert cs.acquire_lock(alice.id, "newer", token, "chatgpt")[0]
    with pytest.raises(UserFacingTaskError):
        run_conversation_import(alice.id, token, "older", OPTIONS)
    assert Node.query.count() == 0
    assert cs.read_lock(alice.id)["task_id"] == "newer"


def test_progress_reports_messages(app):
    from backend.routes.import_data import create_conversation_nodes
    alice = _make_user("alice")
    _db.session.commit()
    convs = _analyzed(3, 4)
    seen = []
    state = create_conversation_nodes(
        alice.id, "chatgpt", enumerate(convs), "private", "none", None,
        batch_size=4, on_progress=seen.append, progress_interval=0)
    assert seen == [4, 8, 12, 12]
    assert state["done"] == 12 and state["next_conv"] == 3


# ── one running import per user ──────────────────────────────────────────

def _queue_only(monkeypatch):
    """Confirm queues the job but nothing runs it."""
    from backend.routes import import_data
    queued = []
    monkeypatch.setattr(import_data, "_dispatch_conversation_import",
                        lambda *args: queued.append(args))
    return queued


def test_a_second_confirm_returns_the_running_job(app, monkeypatch):
    alice, client = _user_and_client(app)
    queued = _queue_only(monkeypatch)
    token = _stash(alice.id, _analyzed(2, 2))
    first = client.post("/api/import/chatgpt/confirm",
                        json={"import_token": token})
    assert first.status_code == 202
    second = client.post("/api/import/chatgpt/confirm",
                         json={"import_token": token})
    assert second.status_code == 202
    assert second.get_json()["task_id"] == first.get_json()["task_id"]
    assert second.get_json()["already_running"] is True
    assert len(queued) == 1

    # Another export while this one runs: refused, with the running id.
    other = _stash(alice.id, _analyzed(1, 2, prefix="o"))
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"import_token": other})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "import_running"
    assert resp.get_json()["task_id"] == first.get_json()["task_id"]
    assert "still running" in resp.get_json()["error"]
    assert len(queued) == 1


def test_a_stale_lock_does_not_block_a_new_confirm(app, monkeypatch):
    alice, client = _user_and_client(app)
    queued = _queue_only(monkeypatch)
    token = _stash(alice.id, _analyzed(1, 2))
    assert cs.acquire_lock(alice.id, "dead-task", token, "chatgpt")[0]
    old = time.time() - cs.LOCK_STALE_SECONDS - 5
    os.utime(cs._lock_path(alice.id), (old, old))
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"import_token": token})
    assert resp.status_code == 202
    assert resp.get_json()["task_id"] != "dead-task"
    assert cs.read_lock(alice.id)["task_id"] == resp.get_json()["task_id"]
    assert len(queued) == 1


def test_a_failed_dispatch_frees_the_lock(app, monkeypatch):
    from backend.routes import import_data
    alice, client = _user_and_client(app)

    def broken(*args):
        raise ConnectionError("broker down")

    monkeypatch.setattr(import_data, "_dispatch_conversation_import", broken)
    token = _stash(alice.id, _analyzed(1, 2))
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"import_token": token})
    assert resp.status_code == 500
    assert "broker" not in resp.get_data(as_text=True)
    assert cs.read_lock(alice.id) is None


def test_the_deleted_content_prompt_still_comes_first(app, monkeypatch):
    from datetime import datetime
    alice, client = _user_and_client(app)
    n = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
             source_key="chatgpt:c0-m0", deleted_at=datetime.utcnow())
    n.set_content("old")
    _db.session.add(n)
    _db.session.commit()
    queued = _queue_only(monkeypatch)
    token = _stash(alice.id, _analyzed(1, 2))
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"import_token": token})
    assert resp.status_code == 409
    assert resp.get_json() == {"error": "deleted_content_matches",
                               "deleted_matches": 1}
    assert queued == [] and cs.read_lock(alice.id) is None
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"import_token": token, "on_deleted": "skip"})
    assert resp.status_code == 202
    assert queued[0][3]["on_deleted"] == "skip"


# ── one data key per chunk ───────────────────────────────────────────────

def test_kms_calls_do_not_scale_with_nodes(app, kms, monkeypatch):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    convs = _analyzed(30, 40)  # 1200 messages
    token = _stash(alice.id, convs)
    assert kms == {"encrypt": 1, "decrypt": 0}  # the stash's data key

    # A worker process: nothing cached.
    encryption._dek_cache.clear()
    encryption._dek_cache_order.clear()
    result = run_conversation_import(alice.id, token, "t1", OPTIONS)
    assert result["created"] == 1200
    # One wrap per committed chunk of >= 500 nodes (3 here), one unwrap
    # for the stash; it was one wrap per node before.
    assert kms["encrypt"] - 1 <= 3
    assert kms["decrypt"] == 1


def test_a_node_written_with_the_shared_key_decrypts_normally(app, kms):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(2, 3))
    run_conversation_import(alice.id, token, "t1", OPTIONS)
    nodes = Node.query.filter_by(human_owner_id=alice.id).all()
    raws = [_db.session.execute(_db.text(
        "SELECT content FROM node WHERE id = :id"), {"id": n.id}).scalar()
        for n in nodes]
    assert all(r.startswith(encryption.ENCRYPTED_PREFIX_V2) for r in raws)
    assert len({encryption.wrapped_dek_of(r) for r in raws}) == 1
    assert not any("message" in r for r in raws)
    # Another process (empty cache) reads them through the normal path.
    encryption._dek_cache.clear()
    encryption._dek_cache_order.clear()
    before = kms["decrypt"]
    texts = sorted(encryption.decrypt_content(r) for r in raws)
    assert "c0 message 1" in texts and "# c1\n\nc1 message 0" in texts
    assert kms["decrypt"] == before + 1  # one unwrap for all of them


def test_set_content_with_a_shared_key_matches_the_normal_envelope(kms):
    dek = encryption.new_dek()
    n = Node(privacy_level="private")
    n.set_content("hello", dek=dek)
    encryption._dek_cache.clear()
    encryption._dek_cache_order.clear()
    assert n.content.startswith(encryption.ENCRYPTED_PREFIX_V2)
    assert encryption.decrypt_content(n.content) == "hello"
    public = Node(privacy_level="public")
    public.set_content("open", dek=dek)
    assert public.content == "open"
    n.set_privacy_level("public", dek=dek)
    assert n.content == "hello"
    n.set_privacy_level("private", dek=dek)
    assert encryption.wrapped_dek_of(n.content) == \
        encryption.wrapped_dek_of(encryption.encrypt_with_dek("x", dek))


def test_a_public_import_makes_no_data_key(app, kms):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(3, 3))
    wraps = kms["encrypt"]
    run_conversation_import(alice.id, token, "t1",
                            dict(OPTIONS, privacy_level="public"))
    assert kms["encrypt"] == wraps
    assert {n.content for n in Node.query.filter_by(
        human_owner_id=alice.id)} >= {"c0 message 1"}


# ── the stash ────────────────────────────────────────────────────────────

def test_the_stash_is_encrypted_at_rest(app, kms):
    alice, client = _user_and_client(app)
    body = _analyze(client, [_chat(3, "secret-chat")]).get_json()
    path = cs.stash_path(alice.id, body["import_token"])
    raw = path.read_text()
    assert "message 0 of secret-chat" not in raw
    assert "secret-chat" not in raw
    header = json.loads(raw.split("\n", 1)[0])
    assert header == {"format": 1, "source": "chatgpt", "conversations": 1,
                      "messages": 3}
    for line in raw.strip().split("\n")[1:]:
        assert line.startswith(encryption.ENCRYPTED_PREFIX_V2)
    assert cs.read_keys(path) == [f"chatgpt:secret-chat-m{i}"
                                  for i in range(3)]
    assert _stashed(body)[0]["name"] == "secret-chat"


def test_stash_expiry(app, monkeypatch):
    alice, client = _user_and_client(app)
    _queue_only(monkeypatch)
    token = _stash(alice.id, _analyzed(1, 2))
    path = cs.stash_path(alice.id, token)
    old = time.time() - cs.STASH_TTL_SECONDS - 60
    os.utime(path, (old, old))
    # An expired stash is refused before the sweep gets to it...
    resp = client.post("/api/import/chatgpt/confirm",
                       json={"import_token": token})
    assert resp.status_code == 400 and "expired" in resp.get_json()["error"]
    # ...and the sweep removes it, while a fresh one stays.
    fresh = _stash(alice.id, _analyzed(1, 2, prefix="f"))
    assert not path.exists()  # writing a stash sweeps too
    assert cs.stash_path(alice.id, fresh).exists()
    assert cs.sweep_expired() == 0
    later = time.time() + cs.STASH_TTL_SECONDS + 60
    assert cs.sweep_expired(now=later) >= 1
    assert not cs.stash_path(alice.id, fresh).exists()


def test_a_running_job_keeps_its_stash_fresh(app):
    from backend.routes.import_data import run_conversation_import
    alice = _make_user("alice")
    _db.session.commit()
    token = _stash(alice.id, _analyzed(1, 2))
    path = cs.stash_path(alice.id, token)
    old = time.time() - cs.STASH_TTL_SECONDS + 30  # nearly expired
    os.utime(path, (old, old))
    seen = []
    run_conversation_import(
        alice.id, token, "t1", OPTIONS,
        on_progress=lambda done, total: seen.append(cs.is_fresh(path)))
    assert seen and all(seen)


# ── the profile hand-off ─────────────────────────────────────────────────

def test_the_profile_hand_off_runs_once_at_the_end(app, monkeypatch):
    from backend.routes import import_data
    from datetime import datetime
    alice = _make_user("alice")
    _db.session.commit()
    calls = []
    monkeypatch.setattr(
        import_data, "_maybe_update_profile_after_import",
        lambda uid, ts, tokens: calls.append((uid, ts, tokens)) or "p-1")
    convs = _analyzed(4, 2)
    convs[2]["messages"][0]["created_at"] = "2020-05-05T05:05:05"
    expected_tokens = sum(len(m["text"]) // 4 for c in convs
                          for m in c["messages"])
    token = _stash(alice.id, convs)
    chat = dict(OPTIONS, ai_usage="chat")
    restore = _crash_after(monkeypatch, 4)
    with pytest.raises(_Crash):
        import_data.run_conversation_import(alice.id, token, "t1", chat,
                                            batch_size=2)
    assert calls == []  # not before the job finishes
    restore()
    _db.session.rollback()
    result = import_data.run_conversation_import(alice.id, token, "t2", chat,
                                                 batch_size=2)
    assert calls == [(alice.id, datetime(2020, 5, 5, 5, 5, 5),
                      expected_tokens)]
    assert result["profile_update_task_id"] == "p-1"

    # AI usage "none": no hand-off (as before).
    calls.clear()
    token = _stash(alice.id, _analyzed(1, 2, prefix="n"))
    import_data.run_conversation_import(alice.id, token, "t3", OPTIONS)
    assert calls == []
