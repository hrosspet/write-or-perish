"""Circles: readers of a shared entry can play its original recording,
also after its author edited the text (Peter, 2026-11-23: "agreed - keep &
say so"). The behaviour stays; the share step says so. The edit form
learns whether the entry has a recording from GET /nodes/<id>/audience
(``has_recording``, for the entry's owner only).

Through the routes, with the LLM task module mocked."""
from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _client, _share, _db, Node,
)


def _audience(app, who, node_id):  # noqa: F811
    r = _client(app, who).get(f"/api/nodes/{node_id}/audience")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_the_edit_form_learns_whether_the_entry_has_a_recording(app):  # noqa: F811
    recorded = _share(app, names=["ben"], content="recorded, then edited")
    streamed = _share(app, names=["ben"], content="recorded live")
    typed = _share(app, names=["ben"], content="typed")
    _db.session.get(Node, recorded).audio_original_url = (
        "/media/nodes/1/recorded/original.webm")
    _db.session.get(Node, streamed).streaming_transcription = True
    _db.session.commit()
    assert _audience(app, "anna", recorded)["has_recording"] is True
    assert _audience(app, "anna", streamed)["has_recording"] is True
    assert _audience(app, "anna", typed)["has_recording"] is False
    # A reader is not told about the author's files here.
    assert _audience(app, "ben", recorded)["has_recording"] is False


def test_a_reader_is_let_to_the_recording_after_an_edit(app):  # noqa: F811
    """What the sentence says: the audio route lets a reader of the
    shared entry through as it lets its author, also after the text
    changed. No file is on disk in the test, so both get "no audio
    available"; someone outside the audience gets the node's not-found
    answer."""
    from backend.models import User
    entry = _share(app, names=["ben"], content="recorded words")
    _db.session.get(Node, entry).audio_original_url = (
        "/media/nodes/1/x/original.webm")
    for name in ("anna", "ben", "dana"):
        User.query.filter_by(username=name).first().plan = "pro"
    _db.session.commit()
    assert _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": "edited words"}).status_code == 200
    for who in ("anna", "ben"):
        r = _client(app, who).get(f"/api/nodes/{entry}/audio")
        assert r.status_code == 404, (who, r.get_json())
        assert r.get_json()["error"] == "No audio available for this node"
    r = _client(app, "dana").get(f"/api/nodes/{entry}/audio")
    assert r.status_code == 404
    assert r.get_json()["error"] == "Node not found"
