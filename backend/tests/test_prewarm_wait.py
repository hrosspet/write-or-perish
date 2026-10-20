"""A voice reply waits for its cache pre-warm (#187; Peter, 2026-10-09).

A cache entry exists only once the finalize pre-warm's request has been
processed. A reply whose first Anthropic call starts earlier reads
nothing from the cache and writes the whole prompt again. So:

- the pre-warm signals that it finished on every exit (wrote the cache,
  failed, skipped);
- the reply's first model call waits for that signal, at most
  PREWARM_WAIT_MAX_SECONDS (10 s), and not at all when the signal is
  already there; tool-round calls never wait;
- only a reply that had a pre-warm sent for it waits (finalize passes it
  a token); every other reply starts as before;
- without Redis nothing waits, and the wait never fails the reply.

Time is faked (prompt_cache.time), so no test sleeps.
"""
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _build_chain,
    _mk_artifact, _resp, _fresh, _ScriptedProvider,
)
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixture)
    st, _user, _node, _reply, _prompt_root, _voice_draft,
)
from backend.extensions import db as _db
from backend.models import Draft
import backend.utils.prompt_cache as prompt_cache

CFG = {}  # no PREWARM_WAIT_MAX_SECONDS: the 10 s default


class _FakeRedis:
    def __init__(self, down=False):
        self.store = {}
        self.ttls = {}
        self.down = down

    def get(self, key):
        if self.down:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.down:
            raise ConnectionError("redis is down")
        self.store[key] = value.encode() if isinstance(value, str) else value
        self.ttls[key] = ttl


class _Clock:
    """Stands in for the time module inside prompt_cache: sleep()
    advances monotonic() and runs what was scheduled with at()."""

    def __init__(self):
        self.now = 0.0
        self.slept = []
        self._events = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs
        for event in list(self._events):
            if self.now >= event[0]:
                self._events.remove(event)
                event[1]()

    def at(self, secs, fn):
        self._events.append((self.now + secs, fn))


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(prompt_cache, "time", fake)
    return fake


def _wait_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("prewarm-wait")]


# ── The wait ─────────────────────────────────────────────────────────────

def test_the_reply_waits_until_the_signal_and_no_longer(redis, clock, caplog):
    caplog.set_level(logging.INFO)
    token = prompt_cache.new_prewarm_token()
    clock.at(0.9, lambda: prompt_cache.mark_prewarm_done(
        CFG, token, {"status": "ok"}))

    waited = prompt_cache.wait_for_prewarm(CFG, token, 7)

    # Stopped at the first check after the signal: within one poll.
    assert 0.9 <= waited <= 0.9 + prompt_cache.PREWARM_WAIT_POLL_SECONDS
    assert max(clock.slept) <= prompt_cache.PREWARM_WAIT_POLL_SECONDS
    assert _wait_lines(caplog) == [
        "prewarm-wait node=7 waited=1.0s finished=yes prewarm=ok"]


def test_a_finished_warm_means_no_wait(redis, clock, caplog):
    caplog.set_level(logging.INFO)
    token = prompt_cache.new_prewarm_token()
    prompt_cache.mark_prewarm_done(
        CFG, token, {"status": "skipped", "reason": "no_key"})

    assert prompt_cache.wait_for_prewarm(CFG, token, 7) == 0
    assert clock.slept == []
    assert _wait_lines(caplog) == [
        "prewarm-wait node=7 waited=0.0s finished=yes "
        "prewarm=skipped:no_key"]


def test_the_wait_stops_at_the_cap(redis, clock, caplog):
    caplog.set_level(logging.INFO)

    waited = prompt_cache.wait_for_prewarm(CFG, "never-signalled", 7)

    assert waited == pytest.approx(10.0)
    assert sum(clock.slept) == pytest.approx(10.0)
    assert len(_wait_lines(caplog)) == 1
    assert "finished=no" in _wait_lines(caplog)[0]


def test_the_cap_is_a_setting(redis, clock):
    assert prompt_cache.wait_for_prewarm(
        {"PREWARM_WAIT_MAX_SECONDS": 3.0}, "x", 7) == pytest.approx(3.0)
    clock.slept.clear()
    # 0 turns the wait off.
    assert prompt_cache.wait_for_prewarm(
        {"PREWARM_WAIT_MAX_SECONDS": 0.0}, "x", 7) == 0
    assert clock.slept == []


