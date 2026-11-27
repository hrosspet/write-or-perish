"""One X archive import per account (POST /api/import/twitter/confirm).

- A confirm queues the job under its own task id and takes the account's
  key (twitter_archive.IMPORT_LOCK_KEY). While it is held, a confirm of
  the same archive answers 202 with that job's id ("already_running")
  and queues nothing; one of another archive gets 409 "import_running"
  (the text the apps show for the other importers).
- The deleted-content question (409) still comes before the key.
- The job holds the key with the running TTL, refreshes it from a thread
  however long a step takes, and frees it when it ends, also when it
  fails; a confirm whose job could not be queued frees it at once.
- A key its job left behind: a confirm goes through when Celery says the
  job ended, or when the job started and has not beaten for three beats;
  a job that beats keeps the account; a job Celery cannot tell about
  (queued, or lost) holds it until IMPORT_QUEUED_TTL.
- A new analyze keeps one waiting archive per account, and the archive a
  queued import reads.
- The job stores one data key per committed batch of tweets, not one per
  tweet.
"""

from datetime import datetime

import pytest

from backend.tests.test_twitter_import import (  # noqa: F401
    app, _make_user, _login, _fake_task_module, _tweet, _tweets_js,
    _zip_bytes, ta, _db, Node,
)
from backend.tests.fake_redis import install
from backend.utils import encryption, run_locks


def _archive(start, n):
    return _zip_bytes(_tweets_js(
        [_tweet(start + i, f"tweet number {start + i}") for i in range(n)]))


def _client(app, name="alice"):
    from flask import g
    g.pop("_login_user", None)
    client = app.test_client()
    user = _make_user(name)
    _db.session.commit()
    _login(client, user.id)
    return client, user


def _analyze(client, start=1, n=3):
    resp = client.post("/api/import/twitter/analyze",
                       data={"zip_file": (_archive(start, n), "a.zip")},
                       content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["import_token"]


def _confirm(client, token, **extra):
    return client.post("/api/import/twitter/confirm", json={
        "import_token": token, "privacy_level": "private",
        "ai_usage": "none", **extra})


@pytest.fixture
def redis(monkeypatch):
    return install(monkeypatch)


def test_same_archive_confirmed_twice_is_queued_once(app, monkeypatch, redis):
    client, user = _client(app)
    task = _fake_task_module(monkeypatch)
    token = _analyze(client)
    first = _confirm(client, token)
    second = _confirm(client, token)
    assert first.status_code == 202 and second.status_code == 202
    assert task.apply_async.call_count == 1
    assert second.get_json()["already_running"] is True
    assert second.get_json()["task_id"] == first.get_json()["task_id"]
    assert second.get_json()["total"] == 3


def test_another_archive_while_one_is_queued_is_refused(
        app, monkeypatch, redis):
    from backend.routes.import_data import IMPORT_RUNNING_MESSAGE
    client, user = _client(app)
    task = _fake_task_module(monkeypatch)
    first = _confirm(client, _analyze(client, 1)).get_json()
    other = _analyze(client, 1000)
    resp = _confirm(client, other)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "import_running"
    assert resp.get_json()["error"] == IMPORT_RUNNING_MESSAGE
    assert resp.get_json()["task_id"] == first["task_id"]
    assert task.apply_async.call_count == 1
    # Another account is not held up.
    someone, _ = _client(app, "bob")
    assert _confirm(someone, _analyze(someone, 5000)).status_code == 202


def test_deleted_content_question_comes_before_the_key(
        app, monkeypatch, redis):
    client, user = _client(app)
    task = _fake_task_module(monkeypatch)
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             source_key="twitter:1", deleted_at=datetime.utcnow())
    n.set_content("gone")
    _db.session.add(n)
    _db.session.commit()
    token = _analyze(client)
    assert _confirm(client, token).status_code == 409
    assert ta.running_import(user.id) is None
    resp = _confirm(client, token, on_deleted="skip")
    assert resp.status_code == 202
    assert task.apply_async.call_count == 1


def test_a_confirm_whose_job_was_not_queued_frees_the_key(
        app, monkeypatch, redis):
    client, user = _client(app)
    task = _fake_task_module(monkeypatch)
    task.apply_async.side_effect = RuntimeError("broker down")
    resp = _confirm(client, _analyze(client))
    assert resp.status_code == 500
    assert ta.running_import(user.id) is None


