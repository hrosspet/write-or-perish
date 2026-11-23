"""Circles: a reader leaves (Peter, 2026-11-23: "build leave into the
current PR").

A person can be put into a circle, or named for an entry, without being
asked. Leaving takes them out: from an entry's page, the circles through
which they read it and their name on it; from the list of people they
hid, everything of that person's that holds them. Afterwards they get the
answers a removed reader gets, nobody sees their name among the readers,
and the author's own lists no longer show them.

anna writes; ben, chidi and dana read; eve has no Circles flag. Through
the routes, with the LLM task module mocked: no model is ever called.
"""
from datetime import datetime, timedelta

import pytest

from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _circle, _share, _reply, _can_read,
    _shared_ids, _put, _notes, _db, Node, NodeCircle, NodeReader,
    CircleMember,
)


def _preview(app, who, **body):  # noqa: F811
    return _client(app, who).post("/api/circles/leave/preview", json=body)


def _leave(app, who, **body):  # noqa: F811
    return _client(app, who).post("/api/circles/leave", json=body)


def _members(app, owner, cid):  # noqa: F811
    circles = _client(app, owner).get("/api/circles").get_json()["circles"]
    return [m["username"] for c in circles if c["id"] == cid
            for m in c["members"]]


def _readers(app, who, node_id):  # noqa: F811
    return _client(app, who).get(
        f"/api/nodes/{node_id}").get_json()["audience"]["readers"]


def test_a_reader_leaves_the_circle_an_entry_came_through(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    e1 = _share(app, circle_ids=[close], content="first")
    e2 = _share(app, circle_ids=[close], content="second")
    e3 = _share(app, names=["ben"], content="for ben by name")
    reply = _reply(app, "ben", e1, "ben was here").get_json()["id"]

    # The dialog's numbers: both entries of the circle close, not e3.
    r = _preview(app, "ben", entry_id=e1)
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"author": "anna", "closes": 2}
    # The preview changes nothing.
    assert _can_read(app, "ben", e1) and _can_read(app, "ben", e2)

    r = _leave(app, "ben", entry_id=e1)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["left"] is True

    # The answers a removed reader gets.
    assert not _can_read(app, "ben", e1)
    assert not _can_read(app, "ben", e2)
    assert _can_read(app, "ben", e3)
    assert _shared_ids(app, "ben") == [e3]
    assert {n["link"] for n in _notes(app, "ben")} == {f"/node/{e3}"}
    # Nobody sees his name among the readers; anna's circle no longer
    # lists him.
    assert _readers(app, "chidi", e1) == ["chidi"]
    assert _readers(app, "anna", e2) == ["chidi"]
    assert _members(app, "anna", close) == ["chidi"]
    # What he wrote in the thread stays there for its people, with his
    # name; he can no longer change it there, as after removal.
    shown = _client(app, "chidi").get(f"/api/nodes/{reply}").get_json()
    assert shown["user"]["username"] == "ben"
    r = _put(app, "ben", reply, content="changed after leaving")
    assert r.status_code == 403
    assert r.get_json()["code"] == "circle_thread_closed"
    assert _put(app, "ben", reply, privacy_level="private").status_code == 200
    # Nothing is left to leave there.
    assert _leave(app, "ben", entry_id=e1).status_code == 404
    # anna can add him again; the circle's entries open to him again.
    assert _client(app, "anna").post(f"/api/circles/{close}/members", json={
        "username": "ben", "include_existing": True}).status_code == 201
    assert _can_read(app, "ben", e1) and _can_read(app, "ben", e2)


def test_a_reader_named_for_one_entry_leaves_it(app):  # noqa: F811
    e = _share(app, names=["ben", "chidi"], content="named")
    other = _share(app, names=["ben"], content="another named")
    r = _preview(app, "ben", entry_id=e)
    assert r.get_json() == {"author": "anna", "closes": 1}
    assert _leave(app, "ben", entry_id=e).status_code == 200
    assert not _can_read(app, "ben", e)
    assert _can_read(app, "ben", other)
    assert NodeReader.query.filter_by(
        node_id=e, user_id=_user("ben").id).count() == 0
    # anna's picker is seeded without him, and the readers list drops him.
    mine = _client(app, "anna").get(f"/api/nodes/{e}").get_json()["audience"]
    assert mine["usernames"] == ["chidi"]
    assert mine["readers"] == ["chidi"]


