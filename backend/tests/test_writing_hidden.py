"""'Delete all my writing' hides everything at once (#268; Peter,
2026-10-09: "a soft-delete of everything immediately + real deletion after
30 days. The Cancel deletion button undoing the soft deletion").

On #415's fixture world (sqlite with foreign keys on): the request hides
every node and per-user row of alice's at once, for her and for bob; a
restore brings back exactly that (and not what she had deleted before),
in one transaction; the purge after the grace period deletes what the
request hid and keeps what she wrote afterwards; saving the same
reference again, re-importing an entry or answering a poll again during
the grace period works; background jobs skip her meanwhile.
"""
from datetime import datetime, timedelta

import pytest

import backend.tests.test_user_purge as _base
from backend.tests.test_user_purge import (
    _add, _bob_snapshot, _client, _db, _file, _node, _stub_dispatch, up, T0,
)

# #415's fixtures: the app with foreign keys on, the stubs, alice's world.
app = _base.app
stubs = _base.stubs
world = _base.world
from backend.models import (
    HIDDEN_ROW_TABLES, APICostLog, Draft, ExternalItem, Node, PollResponse,
    TTSChunk, User, UserDataPurge, UserDataPurgeHidden, UserProfile,
    UserTodo,
)
from backend.utils.hidden_rows import (
    drop_hidden_poll_response, including_hidden_rows, on_hold_user_ids,
    reclaim_external_items, writing_on_hold,
)
from backend.utils.privacy import can_user_access_node
from backend.utils.system_accounts import ERASED_SYSTEM_USERNAME


def _alice_nodes(world):
    """Every node of alice's: her own, the AI replies she asked for, and
    the legacy AI reply under her entry (L2)."""
    a = world.alice.id
    with including_hidden_rows():
        return Node.query.filter(
            (Node.user_id == a) | (Node.human_owner_id == a)
            | (Node.id == world.ids["L2"])).order_by(Node.id).all()


def _rows(model, *conds):
    cols = [c.name for c in model.__table__.columns]
    with including_hidden_rows():
        return [{c: getattr(r, c) for c in cols}
                for r in model.query.filter(*conds).order_by(model.id)]


def _alice_snapshot(world):
    """Every row of alice's that the request hides, all columns."""
    a = world.alice.id
    _db.session.expire_all()
    out = {"node": _rows(Node, Node.id.in_([n.id for n in _alice_nodes(world)]))}
    for model in HIDDEN_ROW_TABLES:
        out[model.__tablename__] = _rows(model, model.user_id == a)
    out["user"] = _rows(User, User.id == a)
    return out


def _ask(app, world):
    c = _client(app, world.alice)
    r = c.delete("/api/account/data", json={"confirm": "alice"})
    assert r.status_code == 202
    return c, r.get_json()


def _deleted_before(world):
    """An entry alice deleted herself, before the request."""
    n = _node(world.alice, owner=world.alice.id, text="deleted earlier",
              deleted_at=T0)
    _db.session.commit()
    return n.id


def test_the_request_hides_all_her_writing_at_once(app, world, stubs):
    gone_id = _deleted_before(world)
    bob_before = _bob_snapshot(world)
    a, b = world.alice.id, world.bob.id
    assert writing_on_hold(a) is False

    _, body = _ask(app, world)
    assert body["status"] == "scheduled" and body["restorable"] is True
    job = UserDataPurge.query.one()
    assert job.scope == "hidden" and job.source == "self"

    # Every node of hers is deleted now, the legacy AI reply included; the
    # one she had deleted keeps its own date.
    _db.session.expire_all()
    for n in _alice_nodes(world):
        assert n.deleted_at is not None, n.id
    assert _db.session.get(Node, gone_id).deleted_at == T0
    kinds = up.hidden_counts(job.id)
    assert kinds["node"] == len(_alice_nodes(world)) - 1
    assert kinds["node_deleted"] == 1

    # The rows of every per-user table are gone from every query, hers
    # only; bob's are all there.
    for model in HIDDEN_ROW_TABLES:
        assert model.query.filter_by(user_id=a).count() == 0, model
        assert [r.id for r in model.query.filter_by(user_id=a).all()] == []
    assert UserProfile.query.filter_by(user_id=b).count() == 1
    assert ExternalItem.query.filter_by(user_id=b).count() == 1
    assert UserTodo.query.filter_by(user_id=b).count() == 1
    with including_hidden_rows():
        assert UserProfile.query.filter_by(user_id=a).count() == 2

    # Bob no longer sees her public entry, and his reply under it stays.
    assert not can_user_access_node(_db.session.get(Node, world.ids["A1"]), b)
    assert can_user_access_node(_db.session.get(Node, world.ids["B1"]), b)

    # Nothing of bob's changed, and no file was deleted.
    assert _bob_snapshot(world) == bob_before
    assert all(f.exists() for f in world.files_alice)

    # No background job builds anything from her writing meanwhile.
    assert writing_on_hold(a) is True
    assert a not in {u.id for u in User.profile_eligible_query()}
    assert {u for (u,) in _db.session.execute(on_hold_user_ids())} == {a}
    # Nothing in flight was stopped: a restore finds it as it was.
    assert stubs.revoked == []


