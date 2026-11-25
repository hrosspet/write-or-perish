"""The nightly digest sweep sends each account as soon as it is ready
(A.141, Peter 2026-11-25: "make it prepare and send the batch request one
at a time").

- Keys are unwrapped in groups while a list is rendered
  (encryption.prefetch_deks); past the cap the keys of at most
  KEY_GROUP_ROWS - 1 rows are unwrapped without their text being read,
  and no text is read.
- Each account's request goes out as its own batch, its job row
  committed, before the next account is read. The task's soft time limit,
  a key-service outage or another error loses only the account being
  prepared, which stays due; a soft limit during a submission records a
  batch created before it.
- An account due at 4:00 and not reached is taken at 5 or 6 o'clock the
  same night, once a night; one whose list changed only after 4:00 waits
  for its next night. Those that waited longest go first.
- One sweep at a time (a run lock); one account rendered at a time.
- The collector reads the batches' statuses from the providers' listings.

The app, fixtures and fakes of test_external_digest_nightly.py; no model
or provider is called, the key service is a local stand-in.
"""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc

from backend.tests import test_external_digest_nightly as dn
from backend.extensions import db as _db
from backend.models import ExternalDigestBatchJob, ExternalItem, User
from backend.utils import encryption
from backend.utils.soft_limit import SoftTimeLimitExceeded

_digest = dn._digest
app = dn.app  # noqa: F811 (fixture)

HOUR = timedelta(hours=1)


def _night_account(name, hour=None):
    hour = _digest.NIGHTLY_DIGEST_LOCAL_HOUR if hour is None else hour
    user = User(username=name, timezone=dn._tz_at_hour(hour),
                last_seen_at=datetime.utcnow())
    _db.session.add(user)
    _db.session.commit()
    return user


def _saved(user, external_id, fetched_at, text="a page worth keeping"):
    item = ExternalItem(user_id=user.id, source="web_clip",
                        external_id=external_id,
                        url=f"https://example.com/{external_id}",
                        fetched_at=fetched_at)
    item.set_content(text)
    _db.session.add(item)
    _db.session.commit()
    return item


def _accounts(n, saved_ago=2 * HOUR):
    """*n* accounts in their 4 o'clock hour, each with a reference saved
    *saved_ago*, in id order (the order the sweep takes them: none has
    a digest)."""
    users = [_night_account(f"due{i}") for i in range(n)]
    for user in users:
        _saved(user, f"page-{user.id}", datetime.utcnow() - saved_ago)
    return users


def _sent_ids(submitted):
    return [r["user_id"] for call in submitted for reqs in call.values()
            for r in reqs]


def _job_user_ids():
    _db.session.expire_all()
    return [i["user_id"] for job in ExternalDigestBatchJob.query.order_by(
        ExternalDigestBatchJob.id).all() for i in job.items]


def _fail_at(monkeypatch, user_id, exc, where="render"):
    """*exc* raised for *user_id* in the render of its list, or (where
    "after_render") just after it, outside the per-account read whose
    ordinary errors skip only that account."""
    name = ("_render_digest_prompt" if where == "render" else "_note_cut")
    real = getattr(_digest, name)

    def fail(*args):
        uid = args[0] if where == "render" else args[0].id
        if uid == user_id:
            raise exc
        return real(*args)
    monkeypatch.setattr(_digest, name, fail)


# ── One account at a time ───────────────────────────────────────────────

def test_each_account_goes_out_as_its_own_batch_before_the_next_is_read(
        app, monkeypatch):
    """Before, every account's request was held until one submission at
    the end of the run. Now each is its own batch, with its job row
    committed, by the time the next account's list is read; the request
    itself is as before (the account's digest model, one_shot)."""
    users = _accounts(3)
    submitted = dn._stub_batch_submit(monkeypatch)
    jobs_when_read = []
    real = _digest._render_digest_prompt

    def render(uid):
        jobs_when_read.append(_job_user_ids())
        return real(uid)
    monkeypatch.setattr(_digest, "_render_digest_prompt", render)

    assert _digest.sweep_external_digests() == {"status": "ok",
                                                "submitted": 3}
    assert jobs_when_read == [[], [users[0].id],
                              [users[0].id, users[1].id]]
    assert [_sent_ids([call]) for call in submitted] == [
        [u.id] for u in users]
    assert all(call["anthropic"][0]["one_shot"] is True
               and call["anthropic"][0]["model_id"] == "claude-sonnet-5.5"
               for call in submitted)
    assert [len(job.items) for job in ExternalDigestBatchJob.query.all()] \
        == [1, 1, 1]