def test_no_token_means_no_wait_and_no_redis(monkeypatch, clock):
    def _no_redis(config):
        raise AssertionError("Redis was asked")
    monkeypatch.setattr(prompt_cache, "_client", _no_redis)

    assert prompt_cache.wait_for_prewarm(CFG, None, 7) == 0
    prompt_cache.mark_prewarm_done(CFG, None, {"status": "ok"})
    assert clock.slept == []


def test_redis_down_means_no_wait(monkeypatch, clock, caplog):
    caplog.set_level(logging.INFO)
    down = _FakeRedis(down=True)
    monkeypatch.setattr(prompt_cache, "_client", lambda config: down)

    assert prompt_cache.wait_for_prewarm(CFG, "tok", 7) == 0
    assert clock.slept == []
    assert "finished=unknown" in _wait_lines(caplog)[0]
    # The pre-warm side does not raise either.
    prompt_cache.mark_prewarm_done(CFG, "tok", {"status": "ok"})


# ── The reply (generate_llm_response) ────────────────────────────────────

def _voice_turn(alice_chain, token, responses):
    alice, system, user_node, llm_node = alice_chain
    _ScriptedProvider.reset(responses)
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="voice",
                          prewarm_token=token)
    return llm_node


def test_the_first_call_waits_for_the_warm(app, redis, clock, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    chain = _build_chain("voice")
    token = prompt_cache.new_prewarm_token()
    calls_at_signal = []

    def _warm_finishes():
        calls_at_signal.append(len(_ScriptedProvider.calls))
        prompt_cache.mark_prewarm_done(app.config, token, {"status": "ok"})
    clock.at(1.9, _warm_finishes)

    llm_node = _voice_turn(chain, token, [_resp("Hello.")])

    assert calls_at_signal == [0]          # no call before the warm ended
    assert len(_ScriptedProvider.calls) == 1
    assert 1.9 <= sum(clock.slept) <= (
        1.9 + prompt_cache.PREWARM_WAIT_POLL_SECONDS)
    assert _fresh(llm_node.id).llm_task_status == "completed"
    assert len(_wait_lines(caplog)) == 1


def test_tool_rounds_do_not_wait(app, redis, clock, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    chain = _build_chain("voice")
    _mk_artifact(chain[0].id, "reading-list", "1. Dune", title="Reading List")
    _db.session.commit()

    llm_node = _voice_turn(chain, "never-signalled", [
        _resp("Let me look.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"}}]),
        _resp("Start with Dune."),
    ])

    assert len(_ScriptedProvider.calls) == 2
    # One wait, to the cap, before the first call only.
    assert sum(clock.slept) == pytest.approx(10.0)
    assert len(_wait_lines(caplog)) == 1
    assert _fresh(llm_node.id).llm_task_status == "completed"