def test_restore_brings_back_exactly_what_the_request_hid(app, world, stubs):
    gone_id = _deleted_before(world)
    before = _alice_snapshot(world)
    c, _ = _ask(app, world)

    r = c.post("/api/account/data/restore")
    assert r.status_code == 200
    assert r.get_json()["status"] is None
    assert r.get_json()["restorable"] is False
    # One documented change: the poll answer whose AI draft was in flight
    # (R1, "drafting", no text) got no draft while hidden; it comes back
    # where she can write it or ask again.
    assert [r["status"] for r in before["poll_response"]] == ["drafting"]
    after = _alice_snapshot(world)
    before["poll_response"][0]["status"] = "draft_failed"
    before["poll_response"][0]["updated_at"] = \
        after["poll_response"][0]["updated_at"]
    assert after == before
    assert _db.session.get(Node, gone_id).deleted_at == T0
    assert UserDataPurgeHidden.query.count() == 0
    assert UserDataPurge.query.one().status == "cancelled"
    assert writing_on_hold(world.alice.id) is False
    # Nothing to restore any more; the purge never starts.
    assert c.post("/api/account/data/restore").status_code == 404
    after = datetime.utcnow() + timedelta(days=31)
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("restored"),
                                now=after) == []


def test_a_restore_that_fails_half_way_changes_nothing(app, world, stubs,
                                                       monkeypatch):
    _ask(app, world)
    job = UserDataPurge.query.one()
    hidden = UserDataPurgeHidden.query.count()
    real = up.unhide_writing

    def unhide_then_fail(job_ids):
        real(job_ids)            # nodes shown, records gone, in the session
        raise RuntimeError("the database went away")
    monkeypatch.setattr(up, "unhide_writing", unhide_then_fail)

    with pytest.raises(RuntimeError):
        up.cancel_purge(world.alice.id, world.alice.id)
    _db.session.rollback()
    _db.session.expire_all()
    assert _db.session.get(UserDataPurge, job.id).status == "scheduled"
    assert UserDataPurgeHidden.query.count() == hidden
    assert all(n.deleted_at is not None for n in _alice_nodes(world))
    assert UserProfile.query.filter_by(user_id=world.alice.id).count() == 0

    # Run again, it restores everything.
    monkeypatch.setattr(up, "unhide_writing", real)
    assert up.cancel_purge(world.alice.id, world.alice.id)
    assert UserProfile.query.filter_by(user_id=world.alice.id).count() == 2


def test_the_purge_deletes_what_was_hidden_and_keeps_what_came_after(
        app, world, stubs):
    a = world.alice.id
    _ask(app, world)
    job = UserDataPurge.query.one()

    # Written after the request: a new entry with its recording and an AI
    # reply, a todo version, a draft with its recording, a reference, a
    # profile she wrote herself, a cost row.
    alice = _db.session.get(User, a)
    n1 = _node(alice, owner=a, text="a fresh start")
    nl = _node(world.llm, n1, owner=a, node_type="llm", text="a reply")
    todo = _add(UserTodo(user_id=a, content="new list", generated_by="user"))
    draft = _add(Draft(user_id=a, parent_id=n1.id, content="new draft",
                       session_id="sess-new"))
    item = _add(ExternalItem(user_id=a, source="web_clip", external_id="e-new",
                             content="new clip"))
    prof = _add(UserProfile(user_id=a, content="mine", generated_by="user"))
    cost = _add(APICostLog(user_id=a, model_id="m", request_type="conversation",
                           cost_microdollars=5,
                           created_at=job.requested_at + timedelta(seconds=1)))
    _db.session.commit()
    au = stubs.audio
    new_files = [
        _file(au / f"user/{a}/node/{n1.id}/original.webm"),
        _file(au / f"drafts/{a}/sess-new/chunk_0.webm"),
        _file(au / f"user/{a}/profile/{prof.id}/tts.mp3"),
    ]
    kept_ids = {n1.id, nl.id}

    # The grace period is over: the beat starts the purge.
    tokens = []
    later = job.scheduled_for + timedelta(minutes=1)
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t), now=later) == [job.id]
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()

    # Everything the request hid is gone, apart from the entries other
    # people replied under, which stay as empty placeholders.
    left = {n.id for n in _alice_nodes(world)}
    tombstones = {world.ids[k] for k in ("A1", "A4", "A5")}
    assert left == kept_ids | tombstones
    for nid in tombstones:
        n = _db.session.get(Node, nid)
        assert n.content is None and n.deleted_at is not None
    # What she wrote after the request is there, live.
    for nid in kept_ids:
        assert _db.session.get(Node, nid).deleted_at is None
    assert [r.id for r in UserTodo.query.filter_by(user_id=a)] == [todo.id]
    assert [r.id for r in Draft.query.filter_by(user_id=a)] == [draft.id]
    assert [r.id for r in ExternalItem.query.filter_by(user_id=a)] == [item.id]
    assert [r.id for r in UserProfile.query.filter_by(user_id=a)] == [prof.id]
    # Old files gone, new ones kept; bob's untouched.
    assert not any(f.exists() for f in world.files_alice)
    assert all(f.exists() for f in new_files)
    assert all(f.exists() for f in world.files_bob)
    # Cost rows until the request lose the name; the new one keeps it.
    erased = User.query.filter_by(username=ERASED_SYSTEM_USERNAME).one()
    assert APICostLog.query.filter_by(user_id=erased.id).count() == 2
    assert [r.id for r in APICostLog.query.filter_by(user_id=a)] == [cost.id]
    # The records of what it hid go with it; she is no longer on hold.
    assert UserDataPurgeHidden.query.count() == 0
    assert writing_on_hold(a) is False
    # Verified: nothing of what it hid is left.
    scope = up.Scope(a, job.id, job.requested_at)
    assert up.leftovers(up.count_user_data(a, scope=scope)) == {}
    assert up.deletion_status(a)["status"] == "done"
    # Bob's rows are still there.
    assert Node.query.get(world.ids["B1"]) is not None
    assert UserProfile.query.filter_by(user_id=world.bob.id).count() == 1