@pytest.mark.parametrize("exc, where", [
    (SoftTimeLimitExceeded(), "render"),
    (gexc.ServiceUnavailable("the key service is unavailable"), "render"),
    (RuntimeError("something else broke"), "after_render"),
], ids=["soft-limit", "key-service-outage", "other-error"])
def test_a_stop_mid_run_loses_only_the_account_being_prepared(
        app, monkeypatch, exc, where):
    """Before, the accounts rendered before the stop were lost with it,
    and the next run did not take them (an hour later they were not at
    4 o'clock): they waited a day, and the same could happen every
    night. Now they are submitted; the one being prepared and those
    after it have nothing written and stay due. The run stops with the
    error, as before."""
    first, second, third, fourth = _accounts(4)
    submitted = dn._stub_batch_submit(monkeypatch)
    _fail_at(monkeypatch, third.id, exc, where)

    with pytest.raises(type(exc)):
        _digest.sweep_external_digests()
    assert _sent_ids(submitted) == [first.id, second.id]
    assert _job_user_ids() == [first.id, second.id]
    stale = _digest._stale_user_ids()
    assert third.id in stale and fourth.id in stale
    assert not ({third.id, fourth.id}
                & _digest._users_in_pending_batches())


def test_a_soft_limit_during_a_submission_records_the_batch_it_created(
        app, monkeypatch):
    """batch_submit raises the soft limit with the batches created before
    it (llm_batch._stop_on_soft_limit): the job is recorded, so the
    collector saves that digest instead of the batch running unread."""
    first, second = _accounts(2)

    def submit(requests_by_provider, api_keys, phase=None):
        stop = SoftTimeLimitExceeded()
        stop.submitted = {"anthropic": "msgbatch_before_the_limit"}
        raise stop
    monkeypatch.setattr(_digest, "batch_submit", submit)

    with pytest.raises(SoftTimeLimitExceeded):
        _digest.sweep_external_digests()
    job = ExternalDigestBatchJob.query.one()
    assert job.batch_id == "msgbatch_before_the_limit"
    assert [i["user_id"] for i in job.items] == [first.id]
    assert second.id not in _digest._users_in_pending_batches()


def test_a_refused_submission_leaves_that_account_due_and_goes_on(
        app, monkeypatch):
    """batch_submit logs a provider's refusal or failure and returns no
    batch: no job row, the account stays due, the next account goes
    out."""
    first, second = _accounts(2)
    sent = []

    def submit(requests_by_provider, api_keys, phase=None):
        uid = requests_by_provider["anthropic"][0]["user_id"]
        sent.append(uid)
        return {} if uid == first.id else {"anthropic": f"b{uid}"}
    monkeypatch.setattr(_digest, "batch_submit", submit)

    assert _digest.sweep_external_digests() == {"status": "ok",
                                                "submitted": 1}
    assert sent == [first.id, second.id]
    assert _job_user_ids() == [second.id]
    assert first.id in _digest._stale_user_ids()


def test_one_account_is_rendered_at_a_time(app, monkeypatch):
    """Memory, unchanged: the accounts are rendered one after another,
    never side by side, and each list streams its rows (see
    test_the_rows_are_streamed_not_loaded_whole)."""
    users = _accounts(4)
    dn._stub_batch_submit(monkeypatch)
    in_flight = []
    active = []
    real = _digest._render_digest_prompt

    def render(uid):
        active.append(uid)
        in_flight.append(len(active))
        try:
            return real(uid)
        finally:
            active.remove(uid)
    monkeypatch.setattr(_digest, "_render_digest_prompt", render)

    assert _digest.sweep_external_digests()["submitted"] == len(users)
    assert in_flight == [1] * len(users)


