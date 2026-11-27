"""An account held as spam gets no bundle load and no final merge (#732).

Payments on, an account the admin marked as spam and did not approve by
hand is held (approval.held_as_spam, #713). Its Yes to "Start from your
tweets" is saved and kept, nothing is read from storage and no model is
called; a load under way stops before its next part and its merge before
the model call; the admin's "Make ready" refuses it; the beat's runs stop
like any run. Once the account is no longer held (the mark removed, or
Activate by hand), the person's next visit (GET /api/dashboard) sends the
kept Yes or the stopped load from where it was, and a final merge the hold
skipped is made then.

Reuses test_prefill_bundles.py's servers (a lab, a bucket directory,
production apps with no approval gate) and helpers. The tests marked
"control" pass on the base branch too.
"""
from datetime import datetime, timedelta

from backend.tests.test_prefill_bundles import (  # noqa: F401
    Node, PrefillBundleLoad, User, _bundle, _login, _run_until_done,
    _snapshot, _start, _to_the_merge, _watch_storage, _x_user, _yes, db,
    pb, pbl, pbm, world)


def _payments_on(app):
    app.config["PAYMENTS_ENABLED"] = True


def _let_in_user(**kw):
    """An X sign-in the gate let in at its first request (payments on)."""
    now = datetime.utcnow()
    return _x_user(approved=True, approved_at=now, let_in_at=now,
                   plan="free", **kw)


def _set_spam(user_id, value):
    db.session.query(User).filter_by(id=user_id).update({User.spam: value})
    db.session.commit()


def _visit(client, user_id):
    """The app's start: GET /api/dashboard, as both clients call it."""
    _login(client, user_id)
    resp = client.get("/api/dashboard/", query_string={"profile": "0"})
    assert resp.status_code == 200
    return resp.get_json()["user"]


def _loads(user_id):
    return PrefillBundleLoad.query.filter_by(user_id=user_id).order_by(
        PrefillBundleLoad.id).all()


def _straight_snapshot(world):
    """What a load nothing interrupted puts into the account."""
    with world.prod("straight").app_context():
        load, _ = _start("alice")
        assert _run_until_done(load.id) == "done"
        got = _snapshot(User.query.filter_by(twitter_id="1001").one().id)
    del world.dispatched[:]    # that server's ids are not the test's
    return got


# ── The Yes ─────────────────────────────────────────────────────────────

def test_a_held_accounts_yes_is_saved_and_reads_nothing(world, monkeypatch):
    """Never approved and marked as spam: the Yes is saved, nothing is
    sent, nothing is read, no model is called. The Yes is kept as an
    unsent check; a second Yes keeps no second one. Its visits while it
    is held change nothing."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _x_user(spam=True).id
        client = prod.test_client()
        _login(client, user_id)
        touched = _watch_storage(monkeypatch)
        resp = _yes(client)
        assert resp.status_code == 200
        assert resp.get_json()["user"]["starting_point"] is None
        assert db.session.get(User, user_id).prefill_consent == "yes"
        (kept,) = _loads(user_id)
        assert (kept.status, kept.stage, kept.error) == ("held", "check", "held_as_spam")
        _login(client, user_id)
        _yes(client)
        assert _visit(client, user_id)["starting_point"] is None
        assert len(_loads(user_id)) == 1
        assert world.dispatched == [] and world.merges == []
        assert touched == [] and world.calls == []
        assert db.session.get(User, user_id).approved is False


def test_a_check_sent_before_the_mark_reads_nothing(world, monkeypatch):
    """A Yes before the mark, its check run after it: kept unread."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _let_in_user(prefill_consent="yes").id
        check_id = pbl.maybe_start_self_load(db.session.get(User, user_id)).id
        _set_spam(user_id, True)
        touched = _watch_storage(monkeypatch)
        assert pbl.run_load(check_id, dispatch=lambda *a, **k: None) == "held"
        assert touched == [] and world.calls == []
        kept = db.session.get(PrefillBundleLoad, check_id)
        assert (kept.status, kept.stage, kept.error, kept.run_token) == (
            "held", "check", "held_as_spam", None)


