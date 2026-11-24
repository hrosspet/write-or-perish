"""One account's replies waiting or being written at once
(utils/llm_nodes.MAX_REPLIES_IN_PROGRESS).

create_llm_placeholder, which every reply path passes through, counts the
account's replies that are waiting or being written under the account's
replies lock and refuses a new one past the bound: 429
``too_many_replies`` where the request is only the reply, and the entry
kept with ``llm_error`` / ``llm_error_code`` where the request also wrote
one. Runs create_app() on sqlite in memory; the reply job is stubbed, so
nothing reaches a broker or a model.
"""
import json
from datetime import datetime, timedelta

import pytest

from backend.tests.test_sign_in_links import (  # noqa: F401
    real_app, _db, User)


@pytest.fixture
def queued(monkeypatch):
    """The reply jobs queued, in order (the broker stubbed). Patched on the
    module create_llm_placeholder imports from (sys.modules), which other
    test modules may have replaced."""
    import importlib
    import itertools
    import types
    lc = importlib.import_module("backend.tasks.llm_completion")
    jobs = []
    ids = itertools.count()

    class _ReplyTask:
        def delay(self, *a, **k):
            jobs.append(a)
            return types.SimpleNamespace(id=f"t{next(ids)}")
        apply_async = delay

    monkeypatch.setattr(lc, "generate_llm_response", _ReplyTask())
    return jobs


def _signed_in(app, username):
    user = User.query.filter_by(username=username).first()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client, user.id


def _entry(user_id, parent_id=None, text="hello"):
    from backend.models import Node
    node = Node(user_id=user_id, human_owner_id=user_id, node_type="user",
                privacy_level="private", ai_usage="chat",
                parent_id=parent_id)
    node.set_content(text)
    _db.session.add(node)
    _db.session.commit()
    return node.id


def _chat_model(app):
    from backend.utils.llm_nodes import is_chat_model
    return next(m for m in app.config["SUPPORTED_MODELS"]
                if is_chat_model(m))


def _ask(client, node_id, model):
    from flask import g
    g.pop("_login_user", None)
    return client.post(f"/api/nodes/{node_id}/llm", json={"model": model})


def _llm_rows(user_id):
    from backend.models import Node
    _db.session.expire_all()
    return Node.query.filter_by(human_owner_id=user_id,
                                node_type="llm").all()


def test_replies_past_the_bound_are_refused_on_any_thread(real_app, queued):  # noqa: F811
    from backend.utils.llm_nodes import (
        MAX_REPLIES_IN_PROGRESS, TOO_MANY_REPLIES_MESSAGE)
    client, uid = _signed_in(real_app, "alice")
    model = _chat_model(real_app)
    # One entry per thread: the bound is the account's, not a thread's.
    threads = [_entry(uid, text=f"thread {i}") for i in range(5)]

    asked = MAX_REPLIES_IN_PROGRESS + 5
    answers = [_ask(client, threads[i % len(threads)], model)
               for i in range(asked)]

    codes = [r.status_code for r in answers]
    assert codes == [202] * MAX_REPLIES_IN_PROGRESS + [429] * 5
    assert answers[-1].get_json() == {"error": TOO_MANY_REPLIES_MESSAGE,
                                      "code": "too_many_replies"}
    # Nothing is written or queued for a refused request.
    assert len(queued) == MAX_REPLIES_IN_PROGRESS
    assert len(_llm_rows(uid)) == MAX_REPLIES_IN_PROGRESS


def test_a_finished_reply_makes_room_for_the_next(real_app, queued):  # noqa: F811
    from backend.utils.llm_nodes import MAX_REPLIES_IN_PROGRESS
    client, uid = _signed_in(real_app, "alice")
    model = _chat_model(real_app)
    entry = _entry(uid)
    for _ in range(MAX_REPLIES_IN_PROGRESS):
        assert _ask(client, entry, model).status_code == 202
    assert _ask(client, entry, model).status_code == 429

    first = _llm_rows(uid)[0]
    first.llm_task_status = "completed"
    _db.session.commit()
    assert _ask(client, entry, model).status_code == 202
    assert _ask(client, entry, model).status_code == 429

    failed = _llm_rows(uid)[1]
    failed.llm_task_status = "failed"
    _db.session.commit()
    assert _ask(client, entry, model).status_code == 202