# ── The same night, once ────────────────────────────────────────────────

def _close_jobs():
    """The collector ran and the batches ended without a digest (failed
    items): nothing pending, nothing saved, the accounts still stale."""
    ExternalDigestBatchJob.query.update({"status": "collected",
                                         "collected_at": datetime.utcnow()})
    _db.session.commit()


def test_an_account_not_reached_is_taken_later_that_night_once(
        app, monkeypatch):
    """The 4 o'clock run stops at the soft time limit while it prepares
    the second account; the 5 o'clock run takes that one and the third,
    and not the first (its batch ended without a digest, so it is still
    stale), nor an account whose list changed only after 4:00; the 6
    o'clock run takes nobody again; 7 o'clock is past the night; the
    next night takes them at 4 as before."""
    now = datetime.utcnow()
    first, second, third = _accounts(3)
    # Current at the 4 o'clock run, then a save after 4:00.
    later = _night_account("saved-after-four")
    _saved(later, "old", now - 4 * HOUR)
    dn._mk_digest(later.id, created_at=now - 3 * HOUR)
    submitted = dn._stub_batch_submit(monkeypatch)
    real_render = _digest._render_digest_prompt
    _fail_at(monkeypatch, second.id, SoftTimeLimitExceeded())

    with pytest.raises(SoftTimeLimitExceeded):
        _digest._sweep(now)
    assert _sent_ids(submitted) == [first.id]
    _close_jobs()
    _saved(later, "new", now + HOUR / 2)
    monkeypatch.setattr(_digest, "_render_digest_prompt", real_render)

    assert _digest._sweep(now + HOUR) == {"status": "ok", "submitted": 2}
    assert _sent_ids(submitted) == [first.id, second.id, third.id]
    _close_jobs()

    for hour in (2, 3):   # 6 o'clock: sent already; 7: past the night
        assert _digest._sweep(now + hour * HOUR) == {"status": "ok",
                                                     "submitted": 0}
    assert _sent_ids(submitted) == [first.id, second.id, third.id]

    # The next night, at 4: all four, the one saved after 4:00 included.
    assert _digest._sweep(now + 24 * HOUR)["submitted"] == 4


def test_a_late_hour_takes_only_accounts_due_at_four(app, monkeypatch):
    """At 5 or 6 o'clock an account is taken only when its list had
    changed by 4:00 and no request of it went out since; an account at 7
    is not taken."""
    now = datetime.utcnow()
    at_five = _night_account("five", hour=5)
    _saved(at_five, "a", now - 2 * HOUR)
    at_six = _night_account("six", hour=6)
    _saved(at_six, "b", now - 3 * HOUR)
    changed_late = _night_account("changed-late", hour=5)
    _saved(changed_late, "c", now - 10 * HOUR)
    dn._mk_digest(changed_late.id, created_at=now - 9 * HOUR)
    _saved(changed_late, "d", now)   # after 4:00 on its clock
    at_seven = _night_account("seven", hour=7)
    _saved(at_seven, "e", now - 4 * HOUR)
    submitted = dn._stub_batch_submit(monkeypatch)

    assert _digest.sweep_external_digests() == {"status": "ok",
                                                "submitted": 2}
    assert sorted(_sent_ids(submitted)) == sorted([at_five.id, at_six.id])


def test_those_that_waited_longest_go_first(app, monkeypatch):
    """The account whose 4:00 passed earliest (left over by an earlier
    run), then the oldest digest, never built first; not the database's
    order. An account a stop kept from its digest one night is first in
    its hour the next."""
    now = datetime.utcnow()
    newer = _night_account("digest-yesterday")
    older = _night_account("digest-three-days-ago")
    never = _night_account("never-built")
    leftover = _night_account("left-over-at-five", hour=5)
    for user, built in ((newer, now - 24 * HOUR), (older, now - 72 * HOUR),
                        (never, None), (leftover, now - 24 * HOUR)):
        _saved(user, f"p{user.id}", now - 2 * HOUR)
        if built is not None:
            dn._mk_digest(user.id, created_at=built)
    submitted = dn._stub_batch_submit(monkeypatch)
    rendered = []
    real = _digest._render_digest_prompt
    monkeypatch.setattr(_digest, "_render_digest_prompt",
                        lambda uid: rendered.append(uid) or real(uid))

    assert _digest.sweep_external_digests()["submitted"] == 4
    assert rendered == [leftover.id, never.id, older.id, newer.id]
    assert _sent_ids(submitted) == rendered