def test_an_entry_imported_again_during_the_grace_period_stays(
        app, world, stubs):
    """Re-importing brings back a deleted node (import_data's restore sets
    deleted_at to None): it is the user's again and the purge keeps it."""
    _ask(app, world)
    job = UserDataPurge.query.one()
    a8 = _db.session.get(Node, world.ids["A8"])
    a8.deleted_at = None
    _db.session.commit()
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                         now=job.scheduled_for + timedelta(minutes=1))
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    assert _db.session.get(Node, world.ids["A8"]) is not None


def test_saving_a_hidden_reference_again_makes_it_hers_again(
        app, world, stubs):
    a = world.alice.id
    _ask(app, world)
    assert ExternalItem.query.filter_by(user_id=a).count() == 0
    # The unique key (user, source, external id) would refuse a new row.
    assert reclaim_external_items(a, ["e1"]) == 1
    _db.session.commit()
    assert [i.id for i in ExternalItem.query.filter_by(user_id=a)] == \
        [world.I1.id]
    job = UserDataPurge.query.one()
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                         now=job.scheduled_for + timedelta(minutes=1))
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    assert _db.session.get(ExternalItem, world.I1.id) is not None
    assert TTSChunk.query.filter_by(item_id=world.I1.id).count() == 1


def test_answering_a_poll_again_replaces_the_hidden_answer(app, world, stubs):
    a = world.alice.id
    _ask(app, world)
    assert PollResponse.query.filter_by(user_id=a).count() == 0
    assert drop_hidden_poll_response(world.poll.id, a) is True
    _add(PollResponse(poll_id=world.poll.id, user_id=a, status="draft"))
    _db.session.commit()   # no unique-key error
    with including_hidden_rows():
        assert PollResponse.query.filter_by(user_id=a).count() == 1
    assert UserDataPurgeHidden.query.filter_by(kind="poll_response").count() == 0


# ── The admin's "Purge data" works the same way (Peter, 2026-10-13) ────

def _admin_purge(app, world):
    r = _client(app, world.admin).post(
        f"/api/admin/users/{world.alice.id}/purge_data",
        json={"confirm_username": "alice"})
    assert r.status_code == 202, r.get_json()
    return r.get_json()["job"]


def _same_as(before, after):
    """The restore's one documented change (see the test above): the
    poll answer whose AI draft was in flight comes back as draft_failed."""
    before["poll_response"][0]["status"] = "draft_failed"
    before["poll_response"][0]["updated_at"] = \
        after["poll_response"][0]["updated_at"]
    return after == before


def test_an_admin_purge_hides_her_writing_at_once_and_she_restores_it(
        app, world, stubs, monkeypatch):
    """Peter, 2026-10-13: "it should definitely work the same as user
    doing themselves". Hidden at once, for her and for bob; she sees the
    same deletion state and restores it herself."""
    _stub_dispatch(monkeypatch, stubs)
    a = world.alice.id
    gone_id = _deleted_before(world)
    before = _alice_snapshot(world)
    bob_before = _bob_snapshot(world)

    job = _admin_purge(app, world)
    assert job["status"] == "scheduled" and job["scope"] == "hidden"
    assert job["source"] == "admin" and job["undoable"] is True
    assert stubs.dispatched == []      # nothing runs before the 30 days
    _db.session.expire_all()
    for n in _alice_nodes(world):
        assert n.deleted_at is not None, n.id
    for model in HIDDEN_ROW_TABLES:
        assert model.query.filter_by(user_id=a).count() == 0, model
    assert writing_on_hold(a) is True
    assert _bob_snapshot(world) == bob_before

    # She sees the same state as after her own request: the status bar
    # and the Account page read it from here.
    c = _client(app, world.alice)
    state = c.get("/api/account/data").get_json()
    assert state["status"] == "scheduled" and state["restorable"] is True
    assert state["purge_at"] == job["scheduled_for"]
    assert state["grace_days"] == up.PURGE_GRACE_DAYS

    r = c.post("/api/account/data/restore")
    assert r.status_code == 200 and r.get_json()["status"] is None
    assert _same_as(before, _alice_snapshot(world))
    assert _db.session.get(Node, gone_id).deleted_at == T0
    done = UserDataPurge.query.one()
    assert done.status == "cancelled" and done.cancelled_by_id == a
    assert UserDataPurgeHidden.query.count() == 0
    assert writing_on_hold(a) is False


def test_the_admins_undo_restores_what_the_admin_purge_hid(
        app, world, stubs, monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    gone_id = _deleted_before(world)
    before = _alice_snapshot(world)
    job = _admin_purge(app, world)
    admin = _client(app, world.admin)

    # A stale page (another job id) undoes nothing.
    r = admin.post(f"/api/admin/users/{world.alice.id}/undo_deletion",
                   json={"job_id": job["id"] + 1})
    assert r.status_code == 404
    assert UserDataPurge.query.one().status == "scheduled"

    r = admin.post(f"/api/admin/users/{world.alice.id}/undo_deletion",
                   json={"job_id": job["id"]})
    assert r.status_code == 200, r.get_json()
    # The Users row has no waiting purge any more.
    assert r.get_json()["job"] is None
    assert _same_as(before, _alice_snapshot(world))
    assert _db.session.get(Node, gone_id).deleted_at == T0
    undone = UserDataPurge.query.one()
    assert undone.status == "cancelled"
    assert undone.cancelled_by_id == world.admin.id
    assert UserDataPurgeHidden.query.count() == 0
    assert up.deletion_status(world.alice.id)["status"] is None
    # Nothing left to undo, and the purge never starts.
    r = admin.post(f"/api/admin/users/{world.alice.id}/undo_deletion",
                   json={"job_id": job["id"]})
    assert r.status_code == 404
    later = datetime.utcnow() + timedelta(days=up.PURGE_GRACE_DAYS + 1)
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("undone"),
                                now=later) == []