def test_the_job_holds_the_key_and_frees_it_when_it_ends(
        app, monkeypatch, redis):
    from backend.tasks import imports as imports_mod
    monkeypatch.setattr(imports_mod, "flask_app", app)
    client, user = _client(app)
    _fake_task_module(monkeypatch)
    token = _analyze(client, 1, 7)
    task_id = _confirm(client, token).get_json()["task_id"]
    key = ta.import_lock_key(user.id)
    assert redis.ttl(key) == ta.IMPORT_QUEUED_TTL
    ttls = []
    monkeypatch.setattr(imports_mod, "BATCH_SIZE", 2)

    def update_state(**meta):
        ttls.append(redis.ttl(key))

    result = imports_mod.import_twitter_archive_impl(
        update_state, user.id, token,
        {"import_type": "separate_nodes", "include_replies": True,
         "privacy_level": "private", "ai_usage": "none", "on_deleted": None},
        task_id=task_id)
    assert result["created"] == 7
    # Refreshed with the running TTL at the start and after every batch.
    assert len(ttls) >= 4 and set(ttls) == {ta.IMPORT_RUNNING_TTL}
    assert ta.running_import(user.id) is None
    # A failed job frees it too.
    token = _analyze(client, 100, 3)
    task_id = _confirm(client, token).get_json()["task_id"]
    ta.stash_delete(ta.stash_path(user.id, token))
    with pytest.raises(Exception):
        imports_mod.import_twitter_archive_impl(
            lambda **kw: None, user.id, token, {}, task_id=task_id)
    assert ta.running_import(user.id) is None


def test_a_key_whose_job_stopped_expires(app, monkeypatch, redis):
    client, user = _client(app)
    _fake_task_module(monkeypatch)
    _confirm(client, _analyze(client, 1))
    other = _analyze(client, 1000)
    assert _confirm(client, other).status_code == 409
    redis.advance(ta.IMPORT_QUEUED_TTL + 1)
    assert _confirm(client, other).status_code == 202


def test_a_new_analyze_keeps_one_waiting_archive_and_the_queued_one(
        app, monkeypatch, redis):
    client, user = _client(app)
    _fake_task_module(monkeypatch)
    queued = _analyze(client, 1)
    assert _confirm(client, queued).status_code == 202
    for k in range(5):
        latest = _analyze(client, 1000 * (k + 1))
    left = {p.name[:-len(".jsonl")]
            for p in (ta.STASH_ROOT / str(user.id)).glob("*.jsonl")}
    assert left == {queued, latest}


def test_without_redis_every_analyzed_archive_stays(app, monkeypatch):
    """When the key cannot be read, nothing is removed (the sweep removes
    stashes after a day)."""
    client, user = _client(app)
    tokens = {_analyze(client, 1000 * k) for k in range(3)}
    left = {p.name[:-len(".jsonl")]
            for p in (ta.STASH_ROOT / str(user.id)).glob("*.jsonl")}
    assert left == tokens


def _rows(n):
    rows, _ = ta.analyze_archive(_archive(1, n))
    return rows


@pytest.fixture
def kms(monkeypatch):
    wraps = []
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek",
                        lambda dek: wraps.append(1) or b"wrapped:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek",
                        lambda w: w[len(b"wrapped:"):])
    return wraps


@pytest.mark.parametrize("batch,keys", [(500, 1), (5, 4)])
def test_one_data_key_per_committed_batch(app, kms, batch, keys):
    from backend.routes.import_data import create_twitter_nodes
    user = _make_user("alice")
    _db.session.commit()
    rows = _rows(20)
    result = create_twitter_nodes(
        user_id=user.id, rows=iter(rows), total=20,
        import_type="separate_nodes", include_replies=True,
        privacy_level="private", ai_usage="none", on_deleted=None,
        provenance="archive_upload", batch_size=batch)
    assert result["created"] == 20
    assert len(kms) == keys
    stored = Node.query.filter_by(human_owner_id=user.id).all()
    assert all(n.content.startswith("ENC:") for n in stored)
    assert sorted(n.get_content() for n in stored) == sorted(
        r["full_text"] for r in rows)


def test_run_lock_adopt_refreshes_a_key_taken_for_the_run(app, redis):
    taken = run_locks.RunLock("k", "job:tok", 7200)
    assert taken.acquire()
    run = run_locks.RunLock("k", "job:tok", 60)
    run.adopt()
    run.beat()
    assert redis.ttl("k") == 60
    run.release()
    assert run_locks.holder("k") is None