def test_one_sweep_at_a_time(app, monkeypatch):
    """Sweeps queued during a freeze run side by side afterwards; two
    runs taking the same account would each send its digest."""
    from backend.tests.fake_redis import install
    from backend.utils import run_locks
    _accounts(1)
    submitted = dn._stub_batch_submit(monkeypatch)
    install(monkeypatch)
    other = run_locks.RunLock(_digest.SWEEP_LOCK_KEY, "another-run",
                              _digest.SWEEP_LOCK_TTL)
    assert other.acquire()

    assert _digest.sweep_external_digests() == {"status": "skipped",
                                                "reason": "running"}
    assert submitted == []
    other.release()
    assert _digest.sweep_external_digests()["submitted"] == 1


# ── The collector reads many small batches from the listings ────────────

def test_the_collector_reads_the_batches_statuses_from_the_listings(
        app, monkeypatch):
    """With a batch per account, a night has as many batches as accounts.
    Their statuses come from the providers' list endpoints, many per call
    (Peter, 2026-11-03); a batch the listing shows still running costs no
    call of its own, an ended one is collected from the listing's object,
    one the listing does not show is read on its own, as before."""
    users = _accounts(3)
    for i, user in enumerate(users):
        dn._pending_job(user, datetime.utcnow(), batch_id=f"b{i}")
    running = SimpleNamespace(id="b0", processing_status="in_progress")
    ended = SimpleNamespace(id="b1", processing_status="ended")
    asked = []
    monkeypatch.setattr(_digest, "list_batches", lambda wanted, keys,
                        oldest=None: asked.append(sorted(wanted)) or {
                            "b0": running, "b1": ended})
    read = []

    def collect(batch_ids, keys, known=None):
        read.append((batch_ids, sorted(known or {})))
        return {}, {}, {}
    monkeypatch.setattr(_digest, "batch_check_and_collect", collect)

    assert _digest._collect_digest_batches() == {"collected": 2,
                                                 "abandoned": 0}
    assert asked == [[("anthropic", "b0"), ("anthropic", "b1"),
                      ("anthropic", "b2")]]
    assert read == [({"anthropic": "b1"}, ["b1"]),
                    ({"anthropic": "b2"}, [])]
    _db.session.expire_all()
    assert [j.status for j in ExternalDigestBatchJob.query.order_by(
        ExternalDigestBatchJob.id)] == ["pending", "collected", "collected"]


# ── Keys unwrapped in groups ────────────────────────────────────────────

@pytest.fixture
def key_service(monkeypatch):
    """Encryption on, the key service a local stand-in that records the
    thread of each unwrap call; the key cache empty."""
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    calls = []

    def decrypt(request):
        calls.append(threading.current_thread().name)
        return SimpleNamespace(
            plaintext=request["ciphertext"][len(b"wrapped:"):])
    monkeypatch.setattr(encryption, "_kms_decrypt", decrypt)
    monkeypatch.setattr(encryption, "_dek_cache", {})
    monkeypatch.setattr(encryption, "_dek_cache_order", [])

    def cold():
        encryption._dek_cache.clear()
        encryption._dek_cache_order.clear()
        calls.clear()
    return SimpleNamespace(calls=calls, cold=cold)


def _bookmarks(user, texts):
    """Saved one a day, oldest first, so the list reads them newest
    first: the last of *texts* first."""
    start = datetime(2026, 1, 1)
    for i, text in enumerate(texts):
        item = ExternalItem(user_id=user.id, source="twitter_bookmark",
                            external_id=f"t{i}", author_handle="author",
                            saved_at=start + timedelta(days=i),
                            fetched_at=start + timedelta(days=i))
        item.set_content(text)
        _db.session.add(item)
    _db.session.commit()


