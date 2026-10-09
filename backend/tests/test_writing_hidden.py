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
    reclaim_description, reclaim_external_items, shown_description,
    writing_on_hold,
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

    # The description shows empty, to her and to others; it is not lost.
    alice = _db.session.get(User, a)
    assert shown_description(alice) == ""
    assert alice.description == "I write about birds"

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
    assert shown_description(_db.session.get(User, world.alice.id)) == \
        "I write about birds"
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
    # profile she wrote herself, a new description, a cost row.
    alice = _db.session.get(User, a)
    n1 = _node(alice, owner=a, text="a fresh start")
    nl = _node(world.llm, n1, owner=a, node_type="llm", text="a reply")
    todo = _add(UserTodo(user_id=a, content="new list", generated_by="user"))
    draft = _add(Draft(user_id=a, parent_id=n1.id, content="new draft",
                       session_id="sess-new"))
    item = _add(ExternalItem(user_id=a, source="web_clip", external_id="e-new",
                             content="new clip"))
    prof = _add(UserProfile(user_id=a, content="mine", generated_by="user"))
    reclaim_description(a)
    alice.description = "starting over"
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
    assert _db.session.get(User, a).description == "starting over"
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


def test_an_admin_bringing_the_request_forward_deletes_everything(
        app, world, stubs, monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    a = world.alice.id
    c, _ = _ask(app, world)
    n1 = _node(_db.session.get(User, a), owner=a, text="after the request")
    _db.session.commit()
    r = _client(app, world.admin).post(f"/api/admin/users/{a}/purge_data",
                                       json={"confirm_username": "alice"})
    assert r.status_code == 202
    job = UserDataPurge.query.one()
    assert job.scope == "all" and job.status == "running"
    # No restore once the admin brought it forward.
    assert _client(app, world.alice).post(
        "/api/account/data/restore").status_code == 409
    n1_id = n1.id
    assert up.run_purge_job(job.id, stubs.dispatched[0][1]) == "done"
    _db.session.expunge_all()
    assert Node.query.filter_by(id=n1_id).first() is None
    assert UserDataPurgeHidden.query.count() == 0


def test_an_admin_purge_cannot_be_restored_by_the_user(app, world, stubs):
    up.schedule_purge(world.alice, requested_by_id=world.admin.id,
                      source="admin", at=datetime.utcnow())
    assert not up.cancel_purge(world.alice.id, world.alice.id)
    assert UserDataPurge.query.one().status == "scheduled"
    assert up.deletion_status(world.alice.id)["restorable"] is False
    # An admin purge hides nothing ahead: it runs at once.
    assert UserDataPurgeHidden.query.count() == 0


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
