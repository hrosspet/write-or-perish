"""Open live-update streams per account (backend/utils/open_streams.py).

- An account holds at most MAX_OPEN_STREAMS open streams; the next gets
  429 "too_many_streams" before its stream starts, on every stream route;
  another account is not held up.
- A stream's place is freed however it ends: its last event, an error,
  the client gone (the response closed part-way), a response never read.
- A stream that stopped writing (its worker was stopped) lapses after
  ENTRY_TTL_SECONDS; a stream that writes moves its time on.
- A busy session's streams are all let through, and without Redis every
  stream is (controls).
"""
import pytest

from backend.tests.test_tts_stream_sse import (  # noqa: F401
    app, client, _node, _events, User,
)
from backend.utils import open_streams, run_locks


class _Pipe:
    def __init__(self, redis):
        self.redis, self.calls = redis, []

    def __getattr__(self, name):
        def call(*a):
            self.calls.append((name, a))
            return self
        return call

    def execute(self):
        return [getattr(self.redis, n)(*a) for n, a in self.calls]


class _SetRedis:
    """Sorted sets on a clock the test moves, and a transaction that runs
    its commands one after the other."""

    def __init__(self):
        self.sets = {}
        self.now = 1000.0

    def pipeline(self):
        return _Pipe(self)

    def zremrangebyscore(self, key, low, high):
        s = self.sets.get(key, {})
        for m in [m for m, score in s.items() if score <= float(high)]:
            del s[m]

    def zadd(self, key, mapping):
        self.sets.setdefault(key, {}).update(mapping)

    def zcard(self, key):
        return len(self.sets.get(key, {}))

    def zrem(self, key, member):
        self.sets.get(key, {}).pop(member, None)

    def expire(self, key, ttl):
        return True

    def open(self, user_id):
        key = open_streams.KEY.format(user_id=user_id)
        return len([s for s in self.sets.get(key, {}).values()
                    if s > self.now])


@pytest.fixture
def redis(monkeypatch):
    fake = _SetRedis()
    monkeypatch.setattr(run_locks, "_client", lambda: fake)
    monkeypatch.setattr(open_streams, "_now", lambda: fake.now)
    return fake


def _user_id():
    return User.query.first().id


def _fill(user_id, n):
    slots = [open_streams.admit(user_id) for _ in range(n)]
    assert all(s is not None for s in slots)
    return slots


def test_an_account_at_the_limit_is_refused_a_new_stream(app, client, redis):
    uid = _user_id()
    slots = _fill(uid, open_streams.MAX_OPEN_STREAMS)
    node_id = _node("completed", "/media/n/tts.mp3?v=1", chunks=1)
    resp = client.get(f"/api/sse/nodes/{node_id}/tts-stream")
    assert resp.status_code == 429
    assert resp.get_json()["code"] == "too_many_streams"
    assert redis.open(uid) == open_streams.MAX_OPEN_STREAMS
    # Another account is not held up.
    assert open_streams.admit(uid + 1) is not None
    # One stream ends: a new one is let through, and freed at its end.
    slots[0].release()
    resp = client.get(f"/api/sse/nodes/{node_id}/tts-stream")
    assert resp.status_code == 200
    assert [e for e, _ in _events(resp)][-1] == "all_complete"
    assert redis.open(uid) == open_streams.MAX_OPEN_STREAMS - 1


@pytest.mark.parametrize("path", [
    "/api/sse/nodes/{node}/transcription-stream",
    "/api/sse/nodes/{node}/tts-stream",
    "/api/sse/nodes/{node}/llm-stream",
    "/api/sse/drafts/{session}/transcription-stream",
])
def test_every_stream_route_counts(app, client, redis, path):
    from backend.extensions import db
    from backend.models import Draft, Node
    uid = _user_id()
    node = Node(user_id=uid, node_type="llm", streaming_transcription=True,
                tts_task_status="processing", llm_task_status="processing")
    node.set_content("text")
    db.session.add(node)
    db.session.add(Draft(user_id=uid, session_id="abc123",
                         streaming_status="recording"))
    db.session.commit()
    _fill(uid, open_streams.MAX_OPEN_STREAMS)
    resp = client.get(path.format(node=node.id, session="abc123"))
    assert resp.status_code == 429
    assert resp.get_json()["code"] == "too_many_streams"


def _events_then(fail=False):
    yield "one"
    yield "two"
    if fail:
        raise RuntimeError("database gone")


def test_a_stream_is_freed_however_it_ends(app, redis):
    uid = _user_id()
    # Its last event.
    stream = open_streams.CountedStream(_events_then(), open_streams.admit(uid))
    assert list(stream) == ["one", "two"]
    assert redis.open(uid) == 0
    # An error.
    stream = open_streams.CountedStream(_events_then(fail=True),
                                        open_streams.admit(uid))
    with pytest.raises(RuntimeError):
        list(stream)
    assert redis.open(uid) == 0
    # The client gone part-way: the server closes the response.
    stream = open_streams.CountedStream(_events_then(), open_streams.admit(uid))
    assert next(stream) == "one"
    assert redis.open(uid) == 1
    stream.close()
    assert redis.open(uid) == 0
    # A response never read.
    stream = open_streams.CountedStream(_events_then(), open_streams.admit(uid))
    stream.close()
    assert redis.open(uid) == 0


def test_a_response_closed_part_way_frees_its_place(app, client, redis):
    uid = _user_id()
    node_id = _node("completed", "/media/n/tts.mp3?v=1", chunks=3)
    resp = client.get(f"/api/sse/nodes/{node_id}/tts-stream", buffered=False)
    assert redis.open(uid) == 1
    next(iter(resp.response))
    resp.close()
    assert redis.open(uid) == 0


def test_a_stream_that_stopped_writing_lapses(app, redis):
    uid = _user_id()
    _fill(uid, open_streams.MAX_OPEN_STREAMS)   # a worker stopped with these
    assert open_streams.admit(uid) is None
    redis.now += open_streams.ENTRY_TTL_SECONDS + 1
    assert open_streams.admit(uid) is not None


def test_a_stream_that_writes_moves_its_time_on(app, redis):
    uid = _user_id()

    def heartbeats():
        while True:
            redis.now += open_streams.REFRESH_SECONDS
            yield "heartbeat"

    stream = open_streams.CountedStream(heartbeats(), open_streams.admit(uid))
    for _ in range(20):   # five minutes of heartbeats
        next(stream)
    assert redis.open(uid) == 1
    stream.close()


def test_a_busy_session_is_let_through(app, redis):
    """Control: four tabs and a phone, each in a Voice turn with replies
    being written (about 20 streams), all reconnecting at once."""
    uid = _user_id()
    _fill(uid, 20)
    _fill(uid, 20)
    assert redis.open(uid) == 40


def test_without_redis_every_stream_is_let_through(app, client):
    uid = _user_id()
    slots = [open_streams.admit(uid)
             for _ in range(open_streams.MAX_OPEN_STREAMS + 5)]
    assert all(s is not None for s in slots)
    node_id = _node("completed", "/media/n/tts.mp3?v=1", chunks=1)
    assert client.get(
        f"/api/sse/nodes/{node_id}/tts-stream").status_code == 200