def _cap_after(monkeypatch, user, total, lines, extra=0):
    """The cap at the fixed part, the first *lines* listed lines with
    their line breaks, and *extra* tokens more."""
    count, _ = _digest._token_counter()
    prompt = _digest._render_digest_prompt(user.id).prompt
    listed = [line for line in prompt.splitlines() if line.startswith("- @")]
    monkeypatch.setitem(
        _digest.flask_app.config, "DIGEST_MAX_PROMPT_TOKENS",
        _digest._fixed_prompt_tokens(count, total)
        + sum(count(line) + 1 for line in listed[:lines]) + extra)
    return listed


def test_keys_are_unwrapped_in_groups_and_few_past_the_cap(
        app, monkeypatch, key_service):
    """Every key the list needs is unwrapped by the prefetch pool, none
    one after another on the render's thread. With lines of even length
    the groups end near the cap: at most one round of the pool past the
    line that did not fit."""
    user = User.query.first()
    _bookmarks(user, [f"tweet {i:04d} " + "x" * 60 for i in range(300)])
    _cap_after(monkeypatch, user, 300, 100)
    key_service.cold()

    rendered = _digest._render_digest_prompt(user.id)
    assert (rendered.listed, rendered.cut) == (100, True)
    assert "tweet 0299" in rendered.prompt   # decrypted, newest first
    assert key_service.calls, "no key was unwrapped"
    assert all(name.startswith("dek-unwrap") for name in key_service.calls)
    past = len(key_service.calls) - (rendered.listed + 1)
    assert 0 <= past <= encryption.DEK_PREFETCH_CONCURRENCY - 1


def test_past_the_cap_at_most_a_group_less_one_key_for_uneven_lines(
        app, monkeypatch, key_service):
    """The bound whatever the lines' lengths: eight short lines first make
    the next group expect far more lines to fit than the long ones after
    them do. The rest of that group's keys are unwrapped, never more than
    KEY_GROUP_ROWS - 1, and no text is read past the line that did not
    fit."""
    user = User.query.first()
    _bookmarks(user, [f"long {i} " + "y" * 270 for i in range(292)]
               + [f"s{i}" for i in range(8)])
    # Room for the 8 short lines, 5 long ones and almost a sixth: by the
    # short lines' average, about 75 more lines.
    count, _ = _digest._token_counter()
    listed = _cap_after(monkeypatch, user, 300, 13)
    monkeypatch.setitem(
        _digest.flask_app.config, "DIGEST_MAX_PROMPT_TOKENS",
        _digest.flask_app.config["DIGEST_MAX_PROMPT_TOKENS"]
        + count(listed[13]))
    read = []
    real = ExternalItem.get_content
    monkeypatch.setattr(ExternalItem, "get_content",
                        lambda self: read.append(self.id) or real(self))
    key_service.cold()

    rendered = _digest._render_digest_prompt(user.id)
    assert (rendered.listed, rendered.cut) == (13, True)
    assert len(read) == rendered.listed + 1
    past = len(key_service.calls) - (rendered.listed + 1)
    assert encryption.DEK_PREFETCH_CONCURRENCY - 1 < past
    assert past <= _digest.KEY_GROUP_ROWS - 1


def test_a_key_service_outage_tries_one_round_of_keys(
        app, monkeypatch, key_service):
    """The first group is one round of the pool: an outage is met by
    that round (each call with its own retries, about 30 s) and the read
    of the first text, then the run ends, as before, without trying the
    rest of the list or the next account."""
    first, _second = _accounts(2)
    for i in range(30):
        _saved(first, f"more-{i}", datetime.utcnow() - 3 * HOUR)
    submitted = dn._stub_batch_submit(monkeypatch)
    key_service.cold()

    def down(request):
        key_service.calls.append(threading.current_thread().name)
        raise gexc.ServiceUnavailable("the key service is unavailable")
    monkeypatch.setattr(encryption, "_kms_decrypt", down)

    with pytest.raises(gexc.ServiceUnavailable):
        _digest.sweep_external_digests()
    assert len(key_service.calls) == encryption.DEK_PREFETCH_CONCURRENCY + 1
    assert submitted == []
