"""A TTS call that stalls is dropped and sent once more (#367): OpenAI's
TTS occasionally trickles a short clip out over minutes, which held up a
voice reply's audio for as long."""
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

_celery_stub = MagicMock()
_celery_stub.Task = object
_GLUE = {
    "celery": _celery_stub,
    "celery.utils": MagicMock(),
    "celery.utils.log": MagicMock(),
    "pydub": MagicMock(),
    "backend.celery_app": MagicMock(),
}
_saved = {k: sys.modules.get(k) for k in _GLUE}
for _k, _v in _GLUE.items():
    sys.modules[_k] = _v
sys.modules.pop("backend.tasks.tts", None)
import backend.tasks.tts as tts  # noqa: E402
for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
sys.modules.pop("backend.tasks.tts", None)


class _Clock:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now


def _client(scripts, calls):
    """Each speech call replays a script of (seconds, bytes) blocks."""
    class _Resp:
        def __init__(self, blocks):
            self.blocks = blocks

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            for seconds, data in self.blocks:
                _Clock.now += seconds
                yield data

    class _Speech:
        class with_streaming_response:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return _Resp(scripts.pop(0))

    class _Client:
        def with_options(self, **options):
            calls.append(options)
            return self

        class audio:
            speech = _Speech

    return _Client()


@pytest.fixture
def fake(monkeypatch):
    _Clock.now = 0.0
    monkeypatch.setattr(tts.time, "monotonic", _Clock.monotonic)
    monkeypatch.setattr(tts, "AudioSegment", MagicMock())


def test_a_stalled_call_is_sent_again(fake, tmp_path):
    calls = []
    trickle = [(10.0, b"a")] * 5            # 50 s for a few bytes
    fast = [(0.5, b"mp3"), (0.5, b"data")]
    tts.synthesize_to_file(_client([trickle, fast], calls), "Hello there.",
                           tmp_path / "c.mp3")
    creates = [c for c in calls if "input" in c]
    assert len(creates) == 2
    assert (tmp_path / "c.mp3").read_bytes() == b"mp3data"
    # The SDK's own retries are off: the deadline is ours.
    options = [c for c in calls if "max_retries" in c]
    assert options[0]["max_retries"] == 0
    assert options[0]["timeout"] == pytest.approx(20.0 + 0.02 * 12)


def test_it_gives_up_after_the_second_stall(fake, tmp_path):
    trickle = [(10.0, b"a")] * 5
    with pytest.raises(tts.TTSStallError):
        tts.synthesize_to_file(_client([list(trickle), list(trickle)], []),
                               "Hello there.", tmp_path / "c.mp3")


def test_a_normal_call_is_sent_once(fake, tmp_path):
    calls = []
    tts.synthesize_to_file(_client([[(1.0, b"x")]], calls), "Hi.",
                           tmp_path / "c.mp3")
    assert len([c for c in calls if "input" in c]) == 1
