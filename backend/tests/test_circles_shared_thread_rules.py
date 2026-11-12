"""Circles under the shared-thread rules of 2026-11-12 (Peter, voice
review): "all AI replies in a public thread are private by default,
autogenerate is off in public threads and circles and can't be turned on,
and only the user can change an AI reply to that circle/public (after the
reply being generated and the user supposedly having read it and decided
it to share it)"; "If a user wants to publish a private node with a quote
of another private node, a dialog stating that this is not possible
should be displayed."

anna writes; ben and chidi read; dana is in nobody's circle. Through the
routes, with the LLM task module mocked: no model is ever called."""
from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _circle, _share, _reply, _can_read,
    _put, _db, Node, SECRET,
)


def _private(app, owner, content):  # noqa: F811
    r = _client(app, owner).post("/api/nodes/", json={
        "content": content, "privacy_level": "private", "ai_usage": "chat"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _ai_reply(app, who, parent, text=None):  # noqa: F811
    """*who*'s AI reply under *parent*, written (its task is mocked).
    Under someone else's node *who* first writes a reply of their own
    and asks under it (the public rule, Peter 2026-11-12)."""
    node = _db.session.get(Node, parent)
    if (node.human_owner_id or node.user_id) != _user(who).id:
        r = _reply(app, who, parent, f"{who}'s own reply")
        assert r.status_code == 201, r.get_json()
        parent = r.get_json()["id"]
    r = _client(app, who).post(f"/api/nodes/{parent}/llm", json={})
    assert r.status_code == 202, r.get_json()
    node = _db.session.get(Node, r.get_json()["node_id"])
    if text is not None:
        node.set_content(text)
    node.llm_task_status = "completed"
    _db.session.commit()
    return node.id


def _count():
    _db.session.expire_all()
    return Node.query.count()


# ── 1. A circle thread is a shared thread ───────────────────────────────

def test_every_circle_shape_counts_as_shared_and_an_only_me_branch_does_not(
        app):  # noqa: F811
    from backend.utils.llm_nodes import shared_with_others
    entry = _share(app, names=["ben", "chidi"])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    above = _private(app, "anna", "anna's earlier note")
    shared_with_path = _share(app, names=["ben"], parent_id=above)
    r = _client(app, "anna").put(f"/api/nodes/{shared_with_path}/audience",
                                 json={"circle_usernames": ["ben"],
                                       "share_path": True})
    assert r.status_code == 200, r.get_json()
    bens_ai = _ai_reply(app, "ben", entry)
    for nid in (entry, reply, above):
        assert shared_with_others(_db.session.get(Node, nid)), nid
    # ben's own AI reply, kept to himself, and a note of his under it:
    # nobody else reads them.
    note = _reply(app, "ben", bens_ai, "a note", privacy="private"
                  ).get_json()["id"]
    for nid in (bens_ai, note):
        assert not shared_with_others(_db.session.get(Node, nid)), nid


def test_no_automatic_reply_in_a_circle_thread_for_anyone(app):  # noqa: F811
    entry = _share(app, names=["ben"])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    hers = _reply(app, "anna", reply, "anna answers").get_json()["id"]
    before = _count()
    for who, nid in (("anna", entry), ("anna", hers), ("ben", reply)):
        r = _client(app, who).post(f"/api/nodes/{nid}/llm",
                                   json={"auto_generate": True})
        assert r.status_code == 409, (who, nid)
        assert r.get_json()["code"] == "auto_generate_off"
    # Under someone else's node nobody asks at all (the public rule).
    for who, nid in (("anna", reply), ("ben", entry)):
        r = _client(app, who).post(f"/api/nodes/{nid}/llm",
                                   json={"auto_generate": True})
        assert r.status_code == 403, (who, nid)
    assert _count() == before
    # LLM Response, the user's own request, works there.
    assert _client(app, "anna").post(
        f"/api/nodes/{entry}/llm", json={}).status_code == 202
    # Under ben's own Only-me AI reply nobody else reads: as in a private
    # branch under a public node, the automatic reply is not refused.
    bens_ai = _ai_reply(app, "ben", entry)
    r = _client(app, "ben").post(f"/api/nodes/{bens_ai}/llm",
                                 json={"auto_generate": True})
    assert r.status_code == 202, r.get_json()


def test_an_ai_reply_in_a_circle_thread_is_private_on_every_request(app):  # noqa: F811
    entry = _share(app, names=["ben", "chidi"])
    # Each under a node of their own (the public rule).
    own = {"anna": entry,
           "ben": _reply(app, "ben", entry, "ben replies").get_json()["id"]}
    for who in ("anna", "ben"):
        for body in ({}, {"privacy_level": "circles"},
                     {"privacy_level": "public"}):
            r = _client(app, who).post(f"/api/nodes/{own[who]}/llm",
                                       json=body)
            assert r.status_code == 202, (who, body)
            node = _db.session.get(Node, r.get_json()["node_id"])
            assert node.privacy_level == "private", (who, body)
            assert node.audience_root_id == entry
            readers = {"anna", "ben", "chidi"} - {who}
            for other in readers:
                assert not _can_read(app, other, node.id), (who, other)


def test_an_ai_reply_is_not_shared_while_it_is_written(app):  # noqa: F811
    entry = _share(app, names=["ben"])
    mine = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    aid = r.get_json()["node_id"]
    for call in (
            lambda: _put(app, "ben", aid, privacy_level="circles"),
            lambda: _client(app, "ben").put(
                f"/api/nodes/{aid}/audience", json={})):
        r = call()
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "reply_in_progress"
    assert _db.session.get(Node, aid).privacy_level == "private"
    assert not _can_read(app, "anna", aid)
    # Written: ben shares it with the thread.
    _db.session.get(Node, aid).llm_task_status = "completed"
    _db.session.commit()
    assert _put(app, "ben", aid, privacy_level="circles").status_code == 200
    assert _can_read(app, "anna", aid)


def test_an_entry_shared_with_its_audience_takes_no_ai_reply_along(app):  # noqa: F811
    """An AI reply made under a private entry stays private when the
    entry is shared later; the author shares it through Edit."""
    entry = _private(app, "anna", "anna writes")
    hers = _ai_reply(app, "anna", entry, "her AI reply")
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience",
                                 json={"circle_usernames": ["ben"]})
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "ben", entry)
    assert not _can_read(app, "ben", hers)
    assert _put(app, "anna", hers, privacy_level="circles").status_code == 200
    assert _can_read(app, "ben", hers)


