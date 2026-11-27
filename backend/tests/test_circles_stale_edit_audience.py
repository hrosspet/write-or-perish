"""Circles with #799: an edit form opened on an older text may not widen
who reads the newer one.

An edit Save sends ``base_content``, the text its form was opened on.
When that is no longer the entry's text, the Save may still narrow who
reads the entry but not widen it (routes/nodes._widens_access). A shared
entry stays at the level "circles" when its audience changes, so the
level alone does not tell: a stale Save that gives the entry a reader it
did not have is refused the same way (409 ``content_changed``), and
nothing is saved.

anna writes; ben, chidi and dana read. Through the routes, with the LLM
task module mocked: no model is ever called.
"""
from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _share, _can_read, _put, _db, Node,
)


def _edit_elsewhere(app, entry, text):  # noqa: F811
    """The entry's text changes in another tab (its form was current)."""
    old = _db.session.get(Node, entry).get_content()
    r = _put(app, "anna", entry, content=text, base_content=old)
    assert r.status_code == 200, r.get_json()


def test_a_stale_form_may_not_add_a_reader(app):  # noqa: F811
    e = _share(app, names=["ben"], content="E")
    _edit_elsewhere(app, e, "E, newer")
    r = _put(app, "anna", e, content="E", base_content="E",
             privacy_level="circles", circle_ids=[],
             circle_usernames=["ben", "chidi"],
             circle_usernames_from=["ben"])
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["code"] == "content_changed"
    assert not _can_read(app, "chidi", e)
    assert _can_read(app, "ben", e)
    assert _db.session.get(Node, e).get_content() == "E, newer"


def test_a_stale_form_may_take_a_reader_off(app):  # noqa: F811
    e = _share(app, names=["ben", "chidi"], content="E")
    _edit_elsewhere(app, e, "E, newer")
    r = _put(app, "anna", e, content="E", base_content="E",
             privacy_level="circles", circle_ids=[],
             circle_usernames=["ben"],
             circle_usernames_from=["ben", "chidi"])
    assert r.status_code == 200, r.get_json()
    assert not _can_read(app, "chidi", e)
    assert _can_read(app, "ben", e)
    # Settings only: the newer text stays.
    assert _db.session.get(Node, e).get_content() == "E, newer"


def test_a_stale_form_that_keeps_the_audience_saves_its_settings(app):  # noqa: F811
    e = _share(app, names=["ben"], content="E")
    _edit_elsewhere(app, e, "E, newer")
    r = _put(app, "anna", e, content="E", base_content="E",
             privacy_level="circles", ai_usage="none", circle_ids=[],
             circle_usernames=["ben"], circle_usernames_from=["ben"])
    assert r.status_code == 200, r.get_json()
    node = _db.session.get(Node, e)
    assert (node.ai_usage, node.get_content()) == ("none", "E, newer")
    assert _can_read(app, "ben", e)


def test_a_current_form_may_add_a_reader(app):  # noqa: F811
    e = _share(app, names=["ben"], content="E")
    r = _put(app, "anna", e, content="E", base_content="E",
             privacy_level="circles", circle_ids=[],
             circle_usernames=["ben", "dana"],
             circle_usernames_from=["ben"])
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "dana", e)
