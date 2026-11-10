""""Delete all my content" as a fresh start (#590; Peter, 2026-10-22).

On #415's fixture world (sqlite with foreign keys on, test_user_purge):

* the request stops only the work that read the old content: provider
  batches that carry only her old work are cancelled at the provider
  (the job rows stay pending, so a late result is still collected,
  dropped and costed), the X bookmark connection ends at once, and the
  account is not on hold: what she writes next is eligible for a profile
  as a new account's would be;
* a restore brings everything back, makes the restored profile the one
  in use again and drops the versions Loore built since; a version she
  saved by hand since is kept and merged at the next update;
* the purge after 30 days deletes only what the request hid, and keeps
  what came after: new writing, new profile versions, a new X
  connection, new notifications and the work in flight on them.
"""
from datetime import datetime, timedelta

import pytest

import backend.tests.test_user_purge as _base
from backend.tests.test_user_purge import (
    _add, _client, _db, _node, _XRevoke, up,
)

# #415's fixtures: the app with foreign keys on, the stubs, alice's world.
app = _base.app
stubs = _base.stubs
world = _base.world
from backend.models import (  # noqa: E402
    APICostLog, ArtifactView, ExternalAccount, Node, ProfileBatchJob,
    ProfileBlockedRange, TTSChunk, User, UserDataPurge, UserNotification,
    UserProfile, UserRecentContext,
)
from backend.utils.hidden_rows import (  # noqa: E402
    account_on_hold, content_deletion_cutoff, read_before_deletion,
)
from backend.utils.profile_versions import current_profile_query  # noqa: E402


def _ask(app, world):
    c = _client(app, world.alice)
    r = c.delete("/api/account/data", json={"confirm": "alice"})
    assert r.status_code == 202, r.get_json()
    _db.session.expire_all()
    return c, UserDataPurge.query.filter_by(user_id=world.alice.id).one()


def _restore(c):
    r = c.post("/api/account/data/restore")
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()


def _built_since(user_id, parent=None, text="built from new writing",
                 **kw):
    """A profile version Loore built after the request."""
    p = UserProfile(user_id=user_id, generated_by="claude-opus",
                    generation_type="initial",
                    parent_profile_id=parent.id if parent else None,
                    source_tokens_used=500,
                    source_data_cutoff=datetime.utcnow(), **kw)
    p.set_content(text)
    _add(p)
    _db.session.commit()
    return p


def _saved_by_hand(user_id, parent, text="MY OWN WORDS"):
    p = UserProfile(user_id=user_id, generated_by="user",
                    parent_profile_id=parent.id,
                    source_tokens_used=parent.source_tokens_used,
                    source_data_cutoff=parent.source_data_cutoff)
    p.set_content(text)
    _add(p)
    _db.session.commit()
    return p


# ── The request ─────────────────────────────────────────────────────────

def test_the_account_is_not_on_hold_after_the_request(app, world, stubs):
    a = world.alice.id
    assert a in {u.id for u in User.profile_eligible_query()}
    _, job = _ask(app, world)

    # Work that read before the request saves nothing; work that reads
    # after it runs as for a new account.
    assert content_deletion_cutoff(a) == job.requested_at
    assert read_before_deletion(a, job.requested_at - timedelta(minutes=1))
    assert read_before_deletion(a, None)
    assert not read_before_deletion(a, datetime.utcnow()
                                    + timedelta(seconds=1))
    assert account_on_hold(a) is False
    assert a in {u.id for u in User.profile_eligible_query()}
    # Bob is untouched by her request.
    assert not read_before_deletion(world.bob.id, None)
    # Everything the profile was built from is hidden: the next build
    # starts from the new writing.
    assert current_profile_query(a).first() is None


def test_an_account_deletion_still_holds_everything(app, world, stubs):
    from backend.utils.account_deletion import schedule_account_deletion
    a = world.alice.id
    schedule_account_deletion(world.alice, requested_by_id=a, source="self")
    assert account_on_hold(a) is True
    assert read_before_deletion(a, datetime.utcnow() + timedelta(days=1))
    assert a not in {u.id for u in User.profile_eligible_query()}


