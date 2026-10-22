"""The Celery wrapper of the background ChatGPT/Claude import
(backend/tasks/chat_imports.py), run eagerly on a real Celery app with an
in-memory broker and result backend: progress meta and the result, the
retry after the soft time limit, the max-retries message, and a second
delivery of a running task.

backend.celery_app builds the full app (create_app) at import, so the
task module is imported here against a stand-in backend.celery_app that
holds a real Celery app and the test's Flask app. Other test files put a
MagicMock in sys.modules["celery"]; the fixture swaps the real package
in for these tests and puts everything back afterwards.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

from backend.tests.test_chatgpt_import import app, _db, _make_user  # noqa: F401
from backend.models import Node
from backend.utils import conversation_stash as cs

OPTIONS = {"privacy_level": "private", "ai_usage": "none", "on_deleted": None}


def _analyzed(n_convs, per_conv, prefix="c"):
    return [{
        "name": f"{prefix}{c}", "created_at": f"2024-01-0{1 + c % 9}T10:00:00",
        "messages": [{
            "text": f"{prefix}{c} message {i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "created_at": "2024-01-01T10:00:00", "model": "",
            "mapping_id": f"{prefix}{c}-m{i}",
        } for i in range(per_conv)],
    } for c in range(n_convs)]


def _stash(user_id, convs):
    from backend.routes.import_data import _stash_conversations
    return _stash_conversations(user_id, "chatgpt", convs,
                                sum(len(c["messages"]) for c in convs))


@pytest.fixture
def task(app, monkeypatch):  # noqa: F811
    swapped = {}
    if isinstance(sys.modules.get("celery"), MagicMock):
        for name in [k for k in sys.modules
                     if k == "celery" or k.startswith("celery.")]:
            swapped[name] = sys.modules.pop(name)
    from celery import Celery
    celery_app = Celery("chat-imports-test", broker="memory://",
                        backend="cache+memory://")
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    stand_in = types.ModuleType("backend.celery_app")
    stand_in.celery = celery_app
    stand_in.flask_app = app
    previous = {k: sys.modules.get(k) for k in (
        "backend.celery_app", "backend.tasks.chat_imports")}
    sys.modules["backend.celery_app"] = stand_in
    sys.modules.pop("backend.tasks.chat_imports", None)
    import importlib
    import backend.tasks as tasks_pkg
    previous_attr = getattr(tasks_pkg, "chat_imports", None)
    try:
        # A fresh import against the stand-in (an earlier test file may
        # have imported it against the mocked Celery).
        chat_imports = importlib.import_module("backend.tasks.chat_imports")
        assert chat_imports.celery is celery_app
        monkeypatch.setattr(chat_imports, "RESUME_COUNTDOWN_SECONDS", 0)
        yield chat_imports
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if previous_attr is None:
            if hasattr(tasks_pkg, "chat_imports"):
                delattr(tasks_pkg, "chat_imports")
        else:
            tasks_pkg.chat_imports = previous_attr
        if swapped:
            for name in [k for k in sys.modules
                         if k == "celery" or k.startswith("celery.")]:
                sys.modules.pop(name)
            sys.modules.update(swapped)


def _queued(user_id, convs, task_id):
    token = _stash(user_id, convs)
    assert cs.acquire_lock(user_id, task_id, token, "chatgpt")[0]
    return token


def _keys(user_id):
    return [k for (k,) in _db.session.query(Node.source_key).filter(
        Node.human_owner_id == user_id)]


def test_the_task_imports_and_reports_progress(task, monkeypatch):
    alice = _make_user("alice")
    _db.session.commit()
    token = _queued(alice.id, _analyzed(3, 2), "task-1")
    metas = []
    real_update = task.import_conversations.update_state
    monkeypatch.setattr(task.import_conversations, "update_state",
                        lambda *a, **k: (metas.append(k.get("meta")),
                                         real_update(*a, **k)))
    res = task.import_conversations.apply(args=(alice.id, token, OPTIONS),
                                          task_id="task-1")
    assert res.state == "SUCCESS"
    assert res.result["created"] == 6 and res.result["user_id"] == alice.id
    assert metas[0] == {"user_id": alice.id, "done": 0, "total": 6}
    assert metas[-1] == {"user_id": alice.id, "done": 6, "total": 6}
    assert cs.read_lock(alice.id) is None
    assert not cs.stash_path(alice.id, token).exists()


def test_a_soft_time_limit_continues_in_a_new_run_of_the_same_task(
        task, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded
    from backend.routes import import_data
    alice = _make_user("alice")
    _db.session.commit()
    token = _queued(alice.id, _analyzed(6, 2), "task-2")
    real_add = import_data._add_imported_message_nodes
    calls = {"n": 0}

    def stop_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 6:  # mid-run, after two committed chunks
            raise SoftTimeLimitExceeded()
        return real_add(*args, **kwargs)

    monkeypatch.setattr(import_data, "_add_imported_message_nodes", stop_once)
    real_run = import_data.run_conversation_import
    starts = []
    locks = []

    def small_chunks(user_id, token, task_id, options, **kwargs):
        state = cs.read_state(user_id, token)
        starts.append(state["next_conv"] if state else 0)
        locks.append(cs.read_lock(user_id))
        return real_run(user_id, token, task_id, options, batch_size=2,
                        **kwargs)

    monkeypatch.setattr(import_data, "run_conversation_import", small_chunks)
    res = task.import_conversations.apply(args=(alice.id, token, OPTIONS),
                                          task_id="task-2")
    assert res.state == "SUCCESS"
    assert starts == [0, 2]  # the second run starts at the saved place
    # Between the runs the lock waits in the queue: no crash to count,
    # and nothing for the resume beat.
    assert locks[1]["waiting"] and not locks[1]["running"]
    assert locks[1]["crashes"] == 0
    keys = _keys(alice.id)
    assert len(keys) == 12 and len(set(keys)) == 12
    assert res.result["created"] == 12
    assert cs.read_lock(alice.id) is None


def test_after_the_last_retry_the_user_is_told_to_confirm_again(
        task, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded
    from backend.routes import import_data
    from backend.utils.task_owner import failure_text
    alice = _make_user("alice")
    _db.session.commit()
    token = _queued(alice.id, _analyzed(2, 2), "task-3")
    runs = []

    def always_out_of_time(*args, **kwargs):
        runs.append(1)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(import_data, "_add_imported_message_nodes",
                        always_out_of_time)
    res = task.import_conversations.apply(args=(alice.id, token, OPTIONS),
                                          task_id="task-3")
    assert res.state == "FAILURE"
    assert len(runs) == task.MAX_TIME_LIMIT_RESUMES + 1
    assert "Press Confirm Import again" in failure_text(res.result, "x")
    assert cs.read_lock(alice.id) is None  # a new confirm may resume it
    assert cs.stash_path(alice.id, token).exists()


def test_a_second_delivery_while_the_task_runs_is_ignored(task):
    alice = _make_user("alice")
    _db.session.commit()
    token = _queued(alice.id, _analyzed(1, 2), "task-4")
    with cs.run_guard(alice.id, token):  # the first delivery, running
        res = task.import_conversations.apply(
            args=(alice.id, token, OPTIONS), task_id="task-4")
    assert res.state == "IGNORED"
    assert cs.read_lock(alice.id)["task_id"] == "task-4"
    assert cs.read_state(alice.id, token) is None
    assert _keys(alice.id) == []


# ── a worker killed outright (#546) ─────────────────────────────────────

class _Killed(Exception):
    pass


def _die_mid_run(monkeypatch, user_id, token, task_id, after_nodes):
    """A run whose process dies after *after_nodes* nodes: the lock is
    left running, and the task's last state is its progress."""
    from backend.routes import import_data
    real_add = import_data._add_imported_message_nodes
    made = {"n": 0}

    def dies(*args, **kwargs):
        if made["n"] >= after_nodes:
            raise _Killed()
        made["n"] += 1
        return real_add(*args, **kwargs)

    monkeypatch.setattr(import_data, "_add_imported_message_nodes", dies)
    with pytest.raises(_Killed):
        import_data.run_conversation_import(
            user_id, token, task_id, OPTIONS, batch_size=2,
            resumable=(_Killed,))
    monkeypatch.setattr(import_data, "_add_imported_message_nodes", real_add)
    _db.session.rollback()