def test_control_payments_off_the_spam_mark_holds_no_load(world, monkeypatch):
    """Payments off, the mark only hides the row (#713): a waiting
    account's Yes loads as before, and its Yes does not let it in."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        prod.config["PAYMENTS_ENABLED"] = False
        user_id = _x_user(spam=True).id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        assert _run_until_done(world.dispatched[0]) == "done"
        assert len(world.calls) == 1
        assert db.session.get(User, user_id).approved is False


# ── Unmarked: the next visit ─────────────────────────────────────────────

def test_unmarked_the_kept_yes_loads_at_the_next_visit(world, monkeypatch):
    """The mark removed: the next GET /api/dashboard sends the kept check
    (the same row), the person sees "checking", and the load runs as for
    any account: loaded, merged once, let in. Later visits send nothing."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _x_user(spam=True).id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        (kept,) = _loads(user_id)
        kept_id = kept.id
        assert kept.status == "held" and world.dispatched == []
        _set_spam(user_id, False)
        assert _visit(client, user_id)["starting_point"]["status"] == "checking"
        assert world.dispatched == [kept_id]
        assert _run_until_done(kept_id) == "done"
        load = db.session.get(PrefillBundleLoad, kept_id)
        assert (load.status, load.error, load.merge_status) == ("done", None, "done")
        assert len(world.calls) == 1
        user = db.session.get(User, user_id)
        assert user.approved is True and user.let_in_at is not None
        _visit(client, user_id)
        assert world.dispatched == [kept_id] and len(_loads(user_id)) == 1


def test_activate_by_hand_ends_the_hold_too(world, monkeypatch):
    """The admin's Activate wins over the mark (#713): the mark stays,
    the account is not held, and the next visit sends the kept check."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _x_user(spam=True).id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        (kept,) = _loads(user_id)
        kept_id = kept.id
        assert kept.status == "held" and world.dispatched == []
        user = db.session.get(User, user_id)
        user.approved, user.approved_at = True, datetime.utcnow()
        db.session.commit()
        _visit(client, user_id)
        assert world.dispatched == [kept_id]
        assert _run_until_done(kept_id) == "done"
        assert db.session.get(User, user_id).spam is True


def test_a_visit_with_no_kept_yes_reads_nothing(world, monkeypatch):
    """Control: an account with a Yes whose check found no bundle (the
    check is removed) or that never answered: a visit sends nothing."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        nobody = _let_in_user(username="bob", twitter_id="2002",
                              twitter_handle="bob", prefill_consent="yes").id
        check_id = pbl.maybe_start_self_load(db.session.get(User, nobody)).id
        assert pbl.run_load(check_id, dispatch=lambda *a, **k: None) == "no_bundle"
        silent = _let_in_user().id
        client = prod.test_client()
        sent = len(world.dispatched)
        touched = _watch_storage(monkeypatch)
        _visit(client, nobody)
        _visit(client, silent)
        assert len(world.dispatched) == sent and touched == []


# ── A load under way ─────────────────────────────────────────────────────

def test_a_load_under_way_stops_before_its_next_part(world, monkeypatch):
    """Marked as spam while a run loads parts: the run stops before the
    next part is read, the merge does not start, the person is not let
    in. Unmarked, the next visit sends the same load from where it was:
    nothing twice, the same account as a load nothing interrupted, and
    one merge."""
    _bundle(world, monkeypatch)
    straight = _straight_snapshot(world)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _let_in_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        load_id = world.dispatched[-1]
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None,
                            max_chunks=1) == "continuing"
        loaded = db.session.get(PrefillBundleLoad, load_id).nodes_done
        assert 0 < loaded < 7
        touched = _watch_storage(monkeypatch)
        real_chunk = pbl._node_chunk
        at_mark = []

        def chunk_then_marked(load, uid, *a, **k):
            more = real_chunk(load, uid, *a, **k)
            if not at_mark:
                _set_spam(uid, True)
                at_mark.append(len(touched))
            return more
        monkeypatch.setattr(pbl, "_node_chunk", chunk_then_marked)
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None) == "held"
        monkeypatch.setattr(pbl, "_node_chunk", real_chunk)
        assert len(touched) == at_mark[0]
        held = db.session.get(PrefillBundleLoad, load_id)
        assert (held.status, held.error, held.stage) == ("held", "held_as_spam", "nodes")
        assert held.merge_status is None
        assert world.merges == [] and world.calls == []
        # Still held: a run sent by anything reads nothing.
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None) == "busy"
        assert len(touched) == at_mark[0]
        _visit(client, user_id)
        assert world.dispatched == [load_id]
        _set_spam(user_id, False)
        _visit(client, user_id)
        assert world.dispatched == [load_id, load_id]
        assert _run_until_done(load_id) == "done"
        assert len(world.calls) == 1
        assert Node.query.filter_by(human_owner_id=user_id).count() == 7
        assert len(_loads(user_id)) == 1
        assert _snapshot(user_id)[0] == straight[0]