def test_the_admin_cannot_undo_her_own_request(app, world, stubs):
    _ask(app, world)
    job = UserDataPurge.query.one()
    r = _client(app, world.admin).post(
        f"/api/admin/users/{world.alice.id}/undo_deletion",
        json={"job_id": job.id})
    assert r.status_code == 409 and r.get_json()["code"] == "users_own"
    assert up.undo_admin_purge(job.id, world.admin.id) is False
    _db.session.expire_all()
    assert _db.session.get(UserDataPurge, job.id).status == "scheduled"
    assert all(n.deleted_at is not None for n in _alice_nodes(world))
    assert UserProfile.query.filter_by(user_id=world.alice.id).count() == 0


def test_an_admin_purge_deletes_for_good_only_after_the_grace_period(
        app, world, stubs, monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    a = world.alice.id
    job = _db.session.get(UserDataPurge, _admin_purge(app, world)["id"])
    # Written after the admin's request: it stays, as after her own.
    n1 = _node(_db.session.get(User, a), owner=a, text="after the purge")
    _db.session.commit()
    n1_id = n1.id

    tokens = []
    almost = job.requested_at + timedelta(days=up.PURGE_GRACE_DAYS) \
        - timedelta(minutes=1)
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                                now=almost) == []
    assert _db.session.get(UserDataPurge, job.id).status == "scheduled"
    later = job.requested_at + timedelta(days=up.PURGE_GRACE_DAYS,
                                         minutes=1)
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                                now=later) == [job.id]
    # Started: neither the user nor the admin can bring it back now.
    assert _client(app, world.alice).post(
        "/api/account/data/restore").status_code == 409
    r = _client(app, world.admin).post(
        f"/api/admin/users/{a}/undo_deletion", json={"job_id": job.id})
    assert r.status_code == 409 and r.get_json()["code"] == "already_running"
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()
    left = {n.id for n in _alice_nodes(world)}
    tombstones = {world.ids[k] for k in ("A1", "A4", "A5")}
    assert left == {n1_id} | tombstones
    assert _db.session.get(Node, n1_id).deleted_at is None
    assert UserProfile.query.filter_by(user_id=a).count() == 0
    assert not any(f.exists() for f in world.files_alice)
    assert all(f.exists() for f in world.files_bob)
    assert UserDataPurgeHidden.query.count() == 0
    assert up.deletion_status(a)["status"] == "done"