def test_the_request_cancels_batches_that_carry_only_her_old_work(
        app, world, stubs):
    a, b = world.alice.id, world.bob.id
    # A batch submitted for her after the request is not cancelled (it
    # read the new writing); nor is one shared with bob.
    _ask(app, world)
    # Only hers, read before the request: her profile batch and the AI
    # draft of her poll answer.
    assert sorted(stubs.cancelled) == ["b-only", "pd-a"]
    # The rows stay pending: the collectors still collect what ran before
    # the cancel landed, drop it and log its cost.
    for bid in ("b-only", "b-mixed", "b-bob"):
        assert ProfileBatchJob.query.filter_by(batch_id=bid).one().status \
            == "pending"
    # Nothing was revoked or reset: a restore finds everything as it was.
    assert stubs.revoked == []
    assert _db.session.get(User, a).profile_batch_pending is True
    assert _db.session.get(User, b).profile_batch_pending is False


def test_the_request_cancels_a_glean_waiting_in_a_provider_batch(
        app, world, stubs):
    import json
    world.A2.tool_calls_meta = json.dumps([
        {"name": "_batch", "status": "submitted", "batch_id": "read-1",
         "provider": "openai", "key_type": "chat"}])
    _db.session.commit()
    _ask(app, world)
    assert "read-1" in stubs.cancelled


def test_a_cancel_that_runs_after_a_restore_cancels_nothing(
        app, world, stubs, monkeypatch):
    """The cancel runs in a Celery task; if the user restored first, the
    batches of the restored content are left to run."""
    sent = []
    monkeypatch.setattr(up, "_dispatch_batch_cancel",
                        lambda *a: sent.append(a))
    c, job = _ask(app, world)
    _restore(c)
    assert len(sent) == 1
    assert up.cancel_batches_read_before(*sent[0]) == 0
    assert stubs.cancelled == []


def test_a_second_request_cancels_what_she_started_since(app, world, stubs):
    a = world.alice.id
    _ask(app, world)
    stubs.cancelled.clear()
    # A build of the new writing was sent after the first request.
    _add(ProfileBatchJob(provider_key="anthropic", batch_id="b-new",
                         status="pending",
                         items=[{"custom_id": "n1", "user_id": a,
                                 "rendered_at": datetime.utcnow().isoformat()}]))
    _db.session.commit()
    _ask(app, world)
    assert "b-new" in stubs.cancelled


def test_the_request_ends_the_x_connection_at_once(app, world, stubs,
                                                    monkeypatch):
    a, b = world.alice.id, world.bob.id
    acc = ExternalAccount.query.filter_by(user_id=a).one()
    acc.access_token, acc.refresh_token = "x-access", "x-refresh"
    _db.session.commit()
    x = _XRevoke(monkeypatch, app, a, "ok")

    c, _job = _ask(app, world)

    # Revoked at X while the stored connection still existed, then gone.
    assert x.tokens == ["x-access", "x-refresh"]
    assert all(rows == 1 for (_t, _auth, _timeout, rows) in x.calls)
    assert ExternalAccount.query.filter_by(user_id=a).count() == 0
    assert ExternalAccount.query.filter_by(user_id=b).count() == 1
    # The dialog's X paragraph reads this before the request.
    assert up.deletion_status(a)["x_connected"] is False

    # A restore brings her saved references back, not the connection.
    _restore(c)
    assert ExternalAccount.query.filter_by(user_id=a).count() == 0
    from backend.models import ExternalItem
    assert ExternalItem.query.filter_by(user_id=a).count() == 1


def test_a_failed_x_revoke_does_not_stop_the_request(app, world, stubs,
                                                     monkeypatch):
    a = world.alice.id
    acc = ExternalAccount.query.filter_by(user_id=a).one()
    acc.access_token, acc.refresh_token = "x-access", "x-refresh"
    _db.session.commit()
    _XRevoke(monkeypatch, app, a, "timeout")
    _ask(app, world)
    assert ExternalAccount.query.filter_by(user_id=a).count() == 0


# ── The restore ─────────────────────────────────────────────────────────