def test_leaving_keeps_a_circle_through_which_the_entry_is_not_read(app):  # noqa: F811
    """ben is in Close (all entries) and in Work with "Only new entries"
    from after E was shared with both: he reads E through Close only, so
    leaving from E leaves Close and keeps Work."""
    close = _circle(app, "anna", "Close", ["ben"])
    work = _circle(app, "anna", "Work", ["chidi"])
    e = _share(app, circle_ids=[close, work], content="E")
    NodeCircle.query.filter_by(node_id=e).update(
        {"shared_at": datetime.utcnow() - timedelta(hours=1)})
    _db.session.commit()
    assert _client(app, "anna").post(f"/api/circles/{work}/members", json={
        "username": "ben", "include_existing": False}).status_code == 201
    later = _share(app, circle_ids=[work], content="later, for Work")
    r = _preview(app, "ben", entry_id=e)
    assert r.get_json() == {"author": "anna", "closes": 1}
    assert _leave(app, "ben", entry_id=e).status_code == 200
    assert not _can_read(app, "ben", e)
    assert _can_read(app, "ben", later)
    assert _members(app, "anna", close) == []
    assert _members(app, "anna", work) == ["ben", "chidi"]


def test_a_reader_who_hid_the_author_leaves_her_from_the_hidden_list(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    e1 = _share(app, circle_ids=[close], content="circle entry")
    e2 = _share(app, names=["ben", "chidi"], content="named entry")
    ben = _client(app, "ben")
    assert ben.post("/api/circles/mutes",
                    json={"username": "anna"}).status_code == 200
    # Hidden, but still named to the others: the list offers leaving.
    data = ben.get("/api/circles/shared-with-me").get_json()
    assert data["hidden"] == ["anna"]
    assert data["hidden_leavable"] == ["anna"]
    assert _readers(app, "chidi", e1) == ["ben", "chidi"]
    r = _preview(app, "ben", username="@anna")
    assert r.get_json() == {"author": "anna", "closes": 2}
    assert _leave(app, "ben", username="anna").status_code == 200
    assert _readers(app, "chidi", e1) == ["chidi"]
    assert _readers(app, "chidi", e2) == ["chidi"]
    assert _members(app, "anna", close) == ["chidi"]
    data = ben.get("/api/circles/shared-with-me").get_json()
    assert data["hidden"] == ["anna"]          # still hidden
    assert data["hidden_leavable"] == []
    # Showing her again opens nothing: he is in none of her audiences.
    assert ben.delete("/api/circles/mutes",
                      json={"username": "anna"}).status_code == 200
    assert not _can_read(app, "ben", e1) and not _can_read(app, "ben", e2)
    assert _shared_ids(app, "ben") == []


def test_leaving_a_hidden_person_takes_every_place_they_gave(app):  # noqa: F811
    """Leaving a person the reader hid (not one entry) takes him out of
    each of her circles and off each entry that names him."""
    close = _circle(app, "anna", "Close", ["ben"])
    work = _circle(app, "anna", "Work", ["ben", "chidi"])
    _share(app, circle_ids=[close], content="close")
    _share(app, circle_ids=[work], content="work")
    _share(app, names=["ben"], content="named")
    assert _client(app, "ben").post(
        "/api/circles/mutes", json={"username": "anna"}).status_code == 200
    assert _leave(app, "ben", username="anna").status_code == 200
    ben_id = _user("ben").id
    assert CircleMember.query.filter_by(user_id=ben_id).count() == 0
    assert NodeReader.query.filter_by(user_id=ben_id).count() == 0
    assert _shared_ids(app, "ben") == []
    assert _members(app, "anna", work) == ["chidi"]


def test_nothing_to_leave_gets_one_answer(app):  # noqa: F811
    """Leaving is no way to test a name or an entry: the author on her own
    entry, a person outside the audience, a missing entry, a name nobody
    has and a person who never shared anything get the same answer."""
    e = _share(app, names=["ben"], content="for ben")
    private = _client(app, "anna").post("/api/nodes/", json={
        "content": "private", "privacy_level": "private",
        "ai_usage": "chat"}).get_json()["id"]
    answers = set()
    for who, body in (("anna", {"entry_id": e}),
                      ("dana", {"entry_id": e}),
                      ("ben", {"entry_id": private}),
                      ("ben", {"entry_id": 999999}),
                      ("ben", {"entry_id": "x"}),
                      ("ben", {"username": "nobody"}),
                      ("ben", {"username": "dana"}),
                      ("ben", {"username": "ben"}),
                      ("ben", {})):
        for call in (_preview, _leave):
            r = call(app, who, **body)
            assert r.status_code == 404, (who, body, r.get_json())
            answers.add(str(r.get_json()))
    assert len(answers) == 1
    assert _can_read(app, "ben", e)
    # Without the Circles flag the routes are not there.
    assert _leave(app, "eve", entry_id=e).status_code == 404
    assert _leave(app, "eve", username="anna").status_code == 404


def _answers(app, who, **body):  # noqa: F811
    out = []
    for call in (_preview, _leave):
        r = call(app, who, **body)
        assert r.status_code == 404, (who, body, r.get_json())
        out.append(r.get_json())
    return out


def test_leave_by_name_is_offered_for_hidden_people_only(app):  # noqa: F811
    """The apps offer leaving a person only in the list of people the
    reader hid. Any other name gets the answer a name that never shared
    anything gets, and nothing changes."""
    nobody = _answers(app, "ben", username="nobody")
    # ben reads anna's entry and has not hidden her.
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close], content="for Close")
    assert _answers(app, "ben", username="anna") == nobody
    assert _can_read(app, "ben", e)
    # dana is in a circle of anna's that nothing is shared with.
    _circle(app, "anna", "Quiet", ["dana"])
    assert _answers(app, "dana", username="anna") == nobody
    # chidi's "Only new entries" cut-off keeps everything closed to him.
    NodeCircle.query.filter_by(node_id=e).update(
        {"shared_at": datetime.utcnow() - timedelta(hours=1)})
    _db.session.commit()
    assert _client(app, "anna").post(f"/api/circles/{close}/members", json={
        "username": "chidi", "include_existing": False}).status_code == 201
    assert not _can_read(app, "chidi", e)
    assert _answers(app, "chidi", username="anna") == nobody
    # Nobody left anything.
    assert _members(app, "anna", close) == ["ben", "chidi"]
    assert CircleMember.query.filter_by(user_id=_user("dana").id).count() == 1


