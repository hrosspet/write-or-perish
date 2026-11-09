"""One heavy background job at a time (#613).

Why: on 2026-11-07 a profile build for a multi-million-unit import ran the
Celery worker out of memory and the whole service stopped answering for
about 50 minutes. Two jobs of that size at once (a second build, or an
import next to a build) need more memory than a worker has. The memory
limit on the worker's unit (docs/RUNBOOKS.md → "Celery queue split")
keeps a runaway job from taking the server down; this slot keeps two
heavy jobs from running at the same time.

What: a task declared with ``base=HeavyTask`` takes one global slot in
Redis before it runs. When another heavy job holds it, the task is sent
again with a countdown (WAIT_SECONDS plus up to WAIT_JITTER_SECONDS),
under the same task id, arguments and retry count, and the worker marks
it RETRY. It is never dropped, its own retry budget (a chat import's
time-limit resumes) is not spent, and the client's status polling reads
it as queued. No extra worker process: with the queue split off or on,
the heavy jobs share the worker they already run on, one at a time.

The slot is one key, SLOT_KEY. Its value names the run that holds it
(task name, task id and a token of its own), and a heartbeat thread
refreshes its TTL every BEAT_SECONDS while the task runs. It is released:

- on success and on failure, by the task's ``finally`` (only its own
  value is deleted);
- on a kill (the memory limit, the hard time limit, a deploy's SIGKILL):
  the process and its heartbeat die together, and the key expires within
  SLOT_TTL_SECONDS. A copy of the killed task delivered again (acks_late)
  has a new token, so it waits for that expiry like any other heavy job.

Only a task a worker runs from the queue takes the slot. A direct call or
an eager apply (tests, a heavy task calling another's function) runs at
once. When Redis cannot be reached the task runs without the slot, as
profile_batch.batch_pipeline_lock does: a job must not stop because the
lock store is down.
"""
import logging
import random
import threading
import time
from uuid import uuid4

from celery import Task

log = logging.getLogger(__name__)

SLOT_KEY = "loore:heavy_slot"

# ── Heuristics (#613, flagged in the PR) ──────────────────────────────────
# How long the slot outlives its holder's last heartbeat: how long heavy
# jobs wait after a holder was killed. Far above a heartbeat's normal gap
# (BEAT_SECONDS), because a process near its memory limit is slowed down
# and its heartbeat with it.
SLOT_TTL_SECONDS = 300
# How often the holder's heartbeat refreshes the TTL.
BEAT_SECONDS = 30
# How long a heavy task waits before it asks again, plus a random part so
# that several waiting tasks do not ask at the same moment.
WAIT_SECONDS = 30
WAIT_JITTER_SECONDS = 10

# Redis client timeouts: connect as batch_pipeline_lock; the socket timeout
# keeps a hung Redis from holding a heartbeat or a release for long.
CONNECT_TIMEOUT_SECONDS = 2
SOCKET_TIMEOUT_SECONDS = 5

# EXPIRE when the key is this run's (1); SET again when it expired and
# nobody took it (2); another run's (0). One script, so no other run can
# take the key between the steps.
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
# DEL only this run's value: a run whose key expired and was taken by
# another never deletes the other's.
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def redis_client(url):
    """A Redis client on the broker's URL (the slot lives on the broker)."""
    import redis
    return redis.Redis.from_url(
        url, socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS)


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def holder_name(value):
    """The run a slot value names (task name and id), without its token."""
    if not value:
        return None
    return value.rsplit(" ", 1)[0]