def test_her_request_during_an_admins_purge_makes_it_hers(
        app, world, stubs, monkeypatch):
    """Two requests for the same user never make two jobs. Hers while the
    admin's waits adds what she wrote since to it (Peter, 2026-10-15) and
    makes it her own request: the admin's Undo would otherwise bring back
    writing she asked to delete herself (the user's edit wins). The
    admin's while hers waits is refused (test_user_purge.py)."""
    _stub_dispatch(monkeypatch, stubs)
    a = world.alice.id
    t1 = datetime.utcnow() + timedelta(minutes=1)
    _at(monkeypatch, t1)
    job = _admin_purge(app, world)
    since = _write_since(world, stubs, "since")
    t2 = t1 + timedelta(days=10)
    _at(monkeypatch, t2)
    c = _client(app, world.alice)
    r = c.delete("/api/account/data", json={"confirm": "alice"})
    assert r.status_code == 202
    assert r.get_json()["restorable"] is True
    assert UserDataPurge.query.count() == 1
    row = UserDataPurge.query.one()
    assert row.id == job["id"] and row.status == "scheduled"
    assert row.source == "self" and row.requested_by_id == a
    assert row.scheduled_for == t2 + timedelta(days=up.PURGE_GRACE_DAYS)
    assert _db.session.get(Node, since.node).deleted_at == t2

    admin = _client(app, world.admin)
    r = admin.get(f"/api/admin/users/{a}/purge_data")
    assert r.get_json()["job"]["undoable"] is False
    r = admin.post(f"/api/admin/users/{a}/undo_deletion",
                   json={"job_id": job["id"]})
    assert r.status_code == 409 and r.get_json()["code"] == "users_own"
    # A second admin request is refused the same way as before.
    r = admin.post(f"/api/admin/users/{a}/purge_data",
                   json={"confirm_username": "alice"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "already_scheduled"
    assert "already deleted by the user" in r.get_json()["error"]
    assert UserDataPurge.query.count() == 1

    # She restores both: what the admin hid and what she added.
    c = _client(app, world.alice)
    assert c.post("/api/account/data/restore").status_code == 200
    _db.session.expire_all()
    # Every node of hers is live again, apart from the one she deleted.
    assert {n.id for n in _alice_nodes(world)
            if n.deleted_at is not None} == {since.gone}
    assert _db.session.get(Node, since.node).deleted_at is None
    assert UserProfile.query.filter_by(user_id=a).count() == 2
    assert UserDataPurgeHidden.query.count() == 0


# ── "Delete all my writing" again while it waits (Peter, 2026-10-15) ───
# One job, one date (the grace period after the latest request), one
# restore: the second request hides what she wrote since under the same
# job.

def _at(monkeypatch, when):
    """The purge code's clock (requests, hides, dates)."""
    monkeypatch.setattr(up, "_now", lambda: when)


class _Since:
    pass


def _write_since(world, stubs, tag):
    """What alice writes or saves after a request: an entry with its
    recording and an AI reply, an entry she deleted herself, a todo
    list, a draft."""
    a = world.alice.id
    alice = _db.session.get(User, a)
    s = _Since()
    n = _node(alice, owner=a, text=f"{tag} entry")
    s.node = n.id
    s.reply = _node(world.llm, n, owner=a, node_type="llm",
                    text=f"{tag} reply").id
    s.gone = _node(alice, owner=a, text=f"{tag} deleted",
                   deleted_at=T0).id
    s.todo = _add(UserTodo(user_id=a, content=f"{tag} list",
                           generated_by="user")).id
    s.draft = _add(Draft(user_id=a, parent_id=n.id, content=f"{tag} draft",
                         session_id=f"sess-{tag}")).id
    _db.session.commit()
    s.file = _file(stubs.audio / f"user/{a}/node/{n.id}/original.webm")
    return s


def _records(job_id):
    return [(h.kind, h.row_id, h.path) for h in
            UserDataPurgeHidden.query.filter_by(job_id=job_id)]


def test_asking_again_hides_what_she_wrote_since_under_the_same_job(
        app, world, stubs, monkeypatch):
    a = world.alice.id
    t1 = datetime.utcnow() + timedelta(minutes=1)
    _at(monkeypatch, t1)
    c, body = _ask(app, world)
    job = UserDataPurge.query.one()
    first = _records(job.id)
    first_nodes = {r for (k, r, _) in first if k == "node"}
    due1 = t1 + timedelta(days=up.PURGE_GRACE_DAYS)
    assert job.scheduled_for == due1
    # Nothing new yet: the Account page offers no second request.
    assert body["new_writing"] is False
    assert c.get("/api/account/data").get_json()["new_writing"] is False

    since = _write_since(world, stubs, "since")
    # An entry the request hid that she brought back by re-importing it.
    _db.session.get(Node, world.ids["A8"]).deleted_at = None
    _db.session.commit()
    state = c.get("/api/account/data").get_json()
    assert state["new_writing"] is True and state["restorable"] is True

    t2 = t1 + timedelta(days=10)
    _at(monkeypatch, t2)
    c, body = _ask(app, world)
    # One job, one new date: the grace period after the second request.
    assert UserDataPurge.query.count() == 1
    job = UserDataPurge.query.one()
    due2 = t2 + timedelta(days=up.PURGE_GRACE_DAYS)
    assert job.scheduled_for == due2 and job.requested_at == t2
    assert job.status == "scheduled" and job.scope == "hidden"
    assert job.source == "self"
    assert body["purge_at"] == up.deletion_status(a)["purge_at"]
    assert body["purge_at"].startswith(due2.isoformat()[:16])
    assert body["restorable"] is True and body["new_writing"] is False

    # What she wrote since is hidden now; what she deleted herself keeps
    # its own date and is purged with it; the first batch keeps its date.
    _db.session.expire_all()
    assert _db.session.get(Node, since.node).deleted_at == t2
    assert _db.session.get(Node, since.reply).deleted_at == t2
    assert _db.session.get(Node, since.gone).deleted_at == T0
    assert _db.session.get(Node, world.ids["A8"]).deleted_at == t2
    for nid in first_nodes - {world.ids["A8"]}:
        assert _db.session.get(Node, nid).deleted_at == t1, nid
    assert UserTodo.query.filter_by(user_id=a).count() == 0
    assert Draft.query.filter_by(user_id=a).count() == 0
    # Recorded under the same job, nothing twice: the re-imported entry
    # keeps its one record.
    records = _records(job.id)
    assert len(records) == len(set(records))
    assert set(first) <= set(records)
    assert set(records) - set(first) == {
        ("node", since.node, None),
        ("node", since.reply, None),
        ("node_deleted", since.gone, None),
        ("user_todo", since.todo, None),
        ("draft", since.draft, None),
        ("file", None, f"audio:user/{a}/node/{since.node}"),
    }

    # The old date passes: nothing starts.
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("old date"),
                                now=due1 + timedelta(minutes=1)) == []
    assert _db.session.get(UserDataPurge, job.id).status == "scheduled"


def test_one_restore_brings_back_both(app, world, stubs, monkeypatch):
    gone_id = _deleted_before(world)
    before = _alice_snapshot(world)
    t1 = datetime.utcnow() + timedelta(minutes=1)
    _at(monkeypatch, t1)
    _ask(app, world)
    since = _write_since(world, stubs, "since")
    _at(monkeypatch, t1 + timedelta(days=10))
    c, _ = _ask(app, world)

    r = c.post("/api/account/data/restore")
    assert r.status_code == 200 and r.get_json()["status"] is None
    after = _alice_snapshot(world)
    # Every row of the first batch, and her user row, is as it was before
    # the first request.
    ids = {t: {row["id"] for row in rows} for t, rows in before.items()}
    first_after = {t: [row for row in after[t] if row["id"] in ids[t]]
                   for t in before}
    assert _same_as(before, first_after)
    assert _db.session.get(Node, gone_id).deleted_at == T0
    # What she wrote since is back too.
    for nid in (since.node, since.reply):
        assert _db.session.get(Node, nid).deleted_at is None
    assert _db.session.get(Node, since.gone).deleted_at == T0
    assert _db.session.get(UserTodo, since.todo) is not None
    assert _db.session.get(Draft, since.draft) is not None
    assert UserDataPurgeHidden.query.count() == 0
    assert UserDataPurge.query.one().status == "cancelled"
    assert writing_on_hold(world.alice.id) is False


def test_the_purge_deletes_both_and_keeps_what_came_after_the_second(
        app, world, stubs, monkeypatch):
    a = world.alice.id
    t1 = datetime.utcnow() + timedelta(minutes=1)
    _at(monkeypatch, t1)
    _ask(app, world)
    since = _write_since(world, stubs, "since")
    cost1 = _add(APICostLog(user_id=a, model_id="m",
                            request_type="conversation", cost_microdollars=5,
                            created_at=t1 + timedelta(days=1))).id
    _db.session.commit()
    t2 = t1 + timedelta(days=10)
    _at(monkeypatch, t2)
    _ask(app, world)
    job = UserDataPurge.query.one()

    # Written after the second request: it stays.
    alice = _db.session.get(User, a)
    later = _node(alice, owner=a, text="after the second")
    later_id = later.id
    todo2 = _add(UserTodo(user_id=a, content="newest list",
                          generated_by="user")).id
    cost2 = _add(APICostLog(user_id=a, model_id="m",
                            request_type="conversation", cost_microdollars=6,
                            created_at=t2 + timedelta(days=1))).id
    _db.session.commit()
    later_file = _file(stubs.audio / f"user/{a}/node/{later_id}/original.webm")

    tokens = []
    due1 = t1 + timedelta(days=up.PURGE_GRACE_DAYS)
    due2 = t2 + timedelta(days=up.PURGE_GRACE_DAYS)
    _at(monkeypatch, due2 + timedelta(minutes=1))
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                                now=due1 + timedelta(minutes=1)) == []
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                                now=due2 + timedelta(minutes=1)) == [job.id]
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()

    # Both batches are gone, apart from the entries other people replied
    # under (empty placeholders).
    tombstones = {world.ids[k] for k in ("A1", "A4", "A5")}
    assert {n.id for n in _alice_nodes(world)} == {later_id} | tombstones
    for nid in (since.node, since.reply, since.gone):
        assert _db.session.get(Node, nid) is None
    assert _db.session.get(UserTodo, since.todo) is None
    assert _db.session.get(Draft, since.draft) is None
    assert not since.file.exists()
    assert not any(f.exists() for f in world.files_alice)
    # What came after the second request stays, live.
    assert _db.session.get(Node, later_id).deleted_at is None
    assert [t.id for t in UserTodo.query.filter_by(user_id=a)] == [todo2]
    assert later_file.exists()
    assert all(f.exists() for f in world.files_bob)
    # Cost rows until the second request lose the name; the later stays.
    erased = User.query.filter_by(username=ERASED_SYSTEM_USERNAME).one()
    assert _db.session.get(APICostLog, cost1).user_id == erased.id
    assert [r.id for r in APICostLog.query.filter_by(user_id=a)] == [cost2]
    assert UserDataPurgeHidden.query.count() == 0
    assert up.deletion_status(a)["status"] == "done"


