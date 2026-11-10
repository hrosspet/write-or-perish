"""Circles and deletion: "Delete all my writing" (#268), account deletion
(#269) and the daily cleanup of deleted nodes remove a user's circles,
memberships, the entries they were named for, mutes and notes, and close
their shared entries at once.

Reuses the fixture world of test_user_purge.py (alice has a row in every
table the purge touches, bob is the control), with foreign keys ON, so a
row left pointing at a deleted node or account fails here as on Postgres.
"""
from datetime import datetime, timedelta

import pytest

from backend.tests.test_user_purge import (  # noqa: F401 - fixtures
    _add, _db, _node, _run_job, app, stubs, world,
)
from backend.models import (
    Circle, CircleMember, CircleMute, Node, NodeCircle, NodeReader, User,
    UserNotification,
)
from backend.utils import account_deletion as acc
from backend.utils import user_purge as up

CIRCLE_MODELS = (Circle, CircleMember, NodeCircle, NodeReader, CircleMute)


@pytest.fixture
def circles(app, world):
    """alice and bob share with each other, both ways, in every table."""
    app.config["CIRCLES_V1"] = True
    a, b = world.alice, world.bob
    carol = _add(User(username="carol", approved=True))

    class C:
        pass
    c = C()
    c.carol = carol
    # alice's circle (bob, carol) and an entry shared with it and by name.
    c.alices = _add(Circle(user_id=a.id, name="Close friends"))
    _add(CircleMember(circle_id=c.alices.id, user_id=b.id))
    _add(CircleMember(circle_id=c.alices.id, user_id=carol.id))
    c.entry = _node(a, owner=a.id, privacy="circles", text="alice shares")
    c.entry.audience_root_id = c.entry.id
    _add(NodeCircle(node_id=c.entry.id, circle_id=c.alices.id))
    _add(NodeReader(node_id=c.entry.id, user_id=b.id))
    c.bobs_reply = _node(b, c.entry, owner=b.id, privacy="circles",
                         text="bob replies")
    _add(UserNotification(user_id=b.id, type="circle_share", title="t",
                          node_id=c.entry.id, actor_user_id=a.id))
    # bob's circle (alice, carol) and an entry shared with alice.
    c.bobs = _add(Circle(user_id=b.id, name="Bob's people"))
    _add(CircleMember(circle_id=c.bobs.id, user_id=a.id))
    _add(CircleMember(circle_id=c.bobs.id, user_id=carol.id))
    c.bobs_entry = _node(b, owner=b.id, privacy="circles", text="bob shares")
    c.bobs_entry.audience_root_id = c.bobs_entry.id
    _add(NodeCircle(node_id=c.bobs_entry.id, circle_id=c.bobs.id))
    _add(NodeReader(node_id=c.bobs_entry.id, user_id=a.id))
    _add(UserNotification(user_id=a.id, type="circle_share", title="t",
                          node_id=c.bobs_entry.id, actor_user_id=b.id))
    # Mutes both ways.
    _add(CircleMute(user_id=a.id, muted_user_id=carol.id))
    _add(CircleMute(user_id=carol.id, muted_user_id=a.id))
    _db.session.commit()
    c.ids = {"entry": c.entry.id, "bobs_entry": c.bobs_entry.id,
             "bobs_reply": c.bobs_reply.id, "alices": c.alices.id,
             "bobs": c.bobs.id}
    return c


def _nothing_of_alice_is_left(world, circles):
    a = world.alice.id
    assert Circle.query.filter_by(user_id=a).count() == 0
    assert CircleMember.query.filter_by(user_id=a).count() == 0
    assert CircleMember.query.filter_by(
        circle_id=circles.ids["alices"]).count() == 0
    assert NodeCircle.query.filter_by(
        circle_id=circles.ids["alices"]).count() == 0
    assert NodeReader.query.filter_by(user_id=a).count() == 0
    assert NodeReader.query.filter_by(node_id=circles.ids["entry"]).count() == 0
    assert CircleMute.query.filter(
        (CircleMute.user_id == a) | (CircleMute.muted_user_id == a)
    ).count() == 0
    assert UserNotification.query.filter(
        (UserNotification.user_id == a)
        | (UserNotification.actor_user_id == a)).count() == 0
    # bob's own circle, his other member and his entry are untouched.
    assert _db.session.get(Circle, circles.ids["bobs"]) is not None
    assert [m.user_id for m in CircleMember.query.filter_by(
        circle_id=circles.ids["bobs"])] == [circles.carol.id]
    assert NodeCircle.query.filter_by(
        node_id=circles.ids["bobs_entry"]).count() == 1
    assert _db.session.get(Node, circles.ids["bobs_entry"]) is not None


