"""One heavy background job at a time (#613, backend/utils/heavy_slot.py).

- The slot: taken by one run, refused to the next, refreshed by the
  heartbeat, deleted only by its own run, and expired after its TTL when
  the run that held it was killed.
- HeavyTask under Celery's real tracer: a free slot runs the task and
  releases the slot on success and on failure; a held slot sends the task
  again with a countdown (same id, arguments, queue and retry count) and
  marks it RETRY, so it is never dropped; Redis unreachable, a direct call
  and an eager apply run the task at once.
- A task id whose runs were killed MAX_KILLED_RUNS times is not run
  again: its stop handler runs, it fails, and the slot and the count go.
- The real app: exactly the heavy jobs use HeavyTask, and all of them are
  on the background queue.

Other test files put a MagicMock in sys.modules["celery"]; the module
fixture swaps the real package in, imports heavy_slot against it, and puts
everything back afterwards (as test_chat_imports_task does).
"""
import importlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import types
from uuid import uuid4

import pytest

from backend import celery_queues as cq

REPO = pathlib.Path(__file__).resolve().parents[2]
# Task ids unique to this run: Celery's in-memory result backend is one
# cache for the whole process.
RUN = uuid4().hex[:8]

# Set by _real_celery for the tests of this module.
hs = Celery = build_tracer = Signature = None


@pytest.fixture(autouse=True, scope="module")
def _real_celery():
    global hs, Celery, build_tracer, Signature
    swapped = {}
    if not isinstance(sys.modules.get("celery"), types.ModuleType):
        for name in [k for k in sys.modules
                     if k == "celery" or k.startswith("celery.")]:
            swapped[name] = sys.modules.pop(name)
    import backend.utils as utils_pkg
    previous = sys.modules.pop("backend.utils.heavy_slot", None)
    previous_attr = getattr(utils_pkg, "heavy_slot", None)
    try:
        hs = importlib.import_module("backend.utils.heavy_slot")
        from celery import Celery as _Celery
        from celery.app.trace import build_tracer as _build_tracer
        from celery.canvas import Signature as _Signature
        Celery, build_tracer, Signature = _Celery, _build_tracer, _Signature
        yield
    finally:
        if previous is None:
            sys.modules.pop("backend.utils.heavy_slot", None)
        else:
            sys.modules["backend.utils.heavy_slot"] = previous
        if previous_attr is None:
            if hasattr(utils_pkg, "heavy_slot"):
                delattr(utils_pkg, "heavy_slot")
        else:
            utils_pkg.heavy_slot = previous_attr
        if swapped:
            for name in [k for k in sys.modules
                         if k == "celery" or k.startswith("celery.")]:
                sys.modules.pop(name)
            sys.modules.update(swapped)


class FakeRedis:
    """The few Redis commands the slot uses, with a clock the test moves."""

    def __init__(self):
        self.data = {}   # key -> (value, expires_at)
        self.clock = 1000.0
        self.lock = threading.Lock()
        self.evals = []
        self.fail = False
        self.closed = False

    def _live(self, key):
        item = self.data.get(key)
        if item is not None and item[1] is not None and item[1] <= self.clock:
            del self.data[key]
            item = None
        return item

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def set(self, key, value, nx=False, ex=None):
        self._check()
        with self.lock:
            if nx and self._live(key) is not None:
                return None
            self.data[key] = (value.encode(),
                              None if ex is None else self.clock + ex)
            return True

    def get(self, key):
        self._check()
        with self.lock:
            item = self._live(key)
            return None if item is None else item[0]

    def ttl(self, key):
        with self.lock:
            item = self._live(key)
            if item is None:
                return -2
            return int(item[1] - self.clock)

    def eval(self, script, numkeys, key, value, *args):
        self._check()
        with self.lock:
            self.evals.append(script)
            item = self._live(key)
            mine = item is not None and item[0] == value.encode()
            if "'expire'" in script:   # heavy_slot.BEAT_SCRIPT
                ttl = int(args[0])
                if mine:
                    self.data[key] = (item[0], self.clock + ttl)
                    return 1
                if item is None:
                    self.data[key] = (value.encode(), self.clock + ttl)
                    return 2
                return 0
            if "'del'" in script:   # heavy_slot.RELEASE_SCRIPT
                if mine:
                    del self.data[key]
                    return 1
                return 0
            raise AssertionError("unknown script")

    def incr(self, key):
        self._check()
        with self.lock:
            item = self._live(key)
            value = int(item[0]) + 1 if item else 1
            self.data[key] = (str(value).encode(),
                              item[1] if item else None)
            return value

    def expire(self, key, seconds):
        self._check()
        with self.lock:
            item = self._live(key)
            if item is not None:
                self.data[key] = (item[0], self.clock + seconds)

    def delete(self, key):
        self._check()
        with self.lock:
            self.data.pop(key, None)

    def close(self):
        self.closed = True