# ── A key its job left behind ────────────────────────────────────────────

def _confirmed(monkeypatch, client):
    """An archive confirmed: (its task id, its import token)."""
    _fake_task_module(monkeypatch)
    token = _analyze(client, 1)
    task_id = _confirm(client, token).get_json()["task_id"]
    return task_id, token


@pytest.mark.parametrize("state", ["SUCCESS", "FAILURE", "REVOKED"])
def test_a_key_whose_job_ended_lets_the_next_confirm_through(
        app, monkeypatch, redis, state):
    client, user = _client(app)
    task_id, token = _confirmed(monkeypatch, client)
    monkeypatch.setattr(run_locks, "task_state",
                        lambda tid: state if tid == task_id else None)
    # The same archive again is a new import, not the ended one's id.
    resp = _confirm(client, token)
    assert resp.status_code == 202
    assert "already_running" not in resp.get_json()
    assert resp.get_json()["task_id"] != task_id


def _started(user, task_id, token):
    """The job took the key over and beat it once (as the task does)."""
    job = run_locks.RunLock(ta.import_lock_key(user.id),
                            ta.import_lock_token(task_id, token),
                            ta.IMPORT_RUNNING_TTL)
    job.adopt()
    job.beat()
    return job


def test_a_key_whose_job_stopped_beating_lets_the_next_confirm_through(
        app, monkeypatch, redis):
    """The job was stopped while it ran (a deploy): Celery still says it
    runs, but it has not beaten its key for three beats."""
    client, user = _client(app)
    task_id, token = _confirmed(monkeypatch, client)
    _started(user, task_id, token)
    monkeypatch.setattr(run_locks, "task_state", lambda tid: "PROGRESS")
    other = _analyze(client, 1000)
    redis.advance(2 * ta.IMPORT_BEAT_SECONDS)
    assert _confirm(client, other).status_code == 409   # could still beat
    redis.advance(2 * ta.IMPORT_BEAT_SECONDS)
    resp = _confirm(client, other)
    assert resp.status_code == 202
    assert resp.get_json()["task_id"] != task_id


def test_a_job_that_beats_keeps_the_account(app, monkeypatch, redis):
    client, user = _client(app)
    task_id, token = _confirmed(monkeypatch, client)
    job = _started(user, task_id, token)
    monkeypatch.setattr(run_locks, "task_state", lambda tid: "PROGRESS")
    other = _analyze(client, 1000)
    for _ in range(10):
        redis.advance(ta.IMPORT_BEAT_SECONDS)
        job.beat()
        assert _confirm(client, other).status_code == 409
    again = _confirm(client, token)
    assert again.get_json()["already_running"] is True
    assert again.get_json()["task_id"] == task_id


def test_a_queued_job_holds_the_account_until_its_ttl(
        app, monkeypatch, redis):
    """Celery cannot tell a queued job from a lost one (PENDING): the key
    holds until IMPORT_QUEUED_TTL."""
    client, user = _client(app)
    _confirmed(monkeypatch, client)
    monkeypatch.setattr(run_locks, "task_state", lambda tid: "PENDING")
    other = _analyze(client, 1000)
    redis.advance(ta.IMPORT_QUEUED_TTL - 1)
    assert _confirm(client, other).status_code == 409
    redis.advance(2)
    assert _confirm(client, other).status_code == 202


def test_the_job_beats_its_key_from_a_thread(app, monkeypatch, redis):
    """However long one step takes, the job's thread refreshes the key."""
    import time as _time
    from backend.tasks import imports as imports_mod
    monkeypatch.setattr(imports_mod, "flask_app", app)
    monkeypatch.setattr(ta, "IMPORT_BEAT_SECONDS", 0.02)
    client, user = _client(app)
    task_id, token = _confirmed(monkeypatch, client)
    key = ta.import_lock_key(user.id)
    seen = []

    def slow_step(**meta):
        # The clock moves almost past the running TTL during one step.
        redis.advance(ta.IMPORT_RUNNING_TTL - 5)
        _time.sleep(0.2)
        seen.append(redis.ttl(key))

    imports_mod.import_twitter_archive_impl(
        slow_step, user.id, token,
        {"import_type": "separate_nodes", "include_replies": True,
         "privacy_level": "private", "ai_usage": "none", "on_deleted": None},
        task_id=task_id)
    assert seen and all(t is not None and t > ta.IMPORT_RUNNING_TTL - 5
                        for t in seen)
    assert ta.running_import(user.id) is None
