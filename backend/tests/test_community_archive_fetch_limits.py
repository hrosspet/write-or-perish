"""Community Archive fetches (POST /api/external/community-archive/fetch).

- One fetch of an account is queued or running at a time: a second press
  for the same handle answers as the first did (202, no second job),
  another handle gets 409 "fetch_running" with a message; the job frees
  the account's key when it ends, also when it fails; a key whose job
  stopped expires by itself; a fetch that could not be queued frees it.
  A new press goes through when Celery says the key's job ended, or when
  it started and has not beaten its key for three beats.
- An account starts at most CA_FETCHES_PER_DAY fetches a day (429
  "too_many_fetches"), counted again the next day.
- max_items is a whole number from 1 up; above CA_FETCH_MAX_ITEMS it is
  cut to it; anything else gets 400 (it was a 500).
- Without Redis every fetch is queued, as before (control).
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

from backend.tests.test_external_content import (  # noqa: F401
    app, client, User, _db,
)
from backend.tests.fake_redis import install
from backend.routes import external
from backend.utils import run_locks

URL = "/api/external/community-archive/fetch"


@pytest.fixture
def task(monkeypatch):
    mod = types.ModuleType("backend.tasks.external_sync")
    mod.fetch_community_archive = MagicMock()
    monkeypatch.setitem(sys.modules, "backend.tasks.external_sync", mod)
    return mod.fetch_community_archive


@pytest.fixture
def redis(monkeypatch):
    return install(monkeypatch)


def _uid():
    return User.query.first().id


def _fetch(client, username="alice", **extra):
    return client.post(URL, json={"username": username, **extra})


def test_second_press_for_the_same_handle_is_not_queued_again(
        app, client, task, redis):
    first = _fetch(client, "@Alice")
    second = _fetch(client, "alice")
    assert first.status_code == 202 and second.status_code == 202
    assert second.get_json()["already_running"] is True
    assert second.get_json()["task_id"] == first.get_json()["task_id"]
    assert task.apply_async.call_count == 1


def test_another_handle_while_one_is_fetched_is_refused(
        app, client, task, redis):
    _fetch(client, "alice")
    resp = _fetch(client, "bob")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "fetch_running"
    assert "@alice" in resp.get_json()["error"]
    assert task.apply_async.call_count == 1


def _run_job(monkeypatch, app, task_id, user_id, username, fail=False):
    """The fetch job itself, with the archive answered here."""
    saved = {k: sys.modules.get(k)
             for k in ("backend.celery_app", "backend.tasks.external_sync")}
    sys.modules["backend.celery_app"] = MagicMock()
    sys.modules.pop("backend.tasks.external_sync", None)
    try:
        import backend.tasks.external_sync as sync
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    monkeypatch.setattr(sync, "flask_app", app)

    def lookup(name):
        if fail:
            raise RuntimeError("archive down")
        return None   # not in the archive

    monkeypatch.setattr(sync, "ca_lookup_account", lookup)
    return sync.fetch_community_archive_impl(task_id, user_id, username, 10)


def test_the_job_frees_the_key_when_it_ends(app, client, task, redis,
                                            monkeypatch):
    uid = _uid()
    task_id = _fetch(client, "alice").get_json()["task_id"]
    key = external.CA_FETCH_LOCK_KEY.format(user_id=uid)
    assert redis.get(key) is not None
    assert _run_job(monkeypatch, app, task_id, uid, "alice")["status"] \
        == "not_found"
    assert redis.get(key) is None
    task_id = _fetch(client, "bob").get_json()["task_id"]
    with pytest.raises(RuntimeError):
        _run_job(monkeypatch, app, task_id, uid, "bob", fail=True)
    assert redis.get(key) is None
    assert _fetch(client, "carol").status_code == 202


def test_another_jobs_end_does_not_free_the_key(app, client, task, redis):
    uid = _uid()
    _fetch(client, "alice")
    external.release_ca_fetch(uid, "some-older-task", "alice")
    assert _fetch(client, "bob").status_code == 409


def test_a_key_whose_job_stopped_expires(app, client, task, redis):
    _fetch(client, "alice")
    assert _fetch(client, "bob").status_code == 409
    redis.advance(external.CA_FETCH_QUEUED_TTL + 1)
    assert _fetch(client, "bob").status_code == 202


def test_a_fetch_that_could_not_be_queued_frees_the_key(
        app, client, task, redis):
    task.apply_async.side_effect = RuntimeError("broker down")
    assert _fetch(client, "alice").status_code == 500
    task.apply_async.side_effect = None
    assert _fetch(client, "bob").status_code == 202


def test_fetches_per_day_are_limited(app, client, task, redis):
    uid = _uid()
    key = external.CA_FETCH_LOCK_KEY.format(user_id=uid)
    for i in range(external.CA_FETCHES_PER_DAY):
        assert _fetch(client, f"h{i}").status_code == 202
        redis.data.pop(key)   # that job ended
    resp = _fetch(client, "one-more")
    assert resp.status_code == 429
    assert resp.get_json()["code"] == "too_many_fetches"
    redis.advance(24 * 3600 + 1)
    assert _fetch(client, "one-more").status_code == 202


@pytest.mark.parametrize("bad", [
    "abc", None, [], {}, "1e9", 1.5, -5, 0, True, "500"])
def test_max_items_that_is_not_a_whole_number_from_1_is_refused(
        app, client, task, bad):
    resp = _fetch(client, "alice", max_items=bad)
    assert resp.status_code == 400
    assert "max_items" in resp.get_json()["error"]
    task.apply_async.assert_not_called()


def test_max_items_is_cut_to_the_most_and_defaults_as_before(
        app, client, task):
    _fetch(client, "alice", max_items=50_000)
    assert task.apply_async.call_args.kwargs["kwargs"] == {
        "max_items": external.CA_FETCH_MAX_ITEMS}
    _fetch(client, "bob")
    assert task.apply_async.call_args.kwargs["kwargs"] == {
        "max_items": external.CA_FETCH_DEFAULT_ITEMS}


def test_without_redis_every_fetch_is_queued(app, client, task):
    for name in ("alice", "bob", "alice"):
        assert _fetch(client, name).status_code == 202
    assert task.apply_async.call_count == 3


# ── A key its job left behind ────────────────────────────────────────────

@pytest.mark.parametrize("state", ["SUCCESS", "FAILURE", "REVOKED"])
def test_a_key_whose_job_ended_lets_the_next_fetch_through(
        app, client, task, redis, monkeypatch, state):
    first = _fetch(client, "alice").get_json()["task_id"]
    monkeypatch.setattr(run_locks, "task_state",
                        lambda tid: state if tid == first else None)
    again = _fetch(client, "alice")
    assert again.status_code == 202
    assert "already_running" not in again.get_json()
    assert task.apply_async.call_count == 2


def test_a_key_whose_job_stopped_beating_lets_the_next_fetch_through(
        app, client, task, redis, monkeypatch):
    uid = _uid()
    first = _fetch(client, "alice").get_json()["task_id"]
    external.ca_fetch_key(uid, first, "alice").beat()   # the job started
    monkeypatch.setattr(run_locks, "task_state", lambda tid: "STARTED")
    redis.advance(2 * external.CA_FETCH_BEAT_SECONDS)
    assert _fetch(client, "bob").status_code == 409     # could still beat
    redis.advance(2 * external.CA_FETCH_BEAT_SECONDS)
    assert _fetch(client, "bob").status_code == 202


def test_a_fetch_that_beats_keeps_the_account(app, client, task, redis,
                                              monkeypatch):
    uid = _uid()
    first = _fetch(client, "alice").get_json()["task_id"]
    job = external.ca_fetch_key(uid, first, "alice")
    monkeypatch.setattr(run_locks, "task_state", lambda tid: "STARTED")
    for _ in range(10):
        redis.advance(external.CA_FETCH_BEAT_SECONDS)
        job.beat()
        assert _fetch(client, "bob").status_code == 409
    assert external.CA_FETCH_RUNNING_TTL < external.CA_FETCH_QUEUED_TTL
