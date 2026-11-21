"""Run locks in Redis: one run of a job at a time, and a heartbeat that
says the run is alive.

Why (2026-11-07): a profile build for a multi-million-unit import ran the
server out of memory. The advance task's hard time limit killed it late,
its message was delivered again, and the hourly seeder took the killed
task's FAILURE result for a stale guard, cleared it and started a second
run: two full renders at once. Queued embedding sweeps then ran side by
side after the freeze and inserted the same rows.

A lock is one key whose value is the run's token (a task id) and whose
TTL is set when it is taken and again at every beat:

- ``RunLock`` for a run that beats: ``acquire`` (False while another run,
  or another copy of this one, holds the key), ``beat`` (raises
  ``RunLockLost`` when another run took the key after this one's expired),
  ``release`` (only this run's token is deleted);
- ``holding`` / ``beat``: the run's lock for code deep in the run (the
  window renderer), without passing it down every call;
- ``holder``: the token of the run that holds a key, or None;
- ``single_run``: a context manager for a job without a heartbeat (the
  embedding sweep).

A run that dies (killed, out of memory) stops beating, and its key
expires after the TTL, so the job resumes at its next dispatch.

When Redis cannot be reached (the unit tests, a local run without the
broker) a run goes ahead unlocked and ``holder`` is None: the behaviour
before the locks.
"""
import logging
import threading
from contextlib import contextmanager
from uuid import uuid4

log = logging.getLogger(__name__)

# ── Heuristics (named here, flagged in the PR) ────────────────────────────
# How long a profile window advance holds its user's key after its last
# beat. A healthy run beats before every window render and token count and
# before each slice of its submission, so its longest gap is one render
# and count or one slice's upload: seconds, a minute or two on a huge
# corpus. Fifteen minutes is far above that, and a killed run's build
# resumes at the next hourly seed or the one after.
PROFILE_RUN_TTL = 15 * 60
PROFILE_RUN_KEY = "loore:profile_windows:run:{user_id}"


def profile_run_key(user_id):
    return PROFILE_RUN_KEY.format(user_id=user_id)


# GET, then EXPIRE when the key is this run's; SET when it expired and
# nobody took it. Returns 1 (refreshed), 2 (taken again), 0 (another
# run's). One script, so no other run can take the key between the steps.
BEAT_SCRIPT = """
local v = redis.call('get', KEYS[1])
if v == ARGV[1] then
  redis.call('expire', KEYS[1], ARGV[2])
  return 1
elseif not v then
  redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 2
end
return 0
"""
# DEL only this run's key: a run whose key expired and was taken by
# another never deletes the other's.
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


_clients = {}


def _client():
    """A Redis client on the broker's URL, one per URL and process (its
    pool reconnects after a fork). Raises when there is no app context, no
    redis package, or the app is a test app (TESTING: a test that wants
    Redis installs tests/fake_redis, as utils/task_owner keeps tests off
    a real one); callers treat any error as "Redis unreachable"."""
    import redis
    from flask import current_app
    if current_app.config.get("TESTING"):
        raise RuntimeError("no Redis for a test app")
    url = current_app.config.get("CELERY_BROKER_URL",
                                 "redis://localhost:6379/0")
    client = _clients.get(url)
    if client is None:
        client = _clients[url] = redis.Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=5)
    return client


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def holder(key):
    """The token of the run that holds ``key`` now (its heartbeat is
    fresh), else None. None also when Redis cannot be reached: callers
    then decide as they did before the lock."""
    try:
        return _text(_client().get(key))
    except Exception as e:  # noqa: BLE001 — unreachable counts as unknown
        log.debug("run lock %s unreadable (%s)", key, type(e).__name__)
        return None


class RunLockLost(Exception):
    """Another run holds this run's key now (this run's expired while it
    did not beat, then the other took it): this run stops."""


class RunLock:
    def __init__(self, key, token, ttl):
        self.key = key
        self.token = str(token)
        self.ttl = int(ttl)
        self._redis = None
        # False also when the run goes ahead unlocked (Redis unreachable):
        # beats and the release then do nothing.
        self.locked = False

    def acquire(self):
        """True when this run may go ahead: the key is now this run's, or
        Redis cannot be reached (unlocked). False when another run, or
        another copy of this one with the same token, holds it."""
        try:
            client = _client()
            taken = client.set(self.key, self.token, nx=True, ex=self.ttl)
        except Exception as e:  # noqa: BLE001 — the job must not stop on it
            log.warning("run lock %s unavailable (%s); running unlocked",
                        self.key, type(e).__name__)
            return True
        if not taken:
            return False
        self._redis, self.locked = client, True
        return True

    def holder(self):
        return holder(self.key)

    def beat(self):
        """Refresh the key's TTL. Raises RunLockLost when another run holds
        the key now. A beat that cannot reach Redis is skipped: the key
        lives until its TTL, and the next beat tries again."""
        if not self.locked:
            return
        try:
            result = self._redis.eval(BEAT_SCRIPT, 1, self.key, self.token,
                                      self.ttl)
        except Exception as e:  # noqa: BLE001 — a Redis blip is not a loss
            log.warning("run lock %s: beat failed (%s)", self.key,
                        type(e).__name__)
            return
        result = int(result or 0)
        if result == 0:
            self.locked = False
            raise RunLockLost(f"{self.key} is held by another run")
        if result == 2:
            log.warning("run lock %s had expired before this beat (no beat "
                        "for over %d s); taken again", self.key, self.ttl)

    def release(self):
        if not self.locked:
            return
        self.locked = False
        try:
            self._redis.eval(RELEASE_SCRIPT, 1, self.key, self.token)
        except Exception as e:  # noqa: BLE001 — the TTL ends it anyway
            log.warning("run lock %s: release failed (%s); it expires in "
                        "%d s", self.key, type(e).__name__, self.ttl)


_current = threading.local()


@contextmanager
def holding(lock):
    """Make ``lock`` the current run's lock for ``beat``."""
    previous = getattr(_current, "lock", None)
    _current.lock = lock
    try:
        yield lock
    finally:
        _current.lock = previous


def beat():
    """Beat the current run's lock (``holding``), if there is one. Raises
    RunLockLost when another run holds it now."""
    lock = getattr(_current, "lock", None)
    if lock is not None:
        lock.beat()


@contextmanager
def single_run(key, ttl):
    """One run at a time of a job without a heartbeat. Yields True when
    this run holds the key (or Redis cannot be reached), False when
    another run holds it: the caller skips this run. ``ttl`` must be
    longer than a run normally takes; a run that dies leaves the key
    until it expires."""
    lock = RunLock(key, uuid4().hex, ttl)
    if not lock.acquire():
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


# ── Counters: how often something started in a window ─────────────────────

def count(key, window):
    """Count one more start under ``key``: a counter that begins with its
    first start and lasts ``window`` seconds. Returns the new count, or
    None when Redis cannot be reached (callers then go ahead, as without
    the counter)."""
    try:
        client = _client()
        client.set(key, 0, nx=True, ex=int(window))
        return int(client.incr(key))
    except Exception as e:  # noqa: BLE001 — unreachable counts as unknown
        log.debug("counter %s unavailable (%s)", key, type(e).__name__)
        return None


def counted(key):
    """The count under ``key`` now (0 when none is running), without adding
    to it; None when Redis cannot be reached."""
    try:
        return int(_text(_client().get(key)) or 0)
    except Exception as e:  # noqa: BLE001 — unreachable counts as unknown
        log.debug("counter %s unreadable (%s)", key, type(e).__name__)
        return None
