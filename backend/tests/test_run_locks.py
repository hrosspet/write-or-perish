"""Run locks with a heartbeat (backend/utils/run_locks.py) and the broker's
visibility timeout (celery_app): the guards against a second run of a job
while the first still runs (2026-11-07: a redelivered profile build and a
second dispatch rendered at the same time)."""
import ast
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
sys.modules.setdefault("celery", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from backend.utils import run_locks  # noqa: E402
from backend.tests.fake_redis import install  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["CELERY_BROKER_URL"] = "redis://localhost:1/0"
    with app.app_context():
        yield app


def _lock(token, key="k", ttl=60):
    return run_locks.RunLock(key, token, ttl)


def test_one_holder_at_a_time_and_release_frees_it(app, monkeypatch):
    redis = install(monkeypatch)
    a, b = _lock("a"), _lock("b")
    assert a.acquire()
    assert not b.acquire()
    # A second copy with the SAME token (its message delivered again) is
    # refused too: the key is not re-entrant.
    assert not _lock("a").acquire()
    assert run_locks.holder("k") == "a"
    a.release()
    assert run_locks.holder("k") is None
    assert b.acquire()
    assert redis.get("k") == b"b"


def test_a_beat_keeps_the_key_alive(app, monkeypatch):
    redis = install(monkeypatch)
    a = _lock("a", ttl=60)
    assert a.acquire()
    for _ in range(5):
        redis.advance(50)
        a.beat()
    assert run_locks.holder("k") == "a"
    assert not _lock("b").acquire()


def test_a_dead_run_s_key_expires_on_its_own(app, monkeypatch):
    redis = install(monkeypatch)
    assert _lock("a", ttl=60).acquire()   # then the run is killed
    redis.advance(59)
    assert not _lock("b").acquire()
    redis.advance(2)
    assert run_locks.holder("k") is None
    assert _lock("b").acquire()


def test_a_beat_after_another_run_took_the_key_raises(app, monkeypatch):
    redis = install(monkeypatch)
    a = _lock("a", ttl=60)
    assert a.acquire()
    redis.advance(61)                     # frozen past the TTL
    b = _lock("b", ttl=60)
    assert b.acquire()
    with pytest.raises(run_locks.RunLockLost):
        a.beat()
    a.release()                           # never deletes the other's key
    assert run_locks.holder("k") == "b"


def test_a_beat_after_expiry_takes_the_key_again_when_free(app, monkeypatch):
    redis = install(monkeypatch)
    a = _lock("a", ttl=60)
    assert a.acquire()
    redis.advance(61)
    a.beat()
    assert run_locks.holder("k") == "a"


def test_beat_reaches_the_current_run_s_lock(app, monkeypatch):
    redis = install(monkeypatch)
    a = _lock("a", ttl=60)
    assert a.acquire()
    run_locks.beat()                      # no current lock: nothing
    with run_locks.holding(a):
        redis.advance(50)
        run_locks.beat()
        redis.advance(50)
        assert run_locks.holder("k") == "a"
    redis.advance(50)
    run_locks.beat()                      # outside: nothing
    assert run_locks.holder("k") is None


def test_unreachable_redis_runs_unlocked(app):
    # Port 1 refuses: the job goes ahead and nobody counts as running.
    a = _lock("a")
    assert a.acquire()
    assert not a.locked
    a.beat()
    a.release()
    assert run_locks.holder("k") is None
    with run_locks.single_run("s", 60) as ok:
        assert ok is True


def test_single_run_skips_while_held_and_frees_after(app, monkeypatch):
    install(monkeypatch)
    with run_locks.single_run("s", 60) as first:
        assert first is True
        with run_locks.single_run("s", 60) as second:
            assert second is False
    with run_locks.single_run("s", 60) as third:
        assert third is True


def _value(node, names):
    """Integer arithmetic of module constants (no eval)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name):
        return names[node.id]
    if isinstance(node, ast.BinOp):
        left, right = _value(node.left, names), _value(node.right, names)
        ops = {ast.Mult: lambda a, b: a * b, ast.Add: lambda a, b: a + b,
               ast.Sub: lambda a, b: a - b}
        return ops[type(node.op)](left, right)
    raise ValueError(ast.dump(node))


def _module_constants(path):
    out = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = _value(node.value, out)
            except (ValueError, KeyError):
                pass
    return out


def test_visibility_timeout_is_above_the_hard_limit_and_the_countdowns():
    """Redis delivers an unacked message again after the visibility
    timeout: it must outlast an acks_late task run to the hard limit (and
    a reserved message's wait for a free process), and every countdown
    the code schedules."""
    text = (ROOT / "celery_app.py").read_text()
    consts = _module_constants(ROOT / "celery_app.py")
    timeout = consts["BROKER_VISIBILITY_TIMEOUT"]
    assert timeout >= 2 * consts["TASK_TIME_LIMIT"] + 600
    assert "task_time_limit=TASK_TIME_LIMIT" in text
    assert ("broker_transport_options={'visibility_timeout': "
            "BROKER_VISIBILITY_TIMEOUT}") in text
    # The longest fixed countdown: an import's deferred profile seed.
    from datetime import timedelta
    seed_wait = timedelta(minutes=30)
    assert "MIN_INACTIVITY = timedelta(minutes=30)" in (
        ROOT / "tasks" / "profile_batch.py").read_text()
    assert seed_wait.total_seconds() < timeout