def test_a_reply_without_a_token_does_not_wait(app, redis, clock, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    llm_node = _voice_turn(_build_chain("voice"), None, [_resp("Hello.")])

    assert clock.slept == []
    assert _wait_lines(caplog) == []
    assert _fresh(llm_node.id).llm_task_status == "completed"


def test_redis_down_the_reply_goes_ahead(app, monkeypatch, clock):  # noqa: F811
    down = _FakeRedis(down=True)
    monkeypatch.setattr(prompt_cache, "_client", lambda config: down)

    llm_node = _voice_turn(_build_chain("voice"), "tok", [_resp("Hello.")])

    assert clock.slept == []
    assert len(_ScriptedProvider.calls) == 1
    assert _fresh(llm_node.id).llm_task_status == "completed"


# ── The pre-warm signals on every exit ───────────────────────────────────

@pytest.fixture
def anthropic_model(app, monkeypatch):  # noqa: F811
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY_CHAT", "ant-chat")
    monkeypatch.setitem(app.config, "SUPPORTED_MODELS", {
        **app.config["SUPPORTED_MODELS"],
        "claude-test": {"provider": "anthropic", "api_model": "claude-x"},
    })


def _anthropic_answers(monkeypatch, answer):
    def _call_anthropic(api_model, messages, api_key, **kw):
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(_ScriptedProvider, "_call_anthropic",
                        staticmethod(_call_anthropic), raising=False)


@pytest.mark.parametrize("case, expected", [
    ("wrote", b"ok"),
    ("failed", b"failed"),
    ("skipped", b"skipped:not_anthropic"),
    ("no_node", b"skipped:no_system_node"),
])
def test_the_warm_signals_on_every_exit(app, redis, anthropic_model, monkeypatch, case, expected):  # noqa: F811
    alice, system, _, _ = _build_chain("voice")
    _anthropic_answers(monkeypatch, RuntimeError("provider down")
                       if case == "failed" else
                       {"content": "", "input_tokens": 3, "output_tokens": 1,
                        "cache_creation_input_tokens": 1000})
    model = "gpt-5" if case == "skipped" else "claude-test"
    node_id = system.id + 999 if case == "no_node" else system.id
    token = prompt_cache.new_prewarm_token()

    result = _llm_task_mod.prewarm_anthropic_cache(
        node_id, alice.id, model, done_token=token)

    key = prompt_cache._PREWARM_DONE_PREFIX + token
    assert redis.store[key] == expected
    assert redis.ttls[key] == prompt_cache.PREWARM_DONE_TTL_SECONDS
    assert result["status"] == expected.decode().split(":")[0]


def test_the_warm_signals_when_it_raises(app, redis, monkeypatch):  # noqa: F811
    def _boom(*a, **kw):
        raise KeyboardInterrupt  # past the body's own except Exception
    monkeypatch.setattr(_llm_task_mod, "_prewarm_anthropic_cache", _boom)
    token = prompt_cache.new_prewarm_token()

    with pytest.raises(KeyboardInterrupt):
        _llm_task_mod.prewarm_anthropic_cache(1, 1, "claude-test",
                                              done_token=token)

    assert redis.store[prompt_cache._PREWARM_DONE_PREFIX + token] == b"failed"


def test_a_warm_without_a_token_writes_no_signal(app, redis, anthropic_model, monkeypatch):  # noqa: F811
    alice, system, _, _ = _build_chain("voice")
    _anthropic_answers(monkeypatch, {"content": "", "input_tokens": 3,
                                     "output_tokens": 1})

    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")

    assert not [k for k in redis.store
                if k.startswith(prompt_cache._PREWARM_DONE_PREFIX)]


# ── Finalize hands the same token to the warm and to the reply ───────────

def _finalize(st, user, session_id, model, parent=None):  # noqa: F811
    return st.finalize_draft_streaming(
        MagicMock(), session_id, 1, label="Voice", user_id=user.id,
        parent_id=parent.id if parent else None, model=model)


def _token_sent(st):  # noqa: F811
    return st._fake_llm.generate_llm_response.si.call_args.kwargs[
        "prewarm_token"]


def test_a_fresh_thread_reply_waits_for_its_warm(st):  # noqa: F811
    user = _user()
    draft = _voice_draft(user, "sess-fresh", "chat", text="spoken words")
    draft.set_content("x" * 600)  # transcript so far: enough to warm
    _db.session.commit()

    _finalize(st, user, "sess-fresh", "claude-test")

    warm = st._fake_llm.prewarm_anthropic_cache.delay
    warm.assert_called_once()
    assert warm.call_args.kwargs["done_token"]
    assert _token_sent(st) == warm.call_args.kwargs["done_token"]


def test_a_long_recording_in_a_thread_waits_for_its_warm(st):  # noqa: F811
    user = _user()
    reply = _reply(_node(user, _prompt_root(user, "voice")), user)
    draft = _voice_draft(user, "sess-long", "chat", text="more words",
                         parent=reply)
    draft.created_at = datetime.utcnow() - timedelta(
        seconds=st.PREWARM_ONGOING_MIN_SECONDS + 60)
    _db.session.commit()

    _finalize(st, user, "sess-long", "claude-test", parent=reply)

    warm = st._fake_llm.prewarm_anthropic_cache.delay
    warm.assert_called_once()
    assert _token_sent(st) == warm.call_args.kwargs["done_token"]


@pytest.mark.parametrize("model, transcript_so_far", [
    ("gpt-5", "x" * 600),        # not Anthropic: no warm
    ("claude-test", "x" * 100),  # under 500 characters: no warm
])
def test_a_reply_without_a_warm_does_not_wait(st, model, transcript_so_far):  # noqa: F811
    user = _user()
    draft = _voice_draft(user, "sess-nowarm", "chat", text="spoken words")
    draft.set_content(transcript_so_far)
    _db.session.commit()

    _finalize(st, user, "sess-nowarm", model)

    st._fake_llm.prewarm_anthropic_cache.delay.assert_not_called()
    assert Draft.query.filter_by(session_id="sess-nowarm").one().llm_node_id
    assert _token_sent(st) is None