# ── 3. A shared node quotes only what its audience can read ─────────────

def _refused(r, quoted_id):
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["code"] == "quotes_not_shared"
    assert "not all of them can read" in body["error"]
    assert [q["id"] for q in body["quoted"]] == [quoted_id]
    assert body["quoted"][0]["preview"]
    return body


def test_a_new_shared_entry_cannot_quote_a_private_entry(app):  # noqa: F811
    private = _private(app, "anna", "ANNAS PRIVATE NOTE")
    before = _count()
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": f"see {{quote:{private}}}", "privacy_level": "circles",
        "ai_usage": "chat", "circle_usernames": ["ben"]})
    body = _refused(r, private)
    assert body["quoted"][0]["preview"] == "ANNAS PRIVATE NOTE"
    assert _count() == before, "nothing is saved"
    # The check before saving answers the same, before anything is saved.
    r = _client(app, "anna").post("/api/circles/audience-preview", json={
        "circle_ids": [], "usernames": ["ben"],
        "content": f"see {{quote:{private}}}"})
    _refused(r, private)
    r = _client(app, "anna").post("/api/circles/audience-preview", json={
        "circle_ids": [], "usernames": ["ben"], "content": "no quote"})
    assert r.status_code == 200 and r.get_json()["readers"] == ["ben"]


def test_an_existing_entry_cannot_be_shared_while_it_quotes_a_private_entry(
        app):  # noqa: F811
    private = _private(app, "anna", "ANNAS PRIVATE NOTE")
    entry = _private(app, "anna", f"see {{quote:{private}}}")
    r = _put(app, "anna", entry, privacy_level="circles",
             circle_usernames=["ben"])
    _refused(r, private)
    node = _db.session.get(Node, entry)
    assert node.privacy_level == "private" and node.audience_root_id is None
    assert not _can_read(app, "ben", entry)
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience",
                                 json={"circle_usernames": ["ben"]})
    _refused(r, private)
    assert not _can_read(app, "ben", entry)
    # Shared first, the quoted entry lets the entry be shared too.
    assert _put(app, "anna", private, privacy_level="circles",
                circle_usernames=["ben"]).status_code == 200
    r = _put(app, "anna", entry, privacy_level="circles",
             circle_usernames=["ben"])
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "ben", entry)