def _age_lock(user_id, seconds):
    import os
    import time
    old = time.time() - seconds
    os.utime(cs._lock_path(user_id), (old, old))


def test_the_beat_sends_a_dead_run_again_and_it_finishes(task, monkeypatch):
    task.celery.conf.task_store_eager_result = True
    alice = _make_user("alice")
    _db.session.commit()
    token = _queued(alice.id, _analyzed(6, 2), "task-5")
    _die_mid_run(monkeypatch, alice.id, token, "task-5", 5)
    task.import_conversations.backend.store_result(
        "task-5", {"user_id": alice.id, "done": 4, "total": 12}, "PROGRESS")

    # Within its heartbeats the run counts as alive.
    assert task.resume_stalled_imports.apply().result == {"resumed": 0}
    _age_lock(alice.id, cs.LOCK_STALE_SECONDS + 5)
    assert task.resume_stalled_imports.apply().result == {"resumed": 1}

    # Sent again under the same id: the client's polling follows it.
    done = task.import_conversations.AsyncResult("task-5")
    assert done.state == "SUCCESS"
    assert done.result["created"] == 12
    keys = _keys(alice.id)
    assert len(keys) == 12 and len(set(keys)) == 12
    assert cs.read_lock(alice.id) is None


def test_the_beat_leaves_live_queued_and_ended_tasks_alone(task, monkeypatch):
    from celery import states
    users = [_make_user(name) for name in ("alive", "queued", "ended")]
    _db.session.commit()
    alive, queued, ended = users
    for user, task_id in ((alive, "t-alive"), (queued, "t-queued"),
                          (ended, "t-ended")):
        token = _queued(user.id, _analyzed(1, 2), task_id)
        if user is not queued:
            cs.claim_lock(user.id, task_id, token, "chatgpt", OPTIONS)
    # Waiting behind other tasks in the queue for a few minutes.
    _age_lock(queued.id, cs.LOCK_STALE_SECONDS + 5)
    # Killed at the hard time limit: Celery stored the failure.
    _age_lock(ended.id, cs.LOCK_STALE_SECONDS + 5)
    task.import_conversations.backend.store_result(
        "t-ended", RuntimeError("time limit"), states.FAILURE)

    sent = []
    monkeypatch.setattr(task.import_conversations, "apply_async",
                        lambda *a, **k: sent.append(k))
    assert task.resume_stalled_imports.apply().result == {"resumed": 0}
    assert sent == []
    assert cs.read_lock(alive.id)["task_id"] == "t-alive"
    assert cs.read_lock(queued.id)["task_id"] == "t-queued"
    assert cs.read_lock(ended.id) is None  # only the leftover lock goes


def test_a_task_whose_runs_keep_dying_fails_with_a_message(task, monkeypatch):
    from backend.utils.task_owner import failure_text
    alice = _make_user("alice")
    _db.session.commit()
    token = _queued(alice.id, _analyzed(2, 2), "task-6")
    for _ in range(cs.MAX_CRASH_RESUMES + 1):
        _die_mid_run(monkeypatch, alice.id, token, "task-6", 1)
    res = task.import_conversations.apply(args=(alice.id, token, OPTIONS),
                                          task_id="task-6")
    assert res.state == "FAILURE"
    assert "Press Confirm Import again" in failure_text(res.result, "x")
    assert cs.running_job(alice.id) is None  # a new confirm may start it
    assert cs.stash_path(alice.id, token).exists()