def test_old_waiting_replies_and_batch_reads_are_not_counted(
        real_app, queued):  # noqa: F811
    from backend.utils.glean import READ_BATCH_MARKER
    from backend.utils.llm_nodes import (
        MAX_REPLIES_IN_PROGRESS, REPLY_COUNTS_FOR_SECONDS)
    client, uid = _signed_in(real_app, "alice")
    model = _chat_model(real_app)
    entry = _entry(uid)
    for _ in range(MAX_REPLIES_IN_PROGRESS):
        assert _ask(client, entry, model).status_code == 202
    rows = _llm_rows(uid)

    # A reply asked for longer ago than the task's time limit, still
    # marked waiting (its worker stopped), no longer counts.
    rows[0].created_at = datetime.utcnow() - timedelta(
        seconds=REPLY_COUNTS_FOR_SECONDS + 60)
    # Nor does a read sent through the Batch API (the admin's
    # experiments), which waits for the provider for hours.
    rows[1].tool_calls_meta = json.dumps([{"name": READ_BATCH_MARKER},
                                          {"name": "_batch"}])
    rows[1].llm_task_status = "processing"
    _db.session.commit()

    assert _ask(client, entry, model).status_code == 202
    assert _ask(client, entry, model).status_code == 202
    assert _ask(client, entry, model).status_code == 429


def test_another_account_is_not_held_by_the_bound(real_app, queued):  # noqa: F811
    from backend.utils.llm_nodes import MAX_REPLIES_IN_PROGRESS
    alice, alice_id = _signed_in(real_app, "alice")
    model = _chat_model(real_app)
    entry = _entry(alice_id)
    for _ in range(MAX_REPLIES_IN_PROGRESS):
        assert _ask(alice, entry, model).status_code == 202
    assert _ask(alice, entry, model).status_code == 429

    root, root_id = _signed_in(real_app, "root")
    assert _ask(root, _entry(root_id), model).status_code == 202


def test_the_count_is_taken_under_the_accounts_lock(
        real_app, queued, monkeypatch):  # noqa: F811
    import backend.utils.llm_nodes as llm_nodes
    client, uid = _signed_in(real_app, "alice")
    model = _chat_model(real_app)
    entry = _entry(uid)
    calls = []
    real_lock, real_count = (llm_nodes._lock_new_replies,
                             llm_nodes.replies_in_progress)
    monkeypatch.setattr(llm_nodes, "_lock_new_replies", lambda user_id: (
        calls.append(("lock", user_id)), real_lock(user_id))[1])
    monkeypatch.setattr(llm_nodes, "replies_in_progress", lambda user_id: (
        calls.append(("count", user_id)), real_count(user_id))[1])

    assert _ask(client, entry, model).status_code == 202
    assert calls == [("lock", uid), ("count", uid)]


def test_text_mode_keeps_the_message_and_says_why(real_app, queued):  # noqa: F811
    from backend.models import Node
    from backend.utils.llm_nodes import (
        MAX_REPLIES_IN_PROGRESS, TOO_MANY_REPLIES_MESSAGE)
    client, uid = _signed_in(real_app, "alice")
    model = _chat_model(real_app)
    system = _entry(uid, text="(conversation)")
    for _ in range(MAX_REPLIES_IN_PROGRESS):
        assert _ask(client, system, model).status_code == 202

    from flask import g
    g.pop("_login_user", None)
    r = client.post(f"/api/textmode/{system}/message",
                    json={"content": "one more thought", "parent_id": system,
                          "model": model})
    assert r.status_code == 202, r.get_json()
    body = r.get_json()
    assert "llm_node_id" not in body
    assert body["llm_error"] == TOO_MANY_REPLIES_MESSAGE
    assert body["llm_error_code"] == "too_many_replies"
    _db.session.expire_all()
    kept = _db.session.get(Node, body["user_node_id"])
    assert kept is not None and kept.get_content() == "one more thought"
    assert len(queued) == MAX_REPLIES_IN_PROGRESS