def _slot(fake, holder="t.heavy[a]", **kw):
    kw.setdefault("beat_every", 3600)   # no heartbeat unless a test wants it
    return hs.Slot(fake, holder, **kw)


# --- the slot ----------------------------------------------------------------

def test_one_run_holds_the_slot_and_the_next_is_refused():
    fake = FakeRedis()
    first, second = _slot(fake, "t.a[1]"), _slot(fake, "t.b[2]")
    assert first.acquire() is True
    assert second.acquire() is False
    assert second.holder() == "t.a[1]"
    assert fake.ttl(hs.SLOT_KEY) == hs.SLOT_TTL_SECONDS
    first.release()
    assert fake.get(hs.SLOT_KEY) is None
    assert second.acquire() is True
    second.release()


def test_the_slot_of_a_killed_run_expires_after_its_ttl():
    fake = FakeRedis()
    killed = _slot(fake, "t.a[1]")
    assert killed.acquire()
    # Killed: no release, and its heartbeat died with the process.
    killed._stop.set()
    killed._thread.join()
    fake.clock += hs.SLOT_TTL_SECONDS - 1
    assert _slot(fake, "t.b[2]").acquire() is False
    fake.clock += 2
    nxt = _slot(fake, "t.b[2]")
    assert nxt.acquire() is True
    nxt.release()


def test_a_beat_refreshes_the_ttl():
    fake = FakeRedis()
    slot = _slot(fake)
    slot.acquire()
    fake.clock += hs.SLOT_TTL_SECONDS - 10
    assert slot.beat() == 1
    fake.clock += hs.SLOT_TTL_SECONDS - 10
    assert _slot(fake, "t.b[2]").acquire() is False
    slot.release()


def test_a_beat_after_expiry_takes_the_free_slot_again():
    fake = FakeRedis()
    slot = _slot(fake)
    slot.acquire()
    fake.clock += hs.SLOT_TTL_SECONDS + 1
    assert slot.beat() == 2
    assert _slot(fake, "t.b[2]").acquire() is False
    slot.release()
    assert fake.get(hs.SLOT_KEY) is None


def test_a_slot_another_run_took_is_reported_and_never_deleted(caplog):
    fake = FakeRedis()
    slow = _slot(fake, "t.a[1]")
    slow.acquire()
    fake.clock += hs.SLOT_TTL_SECONDS + 1
    other = _slot(fake, "t.b[2]")
    assert other.acquire() is True
    with caplog.at_level("ERROR"):
        assert slow.beat() == 0
    assert "heavy slot lost by t.a[1]" in caplog.text
    slow.release()
    assert _slot(fake, "t.c[3]").holder() == "t.b[2]"
    other.release()


def test_the_heartbeat_thread_beats_and_stops_on_release():
    fake = FakeRedis()
    slot = _slot(fake, beat_every=0.01)
    slot.acquire()
    deadline = time.monotonic() + 5
    while hs.BEAT_SCRIPT not in fake.evals and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hs.BEAT_SCRIPT in fake.evals
    slot.release()
    assert not slot._thread.is_alive()
    assert fake.get(hs.SLOT_KEY) is None


def test_the_heartbeat_stops_by_itself_after_the_hard_time_limit():
    fake = FakeRedis()
    slot = _slot(fake, beat_every=0.01, beat_for=0.05)
    slot.acquire()
    slot._thread.join(5)
    assert not slot._thread.is_alive()
    slot.release()


def test_a_release_redis_refuses_leaves_the_key_to_its_ttl():
    fake = FakeRedis()
    slot = _slot(fake)
    slot.acquire()
    fake.fail = True
    slot.release()   # no exception
    fake.fail = False
    assert fake.get(hs.SLOT_KEY) is not None
    fake.clock += hs.SLOT_TTL_SECONDS + 1
    assert fake.get(hs.SLOT_KEY) is None


def test_wait_countdown_is_within_the_bounds():
    for _ in range(50):
        c = hs.wait_countdown()
        assert hs.WAIT_SECONDS <= c <= hs.WAIT_SECONDS + hs.WAIT_JITTER_SECONDS


