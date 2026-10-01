"""Voice turn timing (#371 step 0): marks, the joined record, medians."""
from types import SimpleNamespace

import pytest

from backend.utils import voice_timing


class FakeRedis:
    """The few Redis commands voice_timing uses, in memory."""

    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.ttls = {}

    def pipeline(self, transaction=True):
        return self   # commands apply at once; execute() is a no-op

    def execute(self):
        return []

    def hsetnx(self, key, field, value):
        self.hashes.setdefault(key, {}).setdefault(field, value)

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def _by_score(self, key):
        return sorted(self.zsets.get(key, {}), key=self.zsets[key].get)

    def zremrangebyrank(self, key, start, stop):
        members = self._by_score(key) if key in self.zsets else []
        stop = stop if stop >= 0 else len(members) + stop
        if stop < 0:
            return
        for member in members[start:stop + 1]:
            del self.zsets[key][member]

    def zrevrangebyscore(self, key, high, low, start=0, num=None):
        scores = self.zsets.get(key, {})
        high = float("inf") if high == "+inf" else float(high)
        members = [m for m in reversed(self._by_score(key))
                   if float(low) <= scores[m] <= high] \
            if key in self.zsets else []
        return members[start:None if num is None else start + num]

    def zrevrange(self, key, start, stop):
        members = list(reversed(self._by_score(key))) \
            if key in self.zsets else []
        return members[start:stop + 1]


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(voice_timing, "_redis", lambda: fake)
    return fake


def test_first_mark_of_a_stage_counts(fake_redis):
    voice_timing.mark(7, "first_text", t=10.0)
    voice_timing.mark(7, "first_text", t=12.0)
    voice_timing.mark(7, "chunk_cut", t=13.5, first_chunk_chars=80)
    rec = voice_timing.record(7)
    assert rec["marks"] == {"first_text": 10.0, "chunk_cut": 13.5}
    assert rec["facts"] == {"first_chunk_chars": "80"}
    assert rec["stages"] == {"c": 3.5}
    assert fake_redis.ttls["voice_timing:7"] == voice_timing.TTL_SECONDS


def test_stages_add_up_to_the_total():
    marks = {name: float(i) for i, name in enumerate([
        "rec_stop", "finalize_start", "transcribed", "llm_task_start",
        "llm_request", "first_text", "chunk_cut", "tts_first_byte",
        "tts_last_byte", "chunk_published", "chunk_ready", "playing"])}
    stages = voice_timing.stage_seconds(marks)
    parts = ["a1", "a2", "a3", "b1", "b2", "c", "d", "e", "f", "g", "h"]
    assert sum(stages[p] for p in parts) == stages["total"] == 11.0
    assert stages["a"] == stages["a1"] + stages["a2"] + stages["a3"]
    assert stages["b"] == stages["b1"] + stages["b2"]


def test_a_tapped_turn_has_no_h_or_total():
    """iOS never autoplays: the wait for the tap is the user's, not the
    pipeline's (#371)."""
    marks = {"rec_stop": 0.0, "chunk_ready": 12.0, "play_pressed": 20.0,
             "playing": 20.3}
    stages = voice_timing.stage_seconds(marks)
    assert "h" not in stages and "total" not in stages
    assert stages["ready"] == 12.0
    assert stages["tap"] == 0.3
    # Autoplay blocked, then the lock screen's own play button.
    stages = voice_timing.stage_seconds(
        {"rec_stop": 0.0, "chunk_ready": 12.0, "autoplay_blocked": 12.1,
         "playing": 30.0})
    assert "h" not in stages and "total" not in stages and "tap" not in stages
    # Autoplay: all of it counts.
    stages = voice_timing.stage_seconds(
        {"rec_stop": 0.0, "chunk_ready": 12.0, "playing": 12.3})
    assert stages["h"] == 0.3 and stages["total"] == 12.3


def test_medians_skip_missing_stages():
    records = [{"stages": {"c": 1.0, "d": 4.0}},
               {"stages": {"c": 3.0}},
               {"stages": {"c": 2.0, "d": 2.0}}]
    assert voice_timing.medians(records) == {"c": 2.0, "d": 3.0}


def test_recent_turns_newest_first_and_bounded(fake_redis, monkeypatch):
    monkeypatch.setattr(voice_timing, "RECENT_TURNS", 3)
    clock = iter(range(100, 200))
    monkeypatch.setattr(voice_timing, "time",
                        SimpleNamespace(time=lambda: next(clock)))
    for node_id in (1, 2, 3, 4):
        voice_timing.remember_turn(5, node_id)
    assert voice_timing.recent_turns(5) == [4, 3, 2]
    assert voice_timing.recent_turns(6) == []


def test_no_redis_is_silent(monkeypatch):
    monkeypatch.setattr(voice_timing, "_redis", lambda: None)
    voice_timing.mark(7, "first_text")
    assert voice_timing.record(7)["marks"] == {}
