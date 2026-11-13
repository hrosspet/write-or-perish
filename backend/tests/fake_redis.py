"""A Redis stand-in for the run locks (backend/utils/run_locks.py): SET
with NX/EX, GET, INCR, and EVAL of the two lock scripts, on a clock the
test moves (``advance``), so a TTL can run out."""
from backend.utils import run_locks


class FakeRedis:
    def __init__(self):
        self.now = 0.0
        self.data = {}   # key -> (value bytes, expires at or None)

    def advance(self, seconds):
        self.now += seconds

    def _live(self, key):
        entry = self.data.get(key)
        if entry is not None and entry[1] is not None and entry[1] <= self.now:
            del self.data[key]
            return None
        return entry

    def set(self, key, value, nx=False, ex=None):
        if nx and self._live(key) is not None:
            return None
        self.data[key] = (str(value).encode(),
                          self.now + ex if ex else None)
        return True

    def get(self, key):
        entry = self._live(key)
        return entry[0] if entry is not None else None

    def ttl(self, key):
        entry = self._live(key)
        return None if entry is None else entry[1] - self.now

    def incr(self, key):
        """INCR: a missing key counts from 0; the key keeps its expiry."""
        entry = self._live(key)
        value = int(entry[0]) + 1 if entry is not None else 1
        self.data[key] = (str(value).encode(),
                          entry[1] if entry is not None else None)
        return value

    def eval(self, script, numkeys, key, *args):
        token = str(args[0]).encode()
        entry = self._live(key)
        if script == run_locks.BEAT_SCRIPT:
            ttl = int(args[1])
            if entry is not None and entry[0] == token:
                self.data[key] = (token, self.now + ttl)
                return 1
            if entry is None:
                self.data[key] = (token, self.now + ttl)
                return 2
            return 0
        if script == run_locks.RELEASE_SCRIPT:
            if entry is not None and entry[0] == token:
                del self.data[key]
                return 1
            return 0
        raise AssertionError("unknown script")


def install(monkeypatch):
    """Route run_locks to a new FakeRedis; returns it."""
    fake = FakeRedis()
    monkeypatch.setattr(run_locks, "_client", lambda: fake)
    return fake
