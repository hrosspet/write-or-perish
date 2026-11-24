"""Open live-update streams (routes/sse.py) per account.

Each open stream is one member of the account's sorted set in Redis
(KEY), scored by when it lapses: ENTRY_TTL_SECONDS after it last wrote.
A stream writes at least every 15 seconds (its heartbeat) and moves its
time on then (at most every REFRESH_SECONDS). It removes its member when
it ends for any reason: its last event, an error, the client gone (the
server then closes the response), or never started. A stream whose
worker was stopped (a deploy) lapses by itself.

A new stream is refused with 429 while MAX_OPEN_STREAMS of the account's
streams have not lapsed. The count, the new member and the check are one
Redis transaction, so streams opened at the same moment are counted one
after the other. Without Redis (a local run, tests, an outage) every
stream is let through, as before.
"""
import logging
import time
from uuid import uuid4

from flask import jsonify

from backend.utils import run_locks

log = logging.getLogger(__name__)

# ── Heuristics (named here, listed in the PR) ─────────────────────────────
# Streams one account may hold open. A Voice turn holds three (the
# recording's transcription, the reply's text, its speech), a thread with
# replies being written one per reply, and an account may have several
# tabs and a phone open: a busy session is about 20. A stream that
# reconnects is counted twice until the old one notices it is gone (up to
# one heartbeat), so 40.
MAX_OPEN_STREAMS = 40
# How long a stream counts after it last wrote: four heartbeats.
ENTRY_TTL_SECONDS = 60
REFRESH_SECONDS = 15

KEY = "loore:open_streams:{user_id}"
TOO_MANY_MESSAGE = ("Too many live updates are open for this account. "
                    "Close some Loore tabs or windows, then try again.")


def _now():
    return time.time()


class Slot:
    """One stream's place in its account's count."""

    def __init__(self, client=None, key=None, member=None, now=0.0):
        self._client = client
        self._key = key
        self._member = member
        self._refreshed = now
        self._released = False

    def refresh(self, now=None):
        if self._client is None or self._released:
            return
        now = _now() if now is None else now
        if now - self._refreshed < REFRESH_SECONDS:
            return
        self._refreshed = now
        try:
            pipe = self._client.pipeline()
            pipe.zadd(self._key, {self._member: now + ENTRY_TTL_SECONDS})
            pipe.expire(self._key, ENTRY_TTL_SECONDS)
            pipe.execute()
        except Exception as e:  # noqa: BLE001 — the stream goes on
            log.warning("Open streams: refresh failed (%s)", type(e).__name__)

    def release(self):
        if self._client is None or self._released:
            return
        self._released = True
        try:
            self._client.zrem(self._key, self._member)
        except Exception as e:  # noqa: BLE001 — it lapses by itself
            log.warning("Open streams: release failed (%s)", type(e).__name__)


def admit(user_id, now=None):
    """A Slot for a new stream of *user_id*, or None when the account
    holds MAX_OPEN_STREAMS already. A Slot that counts nothing when Redis
    cannot be reached."""
    try:
        client = run_locks._client()
    except Exception:  # noqa: BLE001 — no Redis: as before the limit
        return Slot()
    now = _now() if now is None else now
    key = KEY.format(user_id=int(user_id))
    member = uuid4().hex
    try:
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, "-inf", now)
        pipe.zadd(key, {member: now + ENTRY_TTL_SECONDS})
        pipe.zcard(key)
        pipe.expire(key, ENTRY_TTL_SECONDS)
        open_now = int(pipe.execute()[2])
    except Exception as e:  # noqa: BLE001 — the stream must not fail on it
        log.warning("Open streams: count unavailable (%s)", type(e).__name__)
        return Slot()
    slot = Slot(client, key, member, now)
    if open_now > MAX_OPEN_STREAMS:
        slot.release()
        return None
    return slot


class CountedStream:
    """A stream's events, holding its slot: moved on as it writes, freed
    however it ends (the WSGI server closes the response also when the
    client is gone, or when it never read it)."""

    def __init__(self, events, slot):
        self._events = events
        self._slot = slot

    def __iter__(self):
        return self

    def __next__(self):
        try:
            item = next(self._events)
        except BaseException:
            self._slot.release()
            raise
        self._slot.refresh()
        return item

    def close(self):
        try:
            close = getattr(self._events, "close", None)
            if close is not None:
                close()
        finally:
            self._slot.release()


def too_many_response():
    return jsonify({"error": TOO_MANY_MESSAGE,
                    "code": "too_many_streams"}), 429