# --- HeavyTask under Celery's tracer -----------------------------------------

@pytest.fixture
def app():
    app = Celery("heavy-slot-test", broker="memory://",
                 backend="cache+memory://")
    app.conf.task_time_limit = 3600
    return app


@pytest.fixture
def fake(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(hs, "redis_client", lambda url: fake)
    return fake


@pytest.fixture
def sent(monkeypatch):
    """The messages a waiting task sends again (instead of the broker)."""
    sent = []

    def apply_async(self, *a, **kw):
        sent.append(dict(self.options, args=self.args, kwargs=self.kwargs))
    monkeypatch.setattr(Signature, "apply_async", apply_async)
    return sent


def _define(app, ran, fail=False):
    @app.task(bind=True, base=hs.HeavyTask, name="t.heavy")
    def heavy(self, a, b=None):
        ran.append({"id": self.request.id, "args": (a, b),
                    "slot": hs.holder_name(_held(app))})
        if fail:
            raise ValueError("boom")
        return a
    return heavy


def _held(app):
    try:
        value = hs.redis_client(app.conf.broker_url).get(hs.SLOT_KEY)
    except ConnectionError:
        return None
    return value.decode() if value else None


def _run_in_worker(app, task, task_id, args=(), kwargs=None, retries=0,
                   queue="background"):
    """Run *task* as a worker runs a message from *queue*."""
    trace = build_tracer(task.name, task, app=app, eager=False,
                         propagate=False)
    request = {"id": task_id, "retries": retries, "hostname": "w@test",
               "delivery_info": {"exchange": "", "routing_key": queue}}
    trace(task_id, list(args), kwargs or {}, request)
    return app.AsyncResult(task_id).state


def test_a_free_slot_runs_the_task_and_releases_the_slot(app, fake, sent):
    ran = []
    heavy = _define(app, ran)
    assert _run_in_worker(app, heavy, f"{RUN}-id-1", (5,), {"b": 6}) == "SUCCESS"
    assert ran == [{"id": f"{RUN}-id-1", "args": (5, 6), "slot": f"t.heavy[{RUN}-id-1]"}]
    assert fake.get(hs.SLOT_KEY) is None
    assert sent == []
    assert fake.closed


def test_a_failing_task_releases_the_slot(app, fake, sent):
    ran = []
    heavy = _define(app, ran, fail=True)
    assert _run_in_worker(app, heavy, f"{RUN}-id-fail", (5,)) == "FAILURE"
    assert len(ran) == 1
    assert fake.get(hs.SLOT_KEY) is None


def test_a_held_slot_sends_the_task_again_and_marks_it_retry(app, fake, sent):
    ran = []
    heavy = _define(app, ran)
    holder = _slot(fake, "t.other[x]")
    holder.acquire()
    state = _run_in_worker(app, heavy, f"{RUN}-id-2", (7,), {"b": 8}, retries=2,
                           queue="celery")
    assert state == "RETRY"
    assert ran == []
    assert len(sent) == 1
    msg = sent[0]
    # The same task, unchanged: id, arguments, queue and retry count.
    assert msg["task_id"] == f"{RUN}-id-2"
    assert list(msg["args"]) == [7] and msg["kwargs"] == {"b": 8}
    assert msg["queue"] == "celery"
    assert msg["retries"] == 2
    assert hs.WAIT_SECONDS <= msg["countdown"] <= (
        hs.WAIT_SECONDS + hs.WAIT_JITTER_SECONDS)
    # The holder's slot is untouched.
    assert hs.holder_name(_held(app)) == "t.other[x]"
    holder.release()
    # Delivered again once the slot is free, it runs under the same id.
    assert _run_in_worker(app, heavy, f"{RUN}-id-2", (7,), {"b": 8},
                          retries=2) == "SUCCESS"
    assert ran[0]["id"] == f"{RUN}-id-2"


def test_two_heavy_tasks_never_run_at_once(app, fake, sent):
    """The second task, started while the first runs, waits; it runs
    after the first has ended."""
    order = []

    @app.task(bind=True, base=hs.HeavyTask, name="t.first")
    def first(self):
        order.append("first starts")
        assert _run_in_worker(app, second, f"{RUN}-id-b") == "RETRY"
        order.append("first ends")

    @app.task(bind=True, base=hs.HeavyTask, name="t.second")
    def second(self):
        order.append("second runs")

    assert _run_in_worker(app, first, f"{RUN}-id-a") == "SUCCESS"
    assert [m["task_id"] for m in sent] == [f"{RUN}-id-b"]
    assert _run_in_worker(app, second, f"{RUN}-id-b") == "SUCCESS"
    assert order == ["first starts", "first ends", "second runs"]


def test_a_task_that_cannot_be_sent_again_runs_now(app, fake, monkeypatch):
    ran = []
    heavy = _define(app, ran)
    holder = _slot(fake, "t.other[x]")
    holder.acquire()

    def broken(self, *a, **kw):
        raise ConnectionError("broker down")
    monkeypatch.setattr(Signature, "apply_async", broken)
    assert _run_in_worker(app, heavy, f"{RUN}-id-3", (1,)) == "SUCCESS"
    assert len(ran) == 1
    holder.release()


def test_redis_unreachable_runs_the_task_without_the_slot(app, fake, sent):
    ran = []
    heavy = _define(app, ran)
    fake.fail = True
    assert _run_in_worker(app, heavy, f"{RUN}-id-4", (1,)) == "SUCCESS"
    assert len(ran) == 1 and sent == []


def test_a_direct_call_and_an_eager_apply_do_not_touch_the_slot(
        app, monkeypatch):
    def no_redis(url):
        raise AssertionError("the slot was used")
    monkeypatch.setattr(hs, "redis_client", no_redis)

    @app.task(bind=True, base=hs.HeavyTask, name="t.plain")
    def plain(self, a):
        return (self.request.id, a)

    assert plain(3) == (None, 3)
    result = plain.apply((4,), task_id=f"{RUN}-eager-1")
    assert result.get() == (f"{RUN}-eager-1", 4)


def test_an_unbound_heavy_task_runs(app, fake, sent):
    @app.task(base=hs.HeavyTask, name="t.unbound")
    def unbound(first_copy=False):
        return {"first_copy": first_copy}

    assert _run_in_worker(app, unbound, f"{RUN}-id-5", (),
                          {"first_copy": True}) == "SUCCESS"
    assert app.AsyncResult(f"{RUN}-id-5").get() == {"first_copy": True}


# --- a run killed again and again --------------------------------------------

def _runs(fake, task_id):
    value = fake.get(hs.RUNS_KEY.format(task_id=task_id))
    return None if value is None else int(value)


def test_a_run_counts_its_start_and_an_ended_run_forgets_it(app, fake, sent):
    seen = []

    @app.task(bind=True, base=hs.HeavyTask, name="t.counted")
    def counted(self, fail=False):
        seen.append(_runs(fake, self.request.id))
        if fail:
            raise ValueError("boom")

    ok, bad = f"{RUN}-count-ok", f"{RUN}-count-bad"
    assert _run_in_worker(app, counted, ok) == "SUCCESS"
    assert _run_in_worker(app, counted, bad, (), {"fail": True}) == "FAILURE"
    assert seen == [1, 1]
    assert _runs(fake, ok) is None and _runs(fake, bad) is None
    assert fake.ttl(hs.SLOT_KEY) == -2


class Killed(Exception):
    """Stands in for the SIGKILL in a test run (the tracer then stores
    FAILURE, which a later state may replace; SUCCESS it would keep)."""


def _killed(fake, monkeypatch):
    """Runs end as after a SIGKILL: no finally (the count and the slot
    stay; the heartbeat dies with the process)."""
    monkeypatch.setattr(hs, "forget_starts", lambda client, task_id: None)
    monkeypatch.setattr(hs.Slot, "release", lambda self: self._stop.set())


def test_a_task_killed_three_times_fails_on_its_next_start(
        app, fake, sent, monkeypatch):
    runs, stopped = [], []

    def stop(task, n):
        stopped.append((task.request.id, n))

    @app.task(bind=True, base=hs.HeavyTask, name="t.killed",
              acks_late=True, reject_on_worker_lost=True,
              on_repeated_kill=stop)
    def killed(self, n):
        runs.append(n)
        raise Killed()

    task_id = f"{RUN}-killed"
    with monkeypatch.context() as m:
        _killed(fake, m)
        for i in range(hs.MAX_KILLED_RUNS):
            if i:
                # Delivered again after the kill: it waits for the dead
                # run's slot to expire, then runs (and is killed again).
                assert _run_in_worker(app, killed, task_id, (7,)) == "RETRY"
                fake.clock += hs.SLOT_TTL_SECONDS + 1
            _run_in_worker(app, killed, task_id, (7,))
    assert len(runs) == hs.MAX_KILLED_RUNS
    assert _runs(fake, task_id) == hs.MAX_KILLED_RUNS
    fake.clock += hs.SLOT_TTL_SECONDS + 1

    # The next start does not run it: the stop is recorded, the task
    # fails (so the message is acked), the slot and the count are gone.
    assert _run_in_worker(app, killed, task_id, (7,)) == "FAILURE"
    assert len(runs) == hs.MAX_KILLED_RUNS
    assert stopped == [(task_id, 7)]
    result = app.AsyncResult(task_id)
    assert type(result.result).__name__ == "HeavyJobKilled"
    assert _runs(fake, task_id) is None
    assert fake.get(hs.SLOT_KEY) is None


def test_two_kills_still_run_the_task(app, fake, sent):
    runs = []

    @app.task(bind=True, base=hs.HeavyTask, name="t.twice")
    def twice(self):
        runs.append(1)

    task_id = f"{RUN}-twice"
    fake.set(hs.RUNS_KEY.format(task_id=task_id),
             str(hs.MAX_KILLED_RUNS - 1), ex=hs.RUNS_TTL_SECONDS)
    assert _run_in_worker(app, twice, task_id) == "SUCCESS"
    assert runs == [1]
    assert _runs(fake, task_id) is None


class ForTheUser(Exception):
    """A stop handler's own error (e.g. a message written for the user)."""


def test_the_stop_handler_chooses_the_error(app, fake, sent):
    def stop(task):
        raise ForTheUser("written for the user")

    @app.task(bind=True, base=hs.HeavyTask, name="t.handled",
              on_repeated_kill=stop)
    def handled(self):
        raise AssertionError("must not run")

    task_id = f"{RUN}-handled"
    fake.set(hs.RUNS_KEY.format(task_id=task_id), str(hs.MAX_KILLED_RUNS),
             ex=hs.RUNS_TTL_SECONDS)
    assert _run_in_worker(app, handled, task_id) == "FAILURE"
    result = app.AsyncResult(task_id).result
    assert type(result).__name__ == "ForTheUser"
    assert "written for the user" in str(result)
    assert fake.get(hs.SLOT_KEY) is None


def test_the_count_kept_for_a_day(app, fake, sent, monkeypatch):
    @app.task(bind=True, base=hs.HeavyTask, name="t.ttl")
    def ttl(self):
        return fake.ttl(hs.RUNS_KEY.format(task_id=self.request.id))

    task_id = f"{RUN}-ttl"
    _run_in_worker(app, ttl, task_id)
    assert app.AsyncResult(task_id).get() == hs.RUNS_TTL_SECONDS


def test_a_count_redis_refuses_runs_the_task(app, fake, sent, monkeypatch):
    runs = []

    @app.task(bind=True, base=hs.HeavyTask, name="t.uncounted")
    def uncounted(self):
        runs.append(1)

    def broken(key):
        raise ConnectionError("redis down")
    monkeypatch.setattr(fake, "incr", broken)
    assert _run_in_worker(app, uncounted, f"{RUN}-uncounted") == "SUCCESS"
    assert runs == [1]


# --- the real app ------------------------------------------------------------

HEAVY_TASKS = {
    "backend.tasks.profile_windows.advance_profile_windows",
    "backend.tasks.exports.update_user_profile",
    "backend.tasks.chat_imports.import_conversations",
    "backend.tasks.imports.import_twitter_archive",
    "backend.tasks.imports.prefill_community_archive",
    "backend.tasks.imports.prefill_x_api",
    "backend.tasks.imports.refresh_community_archive_snapshot",
}

_PROBE = """
import json
from backend.celery_app import celery
from backend.utils.heavy_slot import HeavyTask
print(json.dumps(sorted(name for name, task in celery.tasks.items()
                        if isinstance(task, HeavyTask))))
"""


def test_exactly_the_heavy_jobs_take_the_slot():
    env = dict(os.environ)
    env.update({"DATABASE_URL": "sqlite:///:memory:",
                "ENCRYPTION_DISABLED": "true", "SENTRY_DSN": "",
                "PYTHONPATH": str(REPO)})
    result = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO,
                            env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    heavy = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert heavy == HEAVY_TASKS


def test_every_heavy_job_is_on_the_background_queue():
    for name in HEAVY_TASKS:
        assert cq.TASK_QUEUES[name] == cq.BACKGROUND_QUEUE