def test_a_quote_of_an_entry_shared_with_the_same_circle_is_allowed(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben"])
    quoted = _share(app, circle_ids=[close], content="SHARED WITH CLOSE")
    entry = _share(app, circle_ids=[close],
                   content=f"more on {{quote:{quoted}}}")
    assert _can_read(app, "ben", entry)
    # The same entry for chidi too: chidi cannot read the quoted one.
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": f"more on {{quote:{quoted}}}", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [close],
        "circle_usernames": ["chidi"]})
    _refused(r, quoted)
    # A public quoted entry is read by everyone.
    public = _client(app, "anna").post("/api/nodes/", json={
        "content": "PUBLIC WORDS", "privacy_level": "public",
        "ai_usage": "chat"}).get_json()["id"]
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": f"see {{quote:{public}}}", "privacy_level": "circles",
        "ai_usage": "chat", "circle_usernames": ["chidi"]})
    assert r.status_code == 201, r.get_json()


def test_a_quote_it_already_had_is_checked_against_new_readers_only(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben"])
    quoted = _share(app, circle_ids=[close], content="SHARED WITH CLOSE")
    entry = _share(app, circle_ids=[close],
                   content=f"more on {{quote:{quoted}}}")
    # Making the quoted entry private later is not refused.
    assert _put(app, "anna", quoted, privacy_level="private").status_code == 200
    # An edit of the shared entry that adds nobody keeps its quotes.
    r = _put(app, "anna", entry, content=f"more on {{quote:{quoted}}} (edited)")
    assert r.status_code == 200, r.get_json()
    # A new reader must be able to read what it quotes.
    r = _put(app, "anna", entry, privacy_level="circles",
             circle_ids=[close], circle_usernames=["chidi"])
    _refused(r, quoted)
    assert not _can_read(app, "chidi", entry)
    # A new quote in its text is checked against everyone.
    other = _private(app, "anna", "ANOTHER PRIVATE NOTE")
    r = _put(app, "anna", entry,
             content=f"more on {{quote:{quoted}}} and {{quote:{other}}}")
    _refused(r, other)


def test_a_reply_in_a_shared_thread_quotes_only_what_the_thread_reads(app):  # noqa: F811
    entry = _share(app, names=["ben", "chidi"])
    his = _private(app, "ben", "BENS PRIVATE NOTE")
    before = _count()
    r = _reply(app, "ben", entry, f"as I wrote {{quote:{his}}}")
    _refused(r, his)
    assert _count() == before
    # The thread's own entry is read by everyone in it.
    r = _reply(app, "ben", entry, f"you wrote {{quote:{entry}}}")
    assert r.status_code == 201, r.get_json()


def test_an_ai_reply_shared_with_the_thread_follows_the_same_rule(app):  # noqa: F811
    entry = _share(app, names=["ben"])
    his = _private(app, "ben", "BENS PRIVATE NOTE")
    aid = _ai_reply(app, "ben", entry, f"as you wrote {{quote:{his}}}")
    r = _put(app, "ben", aid, privacy_level="circles")
    _refused(r, his)
    node = _db.session.get(Node, aid)
    assert node.privacy_level == "private"
    assert not _can_read(app, "anna", aid)


def test_the_discussion_and_the_replies_shared_along_are_checked(app):  # noqa: F811
    note = _private(app, "anna", "ANNAS PRIVATE NOTE")
    above = _private(app, "anna", f"earlier, {{quote:{note}}}")
    entry = _client(app, "anna").post("/api/nodes/", json={
        "content": "the entry", "privacy_level": "private",
        "ai_usage": "chat", "parent_id": above}).get_json()["id"]
    # Her reply below it, written while it was private, quotes the note.
    below = _client(app, "anna").post("/api/nodes/", json={
        "content": f"later, {{quote:{note}}}", "privacy_level": "private",
        "ai_usage": "chat", "parent_id": entry}).get_json()["id"]
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"], "share_path": True})
    _refused(r, note)
    assert not _can_read(app, "ben", entry)
    assert not _can_read(app, "ben", above)
    # Without the discussion the entry alone can be shared.
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"]})
    assert r.status_code == 200, r.get_json()
    # "Apply to my replies" would share the reply that quotes the note.
    r = _put(app, "anna", entry, privacy_level="circles",
             circle_usernames=["ben", "chidi"], apply_to_descendants=True)
    _refused(r, note)
    assert not _can_read(app, "ben", below)
    assert not _can_read(app, "chidi", entry)
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"], "apply_to_descendants": True})
    _refused(r, note)
    assert not _can_read(app, "ben", below)


def test_making_it_public_keeps_the_public_rule(app):  # noqa: F811
    quoted = _share(app, names=["ben"], content="SHARED WITH BEN")
    entry = _private(app, "anna", f"see {{quote:{quoted}}}")
    r = _put(app, "anna", entry, privacy_level="public")
    assert r.status_code == 409
    assert r.get_json()["code"] == "quotes_not_public"