def test_asking_again_once_the_purge_has_started_is_refused(
        app, world, stubs):
    c, _ = _ask(app, world)
    job = UserDataPurge.query.one()
    since = _write_since(world, stubs, "since")
    records = _records(job.id)
    due = job.scheduled_for
    assert up.claim_job(job.id, "tok", now=due)

    r = c.delete("/api/account/data", json={"confirm": "alice"})
    assert r.status_code == 409 and r.get_json()["code"] == "already_running"
    assert "was not deleted" in r.get_json()["error"]
    _db.session.expire_all()
    job = _db.session.get(UserDataPurge, job.id)
    assert job.status == "running" and job.scheduled_for == due
    assert _records(job.id) == records
    assert _db.session.get(Node, since.node).deleted_at is None
    assert UserTodo.query.filter_by(user_id=world.alice.id).count() == 1
    assert up.deletion_status(world.alice.id)["new_writing"] is False


def test_a_claim_just_before_the_second_request_wins_and_hides_nothing(
        app, world, stubs, monkeypatch):
    """The beat claims the job after the request read it and before the
    request changed it. The request's conditional update finds the job
    running and hides nothing, so nothing is hidden that the running
    purge would not delete: what she wrote since stays, live."""
    c, _ = _ask(app, world)
    job = UserDataPurge.query.one()
    since = _write_since(world, stubs, "since")
    records = _records(job.id)
    real = up.active_job
    calls = []

    def read_then_claimed(user_id):
        found = real(user_id)
        if not calls:
            # The beat's claim lands now; the request still holds the
            # job as it read it (scheduled).
            UserDataPurge.query.filter_by(id=found.id).update(
                {"status": "running", "task_id": "beat",
                 "heartbeat_at": None}, synchronize_session=False)
        calls.append(user_id)
        return found
    monkeypatch.setattr(up, "active_job", read_then_claimed)

    r = c.delete("/api/account/data", json={"confirm": "alice"})
    assert r.status_code == 409 and r.get_json()["code"] == "already_running"
    _db.session.expire_all()
    assert _records(job.id) == records
    assert _db.session.get(Node, since.node).deleted_at is None
    assert _db.session.get(UserDataPurge, job.id).scheduled_for == \
        job.scheduled_for

    # The purge deletes the first batch only.
    monkeypatch.setattr(up, "active_job", real)
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                         now=job.scheduled_for + timedelta(minutes=1))
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()
    assert _db.session.get(Node, since.node).deleted_at is None
    assert _db.session.get(UserTodo, since.todo) is not None
    assert since.file.exists()


