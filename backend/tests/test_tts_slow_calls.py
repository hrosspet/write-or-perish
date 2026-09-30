"""Slow and cut-off OpenAI TTS calls (#382).

Since 2026-09-27 some gpt-4o-mini-tts calls stream audio far slower than
realtime, and a call that reaches 300 s ends with HTTP 200 and the audio
cut mid-word. ``synthesize_to_file`` must never return such audio as
complete: a slow call is cancelled and resent, a call that ran to the
cutoff is resent, and when every attempt fails the chunk fails.

Imports the real tts module against stub glue (celery / pydub /
backend.celery_app), then restores it, including the ``backend.tasks.tts``
package attribute, so the rest of the suite is unaffected — same pattern
as test_tts_strip.py. openai is the real package: the code under test
catches its exception classes.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import openai
import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

# Imported before the stubs, so it keeps the real pydub for later tests.
import backend.utils.audio_processing  # noqa: E402,F401

# celery.Task must be a real base class so `class TTSTask(Task)` imports.
_celery_stub = MagicMock()
_celery_stub.Task = object

_GLUE = {
    "celery": _celery_stub,
    "celery.utils": MagicMock(),
    "celery.utils.log": MagicMock(),
    "pydub": MagicMock(),
    "backend.celery_app": MagicMock(),
}
import backend.tasks as _tasks_pkg  # noqa: E402

_saved = {k: sys.modules.get(k) for k in _GLUE}
_had_attr = hasattr(_tasks_pkg, "tts")
_saved_attr = getattr(_tasks_pkg, "tts", None)
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
if _had_attr:
    _tasks_pkg.tts = _saved_attr
else:
    delattr(_tasks_pkg, "tts")

SEC = tts.TTS_MP3_BYTES_PER_SEC


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeResponse:
    """A streamed TTS response. *script* is a list of (seconds after the
    call started, seconds of audio delivered then); an exception in place
    of the audio is raised at that time instead."""

    def __init__(self, clock, script, request_id):
        self.headers = {"x-request-id": request_id}
        self._clock = clock
        self._start = clock.t
        self._script = script
        self.delivered = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        for at, audio in self._script:
            self._clock.t = self._start + at
            if isinstance(audio, Exception):
                raise audio
            self.delivered += 1
            yield b"\0" * int(audio * SEC)


class FakeClient:
    """Answers each create() with the next script; a script that is an
    exception is raised by create() itself (no response headers)."""

    def __init__(self, clock, *scripts):
        self._clock = clock
        self._scripts = list(scripts)
        self.calls = []
        self.options = []
        self.responses = []
        self.audio = SimpleNamespace(speech=SimpleNamespace(
            with_streaming_response=SimpleNamespace(create=self._create)))

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        script = self._scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        resp = FakeResponse(self._clock, script, f"req-{len(self.calls)}")
        self.responses.append(resp)
        return resp


_REQ = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")


@pytest.fixture
def env(monkeypatch):
    clock = FakeClock()
    sentry = MagicMock()
    logger = MagicMock()
    monkeypatch.setattr(tts, "_now", clock)
    monkeypatch.setattr(tts, "sentry_sdk", sentry)
    monkeypatch.setattr(tts, "logger", logger)
    monkeypatch.setattr(tts, "AudioSegment", MagicMock())
    return SimpleNamespace(clock=clock, sentry=sentry, logger=logger)


FAST = [(1, 20), (2, 40)]


def test_fast_call_is_kept_and_logged_with_its_request_id(env, tmp_path):
    client = FakeClient(env.clock, FAST)
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Some text.", path)

    assert len(client.calls) == 1
    assert client.calls[0]["timeout"].read == tts.TTS_SLOW_CHECK_SECS
    # One request per attempt: the SDK's own retries would hide inside
    # the attempt's clock.
    assert client.options == [{"max_retries": 0}]
    assert path.stat().st_size == 60 * SEC
    env.sentry.capture_message.assert_not_called()
    assert "req-1" in env.logger.info.call_args.args


def test_normal_long_call_is_not_cancelled(env, tmp_path):
    # ~3x realtime: 240 s of audio in 80 s, well past the 30 s check.
    client = FakeClient(env.clock, [(10, 30), (40, 120), (80, 90)])
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Long text.", path)

    assert len(client.calls) == 1
    assert path.stat().st_size == 240 * SEC


def test_call_slower_than_realtime_is_cancelled_and_resent(env, tmp_path):
    # 3 s of audio after 31 s: cancelled there, never waits for the rest.
    slow = [(10, 1), (31, 2), (300, 100)]
    client = FakeClient(env.clock, slow, FAST)
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Some text.", path)

    assert len(client.calls) == 2
    assert client.responses[0].delivered == 2
    assert client.calls[1]["input"] == "Some text."
    assert path.stat().st_size == 60 * SEC   # only the second attempt
    env.sentry.capture_message.assert_called_once_with(
        "Slow TTS call cancelled", level="warning")


def test_call_that_sends_nothing_is_resent(env, tmp_path):
    silent = [(30, httpx.ReadTimeout("timed out"))]
    client = FakeClient(env.clock, silent, FAST)
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Some text.", path)

    assert len(client.calls) == 2
    assert path.stat().st_size == 60 * SEC


def test_call_without_response_headers_is_resent_and_counted(env, tmp_path):
    # No headers within the read timeout: the SDK raises APITimeoutError
    # from create() (its own retries are off).
    client = FakeClient(env.clock, openai.APITimeoutError(request=_REQ),
                        FAST)
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Some text.", path)

    assert len(client.calls) == 2
    env.sentry.capture_message.assert_called_once_with(
        "Slow TTS call cancelled", level="warning")


def test_server_error_is_resent_but_not_counted_as_slow(env, tmp_path):
    error = openai.InternalServerError(
        "boom", response=httpx.Response(500, request=_REQ), body=None)
    client = FakeClient(env.clock, error, FAST)
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Some text.", path)

    assert len(client.calls) == 2
    env.sentry.capture_message.assert_not_called()


def test_call_that_ran_to_the_cutoff_is_resent(env, tmp_path):
    # Faster than realtime throughout, but it ended at OpenAI's cutoff:
    # the audio is cut and must not be kept.
    cut = [(295, 300)]
    client = FakeClient(env.clock, cut, FAST)
    path = tmp_path / "tts_chunk_0.mp3"

    tts.synthesize_to_file(client, "Some text.", path)

    assert len(client.calls) == 2
    assert path.stat().st_size == 60 * SEC


def test_every_attempt_slow_fails_and_removes_the_partial_file(env, tmp_path):
    slow = [(10, 1), (31, 2)]
    client = FakeClient(env.clock, slow, slow, slow)
    path = tmp_path / "tts_chunk_0.mp3"
    mark = MagicMock()

    with pytest.raises(tts.TTSCallFailed, match="failed after 3 attempts"):
        tts.synthesize_to_file(client, "Some text.", path, mark=mark)

    assert len(client.calls) == 3
    assert not path.exists()
    assert env.sentry.capture_message.call_count == 2
    assert ("tts_last_byte",) not in [c.args for c in mark.call_args_list]