def test_the_purge_removes_everything_of_circles(world, stubs, circles):
    dry = up.count_user_data(world.alice.id)
    for key in ("circle", "circle_member", "node_circle", "node_reader",
                "circle_mute", "circle_note", "node_audience"):
        assert dry[key] > 0, key
    _, outcome = _run_job()
    assert outcome == "done"
    _db.session.expire_all()
    assert up.leftovers(up.count_user_data(world.alice.id)) == {}
    _nothing_of_alice_is_left(world, circles)
    # Her entry stays as an empty placeholder for bob's reply, closed.
    entry = _db.session.get(Node, circles.ids["entry"])
    assert entry is not None and entry.content is None
    assert entry.deleted_at is not None
    assert _db.session.get(Node, circles.ids["bobs_reply"]) is not None


def test_delete_all_my_writing_closes_shared_entries_at_once(
        app, world, stubs, circles):
    from backend.utils.privacy import can_user_access_node
    a, b = world.alice, world.bob
    entry = _db.session.get(Node, circles.ids["entry"])
    assert can_user_access_node(entry, b.id)
    job, _ = up.schedule_purge(a, requested_by_id=a.id, source="self")
    _db.session.commit()
    _db.session.expire_all()
    entry = _db.session.get(Node, circles.ids["entry"])
    assert not can_user_access_node(entry, b.id)
    assert not can_user_access_node(entry, circles.carol.id)

    tokens = []
    after = datetime.utcnow() + timedelta(days=30, minutes=1)
    assert up.dispatch_due_jobs(
        lambda j, t: tokens.append(t), now=after) == [job.id]
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    _db.session.expire_all()
    _nothing_of_alice_is_left(world, circles)
    # The account stays.
    assert _db.session.get(User, a.id) is not None


def test_account_deletion_removes_everything_of_circles(
        app, world, stubs, circles):
    from backend.utils.privacy import can_user_access_node
    a, b = world.alice, world.bob
    a_id = a.id
    job = acc.schedule_account_deletion(
        a, requested_by_id=a.id, source="self")
    _db.session.expire_all()
    # Hidden at once, both ways: her entry is closed, and she is no
    # longer listed among the readers of bob's.
    entry = _db.session.get(Node, circles.ids["entry"])
    assert not can_user_access_node(entry, b.id)
    from backend.utils.circles import readers_of
    assert a_id not in {u.id for u in readers_of(
        _db.session.get(Node, circles.ids["bobs_entry"]))}

    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                         now=datetime.utcnow() + timedelta(days=31))
    assert tokens, "the due job was not claimed"
    assert up.run_purge_job(job.id, tokens[-1]) == "done"
    _db.session.expire_all()
    assert _db.session.get(User, a_id) is None
    assert acc.references_to_user(a_id) == {}
    for model in CIRCLE_MODELS:
        for col in model.__table__.columns:
            if any(fk.column.table.name == "user" for fk in col.foreign_keys):
                assert model.query.filter(col == a_id).count() == 0, col
    assert UserNotification.query.filter_by(actor_user_id=a_id).count() == 0
    assert _db.session.get(Circle, circles.ids["bobs"]) is not None


def test_the_daily_cleanup_deletes_a_shared_entrys_rows(
        app, world, stubs, circles):
    from backend.tasks.node_cleanup import _full_purge
    reply = _db.session.get(Node, circles.ids["bobs_reply"])
    _full_purge(reply)
    _db.session.commit()
    entry = _db.session.get(Node, circles.ids["entry"])
    entry.deleted_at = datetime.utcnow()
    _full_purge(entry)
    _db.session.commit()          # foreign keys are ON: nothing may point at it
    assert _db.session.get(Node, circles.ids["entry"]) is None
    assert NodeCircle.query.filter_by(node_id=circles.ids["entry"]).count() == 0
    assert NodeReader.query.filter_by(node_id=circles.ids["entry"]).count() == 0
    assert UserNotification.query.filter_by(
        node_id=circles.ids["entry"]).count() == 0
    # The circle and its people are not the entry's to delete.
    assert CircleMember.query.filter_by(
        circle_id=circles.ids["alices"]).count() == 2