def test_a_restore_drops_the_versions_built_since(app, world, stubs):
    a = world.alice.id
    c, _job = _ask(app, world)
    new = _built_since(a)
    update = _built_since(a, parent=new, text="an update")
    _add(TTSChunk(profile_id=new.id, chunk_index=0))
    rc = _add(UserRecentContext(user_id=a, content="since", generated_by="m",
                                profile_id=update.id))
    _add(APICostLog(user_id=a, model_id="m", request_type="profile_batch",
                    cost_microdollars=40))
    _db.session.commit()

    _restore(c)

    ids = {p.id for p in UserProfile.query.filter_by(user_id=a)}
    assert ids == {world.P1.id, world.P2.id}
    assert current_profile_query(a).first().id == world.P2.id
    assert TTSChunk.query.filter_by(profile_id=new.id).count() == 0
    # The summary of the new writing stays (the newest row wins, as for
    # the todo list); it no longer points at a dropped version.
    assert _db.session.get(UserRecentContext, rc.id).profile_id is None
    assert _db.session.get(User, a).profile_pending_merge_id is None
    # The cost rows of the dropped versions stay.
    assert APICostLog.query.filter_by(user_id=a).count() == 3


def test_a_restore_keeps_a_version_she_saved_and_merges_it_next(
        app, world, stubs):
    from backend.tasks import exports
    a = world.alice.id
    c, _job = _ask(app, world)
    new = _built_since(a)
    mine = _saved_by_hand(a, new)

    _restore(c)

    # The version Loore built is gone; hers stays, and the restored
    # profile is the one in use again, as a copy of it (a "revert").
    assert _db.session.get(UserProfile, new.id) is None
    kept = _db.session.get(UserProfile, mine.id)
    assert kept is not None and kept.get_content() == "MY OWN WORDS"
    assert kept.parent_profile_id is None
    current = current_profile_query(a).first()
    assert current.id not in (world.P2.id, mine.id)
    assert current.generation_type == "revert"
    assert current.parent_profile_id == world.P2.id
    assert current.content == world.P2.content
    assert current.generated_by == world.P2.generated_by
    assert exports.profile_update_base(a).id == current.id
    # Her version merges into the restored profile at the next update
    # (#183's pending merge): it is due now, nothing older is left to read.
    user = _db.session.get(User, a)
    assert user.profile_pending_merge_id == mine.id
    merge = exports.next_user_merge(user, current, lambda: False)
    assert merge is not None and merge.id == mine.id


def test_a_restore_after_two_requests_drops_everything_built_since_the_first(
        app, world, stubs):
    a = world.alice.id
    c, job = _ask(app, world)
    first = job.first_requested_at
    between = _built_since(a, text="built between the two requests")
    _ask(app, world)
    job = _db.session.get(UserDataPurge, job.id)
    assert job.first_requested_at == first and job.requested_at > first
    after = _built_since(a, text="built after the second")

    _restore(c)

    assert _db.session.get(UserProfile, between.id) is None
    assert _db.session.get(UserProfile, after.id) is None
    assert current_profile_query(a).first().id == world.P2.id


def test_a_restore_of_an_account_deletion_restores_the_profile_too(
        app, world, stubs):
    """An account deletion that replaced a waiting content deletion: its
    restore brings the content back with the same profile rule."""
    from backend.utils.account_deletion import (
        restore_account, schedule_account_deletion,
    )
    a = world.alice.id
    _ask(app, world)
    new = _built_since(a)
    schedule_account_deletion(_db.session.get(User, a), requested_by_id=a,
                              source="self")
    assert restore_account(_db.session.get(User, a))
    _db.session.expire_all()
    assert _db.session.get(UserProfile, new.id) is None
    assert current_profile_query(a).first().id == world.P2.id


# ── Day 30 ──────────────────────────────────────────────────────────────