def test_a_load_a_no_cut_short_ends_as_the_no_ends_it(world, monkeypatch):
    """Yes, part of the load, No (the run links what it loaded), then the
    mark: the load ends stopped by the No, not held, so it can never
    finish as "done" with part of the entries. Unmarked, a Yes again
    loads the rest in a new load, as after any No."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _let_in_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        first = world.dispatched[-1]
        assert pbl.run_load(first, dispatch=lambda *a, **k: None,
                            max_chunks=1) == "continuing"
        _login(client, user_id)
        client.put("/api/dashboard/user", json={"prefill_consent": "no"})
        assert pbl.run_load(first, dispatch=lambda *a, **k: None,
                            max_chunks=1) == "continuing"
        cut = db.session.get(PrefillBundleLoad, first)
        assert (cut.stage, cut.error) == ("links", "prefill_declined")
        _set_spam(user_id, True)
        touched = _watch_storage(monkeypatch)
        assert pbl.run_load(first, dispatch=lambda *a, **k: None) == "stopped"
        assert touched == [] and world.calls == []
        stopped = db.session.get(PrefillBundleLoad, first)
        assert (stopped.status, stopped.error) == ("stopped", "prefill_declined")
        _set_spam(user_id, False)
        _visit(client, user_id)
        assert world.dispatched == [first]
        _login(client, user_id)
        _yes(client)
        second = world.dispatched[-1]
        assert second != first
        assert _run_until_done(second) == "done"
        assert Node.query.filter_by(human_owner_id=user_id).count() == 7


def test_a_load_waiting_for_its_merge_makes_no_call(world, monkeypatch):
    """Marked as spam while the load waits for its final merge: the merge
    is skipped before its model call (also when the mark lands while it
    reads its inputs), and the load is held. Unmarked, the next visit
    sends the load again and the merge is made once."""
    from backend.tasks import profile_windows as pw
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _let_in_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        load_id = world.dispatched[-1]
        _to_the_merge(load_id)
        real_cap = pw.merge_over_cap

        def marked_meanwhile(*a, **k):
            _set_spam(user_id, True)
            return real_cap(*a, **k)
        monkeypatch.setattr(pw, "merge_over_cap", marked_meanwhile)
        assert pbm.run_merge(load_id, dispatch=lambda *a, **k: None) == "skipped"
        monkeypatch.setattr(pw, "merge_over_cap", real_cap)
        load = db.session.get(PrefillBundleLoad, load_id)
        assert (load.merge_status, load.merge_error) == ("skipped", "held_as_spam")
        assert world.calls == []
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None) == "held"
        _set_spam(user_id, False)
        _visit(client, user_id)
        assert world.dispatched[-1] == load_id
        resumed = db.session.get(PrefillBundleLoad, load_id)
        assert (resumed.status, resumed.merge_status) == ("queued", None)
        assert _run_until_done(load_id) == "done"
        done = db.session.get(PrefillBundleLoad, load_id)
        assert (done.merge_status, done.error) == ("done", None)
        assert len(world.calls) == 1


def test_a_done_loads_merge_the_hold_skipped_is_made_after_it(world, monkeypatch):
    """The person was let in with the loaded profile while the merge went
    on in the background; then marked as spam: the merge's next attempt
    makes no call. Unmarked, the next visit makes the merge due again."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _let_in_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        load_id = world.dispatched[-1]
        _to_the_merge(load_id)
        db.session.query(PrefillBundleLoad).filter_by(id=load_id).update(
            {PrefillBundleLoad.merge_started_at:
                datetime.utcnow() - pbm.MERGE_WAIT - timedelta(seconds=1)})
        db.session.commit()
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None) == "done"
        _set_spam(user_id, True)
        assert pbm.run_merge(load_id, dispatch=lambda *a, **k: None) == "skipped"
        assert world.calls == []
        merges = len(world.merges)
        _visit(client, user_id)
        assert len(world.merges) == merges
        _set_spam(user_id, False)
        _visit(client, user_id)
        assert world.merges[-1] == load_id and len(world.merges) == merges + 1
        assert pbm.run_merge(load_id, dispatch=lambda *a, **k: None) == "done"
        assert len(world.calls) == 1
        _visit(client, user_id)
        assert len(world.merges) == merges + 1