def _deactivate(app, name):  # noqa: F811
    user = _user(name)
    user.approved = False
    user.deactivated_at = datetime.utcnow()


def _hold_as_spam(app, name):  # noqa: F811
    app.config["PAYMENTS_ENABLED"] = True
    user = _user(name)
    user.spam = True
    user.let_in_at = datetime.utcnow()


def _start_deletion(app, name):  # noqa: F811
    _user(name).deleted_at = datetime.utcnow()      # its grace period


def _take_back(app, name):  # noqa: F811
    entry = Node.query.filter_by(user_id=_user(name).id).one()
    assert _put(app, name, entry.id, privacy_level="private").status_code == 200


@pytest.mark.parametrize("close_it", [
    _deactivate, _hold_as_spam, _start_deletion, _take_back])
def test_leave_needs_an_entry_the_reader_can_open(app, close_it):  # noqa: F811
    """Leaving from an entry asks the read routes' own question first: an
    entry every read route answers 404 for answers 404 here too, and
    nothing changes."""
    e = _share(app, names=["ben", "chidi"], content="E")
    missing = _answers(app, "ben", entry_id=999999)
    close_it(app, "anna")
    _db.session.commit()
    try:
        assert not _can_read(app, "ben", e)
        assert _answers(app, "ben", entry_id=e) == missing
        rows = NodeReader.query.filter_by(node_id=e).count()
        assert rows == (0 if close_it is _take_back else 2)
    finally:
        app.config.pop("PAYMENTS_ENABLED", None)


def test_a_reply_after_leaving_is_refused_like_after_removal(app):  # noqa: F811
    e = _share(app, names=["ben", "chidi"], content="E")
    assert _leave(app, "ben", entry_id=e).status_code == 200
    r = _reply(app, "ben", e, "one more")
    assert r.status_code == 404, r.get_json()
    assert Node.query.filter_by(parent_id=e).count() == 0