def test_a_restore_just_before_the_second_request_makes_it_a_new_one(
        app, world, stubs, monkeypatch):
    """She restores in one tab while the other asks again: the restore
    wins the job, and the request then hides everything as a new one."""
    c, _ = _ask(app, world)
    job = UserDataPurge.query.one()
    since = _write_since(world, stubs, "since")
    real = up.active_job
    calls = []

    def read_then_restored(user_id):
        found = real(user_id)
        if not calls:
            up.cancel_jobs(UserDataPurge.id == found.id,
                           cancelled_by_id=user_id)
        calls.append(user_id)
        return found
    monkeypatch.setattr(up, "active_job", read_then_restored)

    _ask(app, world)
    _db.session.expire_all()
    jobs = UserDataPurge.query.order_by(UserDataPurge.id).all()
    assert [j.status for j in jobs] == ["cancelled", "scheduled"]
    assert jobs[0].id == job.id
    assert UserDataPurgeHidden.query.filter_by(job_id=job.id).count() == 0
    for n in _alice_nodes(world):
        assert n.deleted_at is not None, n.id
    assert _db.session.get(Node, since.node).deleted_at is not None
    assert UserTodo.query.filter_by(user_id=world.alice.id).count() == 0


def test_the_account_page_offers_it_again_only_with_something_new(
        app, world, stubs):
    a = world.alice.id
    c, body = _ask(app, world)
    assert body["new_writing"] is False
    # A saved reference counts.
    _add(ExternalItem(user_id=a, source="web_clip", external_id="e-new",
                      content="clip"))
    _db.session.commit()
    assert c.get("/api/account/data").get_json()["new_writing"] is True
    _ask(app, world)
    assert c.get("/api/account/data").get_json()["new_writing"] is False
    # An entry she wrote and deleted again herself is nothing to show.
    _node(_db.session.get(User, a), owner=a, text="x", deleted_at=T0)
    _db.session.commit()
    assert c.get("/api/account/data").get_json()["new_writing"] is False
    # Not offered once there is nothing to restore.
    assert c.post("/api/account/data/restore").status_code == 200
    assert c.get("/api/account/data").get_json()["new_writing"] is False


def test_her_ai_reply_alone_offers_it_again(app, world, stubs):
    """She deletes an entry with "this entry only" while a deletion
    waits: the AI reply under it stays live and is hers
    (human_owner_id), so a second request would hide it."""
    a = world.alice.id
    c, _ = _ask(app, world)
    alice = _db.session.get(User, a)
    entry = _node(alice, owner=a, text="new entry")
    reply = _node(world.llm, entry, owner=a, node_type="llm",
                  text="new reply")
    entry_id, reply_id = entry.id, reply.id
    entry.deleted_at = T0
    _db.session.commit()
    assert _db.session.get(Node, reply_id).user_id != a
    assert c.get("/api/account/data").get_json()["new_writing"] is True
    _ask(app, world)
    assert _db.session.get(Node, reply_id).deleted_at is not None
    assert _db.session.get(Node, entry_id).deleted_at is not None
    assert c.get("/api/account/data").get_json()["new_writing"] is False


def test_hidden_rows_stay_out_of_counts_and_relationships(app, world, stubs):
    """A new request (a new session) sees none of them: no row by id, by
    relationship, in a count or on a page of results."""
    from sqlalchemy import func
    a, p1 = world.alice.id, world.P1.id
    _ask(app, world)
    _db.session.expunge_all()
    assert _db.session.query(func.count(UserProfile.id)).filter(
        UserProfile.user_id == a).scalar() == 0
    assert UserProfile.query.filter_by(user_id=a).count() == 0
    assert UserProfile.query.paginate(page=1, per_page=10,
                                      error_out=False).total == 1  # bob's
    assert _db.session.get(User, a).profiles == []
    assert _db.session.get(UserProfile, p1) is None
    with including_hidden_rows():
        assert _db.session.get(UserProfile, p1) is not None


# ── Review of the rework (comment 6088425994) ───────────────────────────

def test_the_purge_keeps_the_files_of_rows_it_keeps(app, world, stubs):
    """A reference saved again and an entry re-imported during the 30 days
    are the user's again: the purge keeps their files (Listen works),
    and deletes every other folder the request recorded."""
    a = world.alice.id
    a8_file = _file(stubs.audio / f"user/{a}/node/{world.ids['A8']}/original.webm")
    item_tts = stubs.audio / f"user/{a}/item/{world.I1.id}/tts.mp3"
    _ask(app, world)
    job = UserDataPurge.query.one()
    assert reclaim_external_items(a, ["e1"]) == 1
    _db.session.get(Node, world.ids["A8"]).deleted_at = None
    _db.session.commit()

    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                         now=job.scheduled_for + timedelta(minutes=1))
    assert up.run_purge_job(job.id, tokens[0]) == "done"

    assert item_tts.exists() and a8_file.exists()
    assert TTSChunk.query.filter_by(item_id=world.I1.id).count() == 1
    assert not any(f.exists() for f in world.files_alice if f != item_tts)
    assert all(f.exists() for f in world.files_bob)


def _poll_draft_module():
    """The real poll_draft module against stub glue (as test_updates does):
    backend.celery_app would boot the full app."""
    import sys
    from unittest.mock import MagicMock
    mod = sys.modules.get("backend.tasks.poll_draft")
    if mod is not None and not isinstance(mod, MagicMock):
        return mod
    saved = {k: sys.modules.get(k) for k in (
        "backend.celery_app", "backend.tasks.poll_draft")}
    sys.modules["backend.celery_app"] = MagicMock()
    sys.modules.pop("backend.tasks.poll_draft", None)
    import backend.tasks.poll_draft as mod
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    return mod