# ── The admin and the beat ───────────────────────────────────────────────

def test_make_ready_refuses_a_held_account_before_reading_storage(world, monkeypatch):
    """The admin's "Make ready" by X id: refused before the manifest is
    read, forced or not; the Check button names the hold. Activated by
    hand, the same account can be made ready."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _x_user(spam=True, last_seen_at=None).id
        touched = _watch_storage(monkeypatch)
        for force in (False, True):
            load, refusal = _start("1001", force=force, merge=True)
            assert load is None and refusal[0] == "held_as_spam"
        assert touched == [] and _loads(user_id) == []
        checked = pbl.check("1001")
        assert checked["refusal"]["code"] == "held_as_spam"
        assert checked["refusal"]["forceable"] is False
        # By handle: one read of the handle index, nothing of the bundle.
        del touched[:]
        assert _start("alice")[1][0] == "held_as_spam"
        assert touched == ["read_bytes"]
        user = db.session.get(User, user_id)
        user.approved, user.approved_at = True, datetime.utcnow()
        db.session.commit()
        load, refusal = _start("1001", force=True)
        assert refusal is None
        assert _run_until_done(load.id) == "done"


def test_an_admin_load_under_way_is_held_too(world, monkeypatch):
    """A load the admin started, marked as spam meanwhile: held, the merge
    not started. Unmarked, the admin makes it ready again; entries already
    there are not loaded twice."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        load, _ = _start("alice", merge=True)
        assert pbl.run_load(load.id, dispatch=lambda *a, **k: None,
                            max_chunks=1) == "continuing"
        _set_spam(load.user_id, True)
        assert pbl.run_load(load.id, dispatch=lambda *a, **k: None) == "held"
        assert world.merges == [] and world.calls == []
        _set_spam(load.user_id, False)
        again, refusal = _start("alice", merge=True)
        assert refusal is None
        assert _run_until_done(again.id) == "done"
        assert Node.query.filter_by(human_owner_id=load.user_id).count() == 7
        assert len(world.calls) == 1


def test_the_beat_stops_a_held_accounts_load_and_merge(world, monkeypatch):
    """The beat sends again a load whose run died and a merge whose
    worker died: for a held account the run stops before reading and the
    merge before its call. Neither is sent again afterwards."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        _payments_on(prod)
        user_id = _let_in_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        load_id = world.dispatched[-1]
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None,
                            max_chunks=1) == "continuing"
        _set_spam(user_id, True)
        stale = datetime.utcnow() - pbl.STALE_AFTER - timedelta(minutes=1)
        db.session.query(PrefillBundleLoad).filter_by(id=load_id).update(
            {PrefillBundleLoad.heartbeat_at: stale})
        db.session.commit()
        assert pbl.stalled_load_ids() == [load_id]
        touched = _watch_storage(monkeypatch)
        assert pbl.run_load(load_id, dispatch=lambda *a, **k: None) == "held"
        assert touched == [] and pbl.stalled_load_ids() == []
    # A merge whose worker died, for an account held since.
    prod2 = world.prod("prod2")
    with prod2.app_context():
        _payments_on(prod2)
        load, _ = _start("alice", merge=True)
        _to_the_merge(load.id)
        _set_spam(load.user_id, True)
        db.session.query(PrefillBundleLoad).filter_by(id=load.id).update(
            {PrefillBundleLoad.merge_status: "running",
             PrefillBundleLoad.merge_attempts: 1,
             PrefillBundleLoad.merge_heartbeat_at:
                 datetime.utcnow() - pbm.MERGE_STALE - timedelta(minutes=1)})
        db.session.commit()
        assert pbm.stalled_merge_ids() == [load.id]
        assert pbm.run_merge(load.id, dispatch=lambda *a, **k: None) == "skipped"
        assert db.session.get(PrefillBundleLoad, load.id).merge_error == "held_as_spam"
        assert world.calls == [] and pbm.stalled_merge_ids() == []
