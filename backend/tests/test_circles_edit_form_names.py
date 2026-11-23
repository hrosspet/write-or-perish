"""Circles: a person who left is not named again by an edit form that was
opened before they left.

The edit form is filled with the names an entry is shared with, and its
save sends them back. It also sends the names it started with
(``circle_usernames_from``): a person named then and not any more (they
left, or were taken off in another form) is left off by the save. A form
opened afterwards starts without them, so the author names them again by
typing their name.

anna writes; ben, chidi and dana read. Through the routes, with the LLM
task module mocked: no model is ever called.
"""
from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _share, _can_read, _put,
)


def _leave(app, who, entry_id):  # noqa: F811
    r = _client(app, who).post("/api/circles/leave",
                               json={"entry_id": entry_id})
    assert r.status_code == 200, r.get_json()


def _readers(app, who, node_id):  # noqa: F811
    return _client(app, who).get(
        f"/api/nodes/{node_id}").get_json()["audience"]["readers"]


def _notes_about(app, who, node_id):  # noqa: F811
    from backend.models import UserNotification
    return UserNotification.query.filter_by(
        user_id=_user(who).id, node_id=node_id).count()


def test_an_edit_form_opened_before_a_reader_left_does_not_name_him_again(app):  # noqa: F811
    e = _share(app, names=["ben", "chidi"], content="E")
    started = ["ben", "chidi"]                 # the form opens
    _leave(app, "ben", e)
    # A typo fixed in the open form: the audience it sends is unchanged.
    r = _put(app, "anna", e, content="E, fixed", privacy_level="circles",
             circle_ids=[], circle_usernames=started,
             circle_usernames_from=started)
    assert r.status_code == 200, r.get_json()
    assert not _can_read(app, "ben", e)
    assert _readers(app, "anna", e) == ["chidi"]
    assert _notes_about(app, "ben", e) == 1      # only the first one
    # The same form adds dana (the second step's route): she is named,
    # ben still is not.
    r = _client(app, "anna").put(f"/api/nodes/{e}/audience", json={
        "circle_ids": [], "circle_usernames": started + ["dana"],
        "circle_usernames_from": started})
    assert r.status_code == 200, r.get_json()
    assert _readers(app, "anna", e) == ["chidi", "dana"]
    assert not _can_read(app, "ben", e)


def test_the_author_names_a_person_who_left_again_from_a_new_form(app):  # noqa: F811
    e = _share(app, names=["ben", "chidi"], content="E")
    _leave(app, "ben", e)
    now = _client(app, "anna").get(
        f"/api/nodes/{e}/audience").get_json()["audience"]["usernames"]
    assert now == ["chidi"]                    # the new form starts here
    r = _put(app, "anna", e, privacy_level="circles", circle_ids=[],
             circle_usernames=now + ["ben"], circle_usernames_from=now)
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "ben", e)
    assert _readers(app, "anna", e) == ["ben", "chidi"]


def test_an_open_form_whose_only_reader_left_shares_with_nobody(app):  # noqa: F811
    e = _share(app, names=["ben"], content="E")
    _leave(app, "ben", e)
    r = _put(app, "anna", e, content="E, fixed", privacy_level="circles",
             circle_ids=[], circle_usernames=["ben"],
             circle_usernames_from=["ben"])
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_no_readers"
    assert not _can_read(app, "ben", e)