def test_a_poll_draft_collected_while_hidden_is_billed_and_saves_nothing(
        app, world, stubs, monkeypatch):
    """The batch with R1's AI draft ends after the request: its cost row
    is written, no draft is saved, and after a restore the answer is
    where she can write it or ask again ('draft_failed')."""
    mod = _poll_draft_module()
    monkeypatch.setattr(mod, "llm_cost_log_fields", lambda model_id, result, batch=False: {
        "cost_microdollars": 77, "input_tokens": 10, "output_tokens": 5})
    c, _ = _ask(app, world)
    r1 = world.R1.id

    mod._save_draft_result(
        {"custom_id": "p1", "response_id": r1, "poll_id": world.poll.id,
         "model_id": "m"},
        {"content": "A drafted answer", "input_tokens": 10,
         "output_tokens": 5})
    _db.session.commit()

    rows = APICostLog.query.filter_by(request_type="poll_draft").all()
    assert [r.cost_microdollars for r in rows] == [77]
    with including_hidden_rows():
        hidden = _db.session.get(PollResponse, r1)
        assert hidden.content is None and hidden.status == "draft_failed"
    assert c.post("/api/account/data/restore").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(PollResponse, r1).status == "draft_failed"


def test_a_poll_draft_queued_before_the_request_is_answerable_after_a_restore(
        app, world, stubs):
    """A draft submit queued before the request skips the hidden answer;
    the restore brings the answer back as 'draft_failed', not stuck in
    'drafting' (the Updates modal then offers writing it or asking
    again). Only an empty 'drafting' answer changes."""
    mod = _poll_draft_module()
    c, _ = _ask(app, world)
    r1 = world.R1.id
    mod._submit_poll_draft(r1)
    with including_hidden_rows():
        assert _db.session.get(PollResponse, r1).status == "drafting"
    assert c.post("/api/account/data/restore").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(PollResponse, r1).status == "draft_failed"


# ── Records left from the short profile description (#538) ──────────────
# Until #538 a request also recorded the user's short profile description
# (kind "user_description", row_id = the user). The description and its
# column are gone, but a job that was waiting at the deploy still has that
# record. Nothing reads the kind any more; the restore, the admin's Undo
# and the purge delete it with the job's other records.

def _old_description_record(job_id, user_id):
    _add(UserDataPurgeHidden(job_id=job_id, kind="user_description",
                             row_id=user_id))
    _db.session.commit()


def test_an_old_description_record_changes_nothing_she_sees(
        app, world, stubs):
    a = world.alice.id
    before = _alice_snapshot(world)
    c, _ = _ask(app, world)
    job = UserDataPurge.query.one()
    _old_description_record(job.id, a)

    # The Account page's state and the logs' counts read the job as before.
    state = c.get("/api/account/data").get_json()
    assert state["status"] == "scheduled" and state["restorable"] is True
    assert state["new_writing"] is False
    assert up.hidden_counts(job.id)["user_description"] == 1

    # Restore: everything comes back, and the old record goes too.
    r = c.post("/api/account/data/restore")
    assert r.status_code == 200 and r.get_json()["status"] is None
    assert _same_as(before, _alice_snapshot(world))
    assert UserDataPurgeHidden.query.count() == 0


def test_asking_again_with_an_old_description_record(
        app, world, stubs, monkeypatch):
    a = world.alice.id
    t1 = datetime.utcnow() + timedelta(minutes=1)
    _at(monkeypatch, t1)
    c, _ = _ask(app, world)
    job = UserDataPurge.query.one()
    _old_description_record(job.id, a)
    first = _records(job.id)
    since = _write_since(world, stubs, "since")
    _at(monkeypatch, t1 + timedelta(days=10))
    c, body = _ask(app, world)
    assert UserDataPurge.query.count() == 1
    assert body["new_writing"] is False
    records = _records(job.id)
    assert len(records) == len(set(records))
    assert set(first) <= set(records)
    assert ("node", since.node, None) in records
    assert [r for r in records if r[0] == "user_description"] == \
        [("user_description", a, None)]


def test_the_purge_with_an_old_description_record(app, world, stubs):
    a = world.alice.id
    _ask(app, world)
    job = UserDataPurge.query.one()
    _old_description_record(job.id, a)
    tokens = []
    later = job.scheduled_for + timedelta(minutes=1)
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                                now=later) == [job.id]
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()
    assert UserDataPurgeHidden.query.count() == 0
    assert up.deletion_status(a)["status"] == "done"
    scope = up.Scope(a, job.id, job.requested_at)
    assert up.leftovers(up.count_user_data(a, scope=scope)) == {}
    assert _db.session.get(User, a) is not None


def test_the_admin_views_and_undo_with_an_old_description_record(
        app, world, stubs, monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    a = world.alice.id
    before = _alice_snapshot(world)
    job = _admin_purge(app, world)
    _old_description_record(job["id"], a)
    admin = _client(app, world.admin)

    r = admin.get(f"/api/admin/users/{a}/purge_data")
    assert r.status_code == 200 and r.get_json()["job"]["id"] == job["id"]
    r = admin.get("/api/admin/users")
    assert r.status_code == 200
    row = next(u for u in r.get_json()["users"] if u["id"] == a)
    assert "description" not in row

    r = admin.post(f"/api/admin/users/{a}/undo_deletion",
                   json={"job_id": job["id"]})
    assert r.status_code == 200, r.get_json()
    assert _same_as(before, _alice_snapshot(world))
    assert UserDataPurgeHidden.query.count() == 0