def test_the_purge_keeps_what_came_after_the_request(app, world, stubs,
                                                     monkeypatch):
    a, b = world.alice.id, world.bob.id
    old_profiles = [world.P1.id, world.P2.id]
    _c, job = _ask(app, world)
    alice = _db.session.get(User, a)

    # Since the request: new writing, a profile Loore built from it, a new
    # X connection, a notification, an artifact view, a blocked range of
    # the new writing, and a build of it in flight.
    fresh = _node(alice, owner=a, text="a fresh start")
    built = _built_since(a)
    _add(ExternalAccount(user_id=a, provider="twitter", handle="alice2",
                         access_token="new-access", refresh_token="new-ref"))
    note = _add(UserNotification(user_id=a, type="profile_ready",
                                 title="Your profile is ready"))
    view = _add(ArtifactView(user_id=a, kind="profile"))
    blocked = _add(ProfileBlockedRange(user_id=a,
                                       range_end=datetime.utcnow(),
                                       code="blocked"))
    _add(ProfileBlockedRange(user_id=a, range_end=datetime(2026, 1, 1),
                             code="old", created_at=job.requested_at
                             - timedelta(days=1)))
    in_flight = _add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b-new", status="pending",
        items=[{"custom_id": "n1", "user_id": a,
                "rendered_at": datetime.utcnow().isoformat()}]))
    alice.profile_batch_pending = True
    alice.profile_generation_task_id = "t-new"
    _db.session.commit()
    x = _XRevoke(monkeypatch, app, a, "ok")

    tokens = []
    later = job.scheduled_for + timedelta(minutes=1)
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                                now=later) == [job.id]
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()

    # The old content is gone...
    assert UserProfile.query.filter(
        UserProfile.id.in_(old_profiles)).count() == 0
    assert _db.session.get(Node, world.ids["A2"]) is None
    assert UserNotification.query.filter_by(user_id=a).all() == [
        _db.session.get(UserNotification, note.id)]
    assert ArtifactView.query.filter_by(user_id=a).all() == [
        _db.session.get(ArtifactView, view.id)]
    assert ProfileBlockedRange.query.filter_by(user_id=a).all() == [
        _db.session.get(ProfileBlockedRange, blocked.id)]
    # ...what came after stays, the new X connection with it, unrevoked.
    assert _db.session.get(Node, fresh.id).deleted_at is None
    assert _db.session.get(UserProfile, built.id) is not None
    assert ExternalAccount.query.filter_by(user_id=a).one().handle == "alice2"
    assert x.calls == []
    assert ExternalAccount.query.filter_by(user_id=b).count() == 1
    # The build in flight on the new writing is neither stripped nor
    # stopped: its job, its flags and its task stay.
    job_row = _db.session.get(ProfileBatchJob, in_flight.id)
    assert job_row.status == "pending" and len(job_row.items) == 1
    alice = _db.session.get(User, a)
    assert alice.profile_batch_pending is True
    assert "t-new" not in stubs.revoked
    # The old work was stripped as before (the shared job keeps bob's).
    mixed = ProfileBatchJob.query.filter_by(batch_id="b-mixed").one()
    assert [i["user_id"] for i in mixed.items] == [b]
    assert ProfileBatchJob.query.filter_by(batch_id="b-only").one().status \
        == "cancelled"


def test_an_account_deletion_still_revokes_and_deletes_the_connection(
        app, world, stubs, monkeypatch):
    a = world.alice.id
    acc = ExternalAccount.query.filter_by(user_id=a).one()
    acc.access_token, acc.refresh_token = "x-access", "x-refresh"
    _db.session.commit()
    x = _XRevoke(monkeypatch, app, a, "ok")
    _job_id, outcome = _base._run_job()
    assert outcome == "done"
    assert x.tokens == ["x-access", "x-refresh"]
    assert ExternalAccount.query.filter_by(user_id=a).count() == 0


@pytest.mark.parametrize("read_at, dropped", [
    (timedelta(seconds=-5), True), (timedelta(seconds=5), False)])
def test_the_purge_scope_takes_only_items_read_before(app, world, stubs,
                                                     read_at, dropped):
    _c, job = _ask(app, world)
    scope = up.scope_of(job)
    item = {"user_id": world.alice.id,
            "rendered_at": (job.requested_at + read_at).isoformat()}
    assert scope.read_before(item, datetime.utcnow()) is dropped
    # No render time: the job's submission stands in.
    assert scope.read_before({"user_id": world.alice.id},
                             job.requested_at - timedelta(seconds=1))