class Slot:
    """The heavy-job slot for one run of a task."""

    def __init__(self, client, holder, key=SLOT_KEY, ttl=None,
                 beat_every=None, beat_for=None):
        self.client = client
        self.key = key
        self.value = f"{holder} {uuid4().hex}"
        self.ttl = int(SLOT_TTL_SECONDS if ttl is None else ttl)
        self.beat_every = BEAT_SECONDS if beat_every is None else beat_every
        # The heartbeat stops after this many seconds even if release()
        # never ran: no task outlives its hard time limit, and a heartbeat
        # left behind in a pool process must not hold the slot for good.
        self.beat_for = beat_for
        self.held = False
        self._stop = threading.Event()
        self._thread = None

    def acquire(self):
        """True when this run now holds the slot and its heartbeat runs;
        False when another run holds it. Raises when Redis cannot be
        reached (the caller runs the task without the slot)."""
        if not self.client.set(self.key, self.value, nx=True, ex=self.ttl):
            return False
        self.held = True
        self._thread = threading.Thread(
            target=self._heartbeat, name="heavy-slot-heartbeat", daemon=True)
        self._thread.start()
        return True

    def holder(self):
        """The run that holds the slot now (task name and id), or None."""
        try:
            return holder_name(_text(self.client.get(self.key)))
        except Exception:  # noqa: BLE001 - only for the log line
            return None

    def beat(self):
        """Refresh the TTL. Returns 1 (refreshed), 2 (it had expired and
        nobody had taken it: taken again) or 0 (another run holds it)."""
        result = int(self.client.eval(BEAT_SCRIPT, 1, self.key, self.value,
                                      self.ttl) or 0)
        if result == 2:
            log.warning("heavy slot had expired before this beat (no beat "
                        "for over %d s); taken again by %s", self.ttl,
                        holder_name(self.value))
        elif result == 0:
            # This run was too slow to beat for SLOT_TTL_SECONDS and another
            # heavy job started: they now run side by side until this one
            # ends. Logged at ERROR so Sentry shows it.
            log.error("heavy slot lost by %s: %s took it after it expired",
                      holder_name(self.value), self.holder())
        return result

    def _heartbeat(self):
        until = (time.monotonic() + self.beat_for) if self.beat_for else None
        while not self._stop.wait(self.beat_every):
            if until is not None and time.monotonic() > until:
                log.warning("heavy slot heartbeat of %s stopped after %d s "
                            "(the task's hard time limit)",
                            holder_name(self.value), self.beat_for)
                return
            try:
                if self.beat() == 0:
                    return
            except Exception as e:  # noqa: BLE001 - a Redis blip is not a loss
                log.warning("heavy slot beat failed (%s); the key lives "
                            "until its TTL", type(e).__name__)

    def release(self):
        """Stop the heartbeat, then delete the key if it is still this
        run's. A failed delete leaves the key to its TTL."""
        if not self.held:
            return
        self.held = False
        self._stop.set()
        if self._thread is not None:
            # A beat in flight finishes first, so it cannot set the key
            # again after the delete.
            self._thread.join(SOCKET_TIMEOUT_SECONDS + 1)
        try:
            self.client.eval(RELEASE_SCRIPT, 1, self.key, self.value)
        except Exception as e:  # noqa: BLE001 - the TTL ends it anyway
            log.warning("heavy slot release failed (%s); it expires within "
                        "%d s", type(e).__name__, self.ttl)

    def close(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001 - nothing left to do
            pass


def wait_countdown():
    return WAIT_SECONDS + random.uniform(0, WAIT_JITTER_SECONDS)


def wait_for_slot(task, holder=None):
    """Send this run of *task* again after a countdown and raise Retry.

    The copy keeps the task id (the profile guard and the import status
    follow it), the arguments, the queue it came from and the retry count:
    Task.retry would add one, and a chat import counts its time-limit
    resumes in it. Returns without raising only when the copy could not be
    sent: the caller then runs the task now rather than drop it."""
    from celery.exceptions import Retry
    countdown = wait_countdown()
    request = task.request
    sig = task.signature_from_request(request, countdown=countdown)
    try:
        sig.apply_async()
    except Exception as e:  # noqa: BLE001 - never drop the task
        log.error("%s[%s] could not be queued again to wait for the heavy "
                  "slot (%s); it runs now beside %s", task.name, request.id,
                  type(e).__name__, holder)
        return
    raise Retry(f"Retry in {countdown:.0f}s: waiting for the heavy-job "
                f"slot (held by {holder})", when=countdown, sig=sig)


class HeavyTask(Task):
    """Base class of the heavy background jobs: one runs at a time.

    Celery calls a task's ``__call__`` when its class defines one. In a
    worker (and in an eager apply) the request is already pushed, so this
    calls ``run`` itself, as Celery does for a task without a custom
    ``__call__``; a direct call goes through ``Task.__call__``, which
    pushes one."""

    def __call__(self, *args, **kwargs):
        request = self.request
        if request.called_directly:
            return super().__call__(*args, **kwargs)
        if request.is_eager:
            return self.run(*args, **kwargs)
        slot = None
        try:
            slot = Slot(redis_client(self.app.conf.broker_url),
                        f"{self.name}[{request.id}]",
                        beat_for=self._hard_time_limit())
            acquired = slot.acquire()
        except Exception as e:  # noqa: BLE001 - run unlocked, never stop
            if slot is not None:
                slot.close()
            log.warning("heavy slot unavailable (%s); %s[%s] runs without "
                        "it", type(e).__name__, self.name, request.id)
            return self.run(*args, **kwargs)
        if not acquired:
            try:
                wait_for_slot(self, slot.holder())
            finally:
                slot.close()
            return self.run(*args, **kwargs)
        try:
            return self.run(*args, **kwargs)
        finally:
            slot.release()
            slot.close()

    def _hard_time_limit(self):
        limits = self.request.timelimit or (None, None)
        return (limits[0] or self.time_limit
                or self.app.conf.task_time_limit or None)
