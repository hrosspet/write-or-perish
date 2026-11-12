"""Circles: who may write into a shared thread, and the quote check for
sharing read from the audience lists alone.

* What a person wrote into someone else's shared thread is read by the
  thread's people; its writer may change it, or put it back there, only
  while they read the thread themselves. Otherwise they may take it back
  to Only me. What they wrote stays readable to the remaining members as
  written (removal closes the thread to the person, not their words to
  the thread).
* The check that a shared node quotes only what its readers can read
  answers from the reader lists the pages show: whatever any reader chose
  to hide for themselves, the answer is the same.

anna writes; ben and chidi read; dana is in nobody's circle. Through the
routes, with the LLM task module mocked: no model is ever called."""
from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _circle, _share, _reply, _can_read,
    _put, _db, Node, SECRET,
)


def _private(app, owner, content, parent_id=None):  # noqa: F811
    r = _client(app, owner).post("/api/nodes/", json={
        "content": content, "privacy_level": "private", "ai_usage": "chat",
        "parent_id": parent_id})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _text(node_id):
    _db.session.expire_all()
    return _db.session.get(Node, node_id).get_content()


def _mute(app, who, author):  # noqa: F811
    r = _client(app, who).post("/api/circles/mutes", json={"username": author})
    assert r.status_code == 200, r.get_json()


def _unmute(app, who, author):  # noqa: F811
    assert _client(app, who).delete(
        f"/api/circles/mutes/{author}").status_code == 200


def _closed(r):
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["code"] == "circle_thread_closed"


# ── Writing into someone else's thread ──────────────────────────────────

def test_a_member_still_in_the_thread_edits_his_reply(app):  # noqa: F811
    entry = _share(app, names=["ben", "chidi"])
    rid = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    assert _put(app, "ben", rid, content="ben replies, edited").status_code == 200
    assert _put(app, "ben", rid, content="again", privacy_level="circles"
                ).status_code == 200
    assert _client(app, "chidi").get(f"/api/nodes/{rid}").get_json()[
        "content"] == "again"


def test_removed_from_the_circle_his_reply_stays_as_written(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    rid = _reply(app, "ben", entry, "BEN AS WRITTEN").get_json()["id"]
    assert _client(app, "anna").delete(
        f"/api/circles/{close}/members/ben").status_code == 200
    _closed(_put(app, "ben", rid, content="BEN CHANGED LATER"))
    _closed(_put(app, "ben", rid, content="BEN CHANGED LATER",
                 privacy_level="circles"))
    # The remaining members read his reply as he wrote it.
    assert _text(rid) == "BEN AS WRITTEN"
    assert _client(app, "chidi").get(f"/api/nodes/{rid}").get_json()[
        "content"] == "BEN AS WRITTEN"
    # He may take it back to Only me, and change it then.
    assert _put(app, "ben", rid, privacy_level="private").status_code == 200
    assert not _can_read(app, "chidi", rid)
    assert _put(app, "ben", rid, content="mine now",
                privacy_level="private").status_code == 200
    # Back into the thread: refused, through Edit and through /audience.
    _closed(_put(app, "ben", rid, privacy_level="circles"))
    _closed(_client(app, "ben").put(f"/api/nodes/{rid}/audience", json={}))
    assert not _can_read(app, "chidi", rid)


def test_dropped_as_a_named_reader_he_cannot_change_the_thread(app):  # noqa: F811
    entry = _share(app, names=["ben", "chidi"])
    rid = _reply(app, "ben", entry, "BEN AS WRITTEN").get_json()["id"]
    assert _put(app, "anna", entry, privacy_level="circles",
                circle_usernames=["chidi"]).status_code == 200
    _closed(_put(app, "ben", rid, content="BEN CHANGED LATER"))
    assert _text(rid) == "BEN AS WRITTEN"


def test_with_circles_off_for_him_he_cannot_change_the_thread(app):  # noqa: F811
    entry = _share(app, names=["ben", "chidi"])
    rid = _reply(app, "ben", entry, "BEN AS WRITTEN").get_json()["id"]
    app.config["CIRCLES_USER_IDS"] = app.config["CIRCLES_USER_IDS"] - {
        _user("ben").id}
    _closed(_put(app, "ben", rid, content="BEN CHANGED LATER"))
    _closed(_put(app, "ben", rid, content="BEN CHANGED LATER",
                 privacy_level="circles"))
    assert _text(rid) == "BEN AS WRITTEN"
    assert _put(app, "ben", rid, privacy_level="private").status_code == 200


def test_taken_back_before_removal_it_cannot_go_back_after(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    rid = _reply(app, "ben", entry, "BEN AS WRITTEN").get_json()["id"]
    assert _put(app, "ben", rid, privacy_level="private").status_code == 200
    assert _client(app, "anna").delete(
        f"/api/circles/{close}/members/ben").status_code == 200
    _closed(_put(app, "ben", rid, content="NEW WORDS", privacy_level="circles"))
    _closed(_client(app, "ben").put(f"/api/nodes/{rid}/audience", json={}))
    assert not _can_read(app, "chidi", rid)
    assert _db.session.get(Node, rid).privacy_level == "private"


def test_an_ai_reply_closed_to_its_owner_is_not_his_to_change(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    # Under his own reply in the thread (the public rule).
    mine = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    aid = r.get_json()["node_id"]
    node = _db.session.get(Node, aid)
    node.set_content("BENS AI REPLY")
    node.llm_task_status = "completed"
    _db.session.commit()
    assert _put(app, "ben", aid, privacy_level="circles").status_code == 200
    assert _client(app, "anna").delete(
        f"/api/circles/{close}/members/ben").status_code == 200
    # Closed to him (as GET answers): no edit of any kind.
    assert _client(app, "ben").get(f"/api/nodes/{aid}").status_code == 404
    assert _put(app, "ben", aid, content="CHANGED").status_code == 404
    assert _put(app, "ben", aid, content="CHANGED",
                privacy_level="circles").status_code == 404
    assert _client(app, "ben").put(f"/api/nodes/{aid}/audience",
                                   json={}).status_code == 404
    assert _text(aid) == "BENS AI REPLY"
    assert _client(app, "chidi").get(f"/api/nodes/{aid}").get_json()[
        "content"] == "BENS AI REPLY"


def test_with_circles_off_for_everyone_an_old_reply_edits_as_on_main(app):  # noqa: F811
    """After Circles was on and is off again, nobody but its writer reads
    a reply left "circles": its edits are as on main, a quote of his own
    private note included."""
    entry = _share(app, names=["ben"])
    rid = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    his = _private(app, "ben", "BENS PRIVATE NOTE")
    app.config["CIRCLES_V1"] = False
    app.config["CIRCLES_USER_IDS"] = set()
    r = _put(app, "ben", rid, content=f"now with {{quote:{his}}}")
    assert r.status_code == 200, r.get_json()
    r = _put(app, "ben", rid, content=f"again {{quote:{his}}}",
             privacy_level="circles")
    assert r.status_code == 200, r.get_json()


# ── The quote check reads the audience lists, not anyone's mutes ────────

def _same_with_and_without(app, who, author, call):  # noqa: F811
    """The status and body of *call* with *who* hiding *author* equal
    those without; returns the answer."""
    _db.session.rollback()
    without = call()
    _mute(app, who, author)
    with_mute = call()
    _unmute(app, who, author)
    assert (without.status_code, without.get_json()) == (
        with_mute.status_code, with_mute.get_json())
    return without


def test_the_author_does_not_learn_that_a_reader_hid_her(app):  # noqa: F811
    quoted = _share(app, names=["ben"], content="SHARED WITH BEN")
    preview = _same_with_and_without(
        app, "ben", "anna", lambda: _client(app, "anna").post(
            "/api/circles/audience-preview", json={
                "circle_ids": [], "usernames": ["ben"],
                "content": f"more on {{quote:{quoted}}}"}))
    assert preview.status_code == 200
    # The save gives the same answer whatever ben chose.
    _mute(app, "ben", "anna")
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": f"more on {{quote:{quoted}}}", "privacy_level": "circles",
        "ai_usage": "chat", "circle_usernames": ["ben"]})
    assert r.status_code == 201, r.get_json()


def test_a_refusal_reads_the_same_with_and_without_a_mute(app):  # noqa: F811
    quoted = _share(app, names=["ben"], content="SHARED WITH BEN")
    _share(app, names=["chidi"], content="FOR CHIDI")   # chidi may hide anna
    preview = _same_with_and_without(
        app, "chidi", "anna", lambda: _client(app, "anna").post(
            "/api/circles/audience-preview", json={
                "circle_ids": [], "usernames": ["ben", "chidi"],
                "content": f"more on {{quote:{quoted}}}"}))
    assert preview.status_code == 409
    assert preview.get_json()["code"] == "quotes_not_shared"


def test_a_member_does_not_learn_of_another_members_mute(app):  # noqa: F811
    """chidi hid anna: a reply ben writes in her thread quoting its own
    entry is fine for ben, as without the mute."""
    entry = _share(app, names=["ben", "chidi"])
    _mute(app, "chidi", "anna")
    r = _reply(app, "ben", entry, f"you wrote {{quote:{entry}}}")
    assert r.status_code == 201, r.get_json()
    _unmute(app, "chidi", "anna")
    r = _reply(app, "ben", entry, f"again {{quote:{entry}}}")
    assert r.status_code == 201, r.get_json()


# ── Sharing the discussion; replies carried along ───────────────────────

def test_a_new_entry_with_the_discussion_checks_the_discussion(app):  # noqa: F811
    note = _private(app, "anna", "ANNAS PRIVATE NOTE")
    root = _private(app, "anna", f"earlier, {{quote:{note}}}")
    before = Node.query.count()
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": "the entry", "privacy_level": "circles", "ai_usage": "chat",
        "circle_usernames": ["ben"], "parent_id": root, "share_path": True})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["code"] == "quotes_not_shared"
    assert [q["id"] for q in r.get_json()["quoted"]] == [note]
    _db.session.expire_all()
    assert Node.query.count() == before
    assert not _can_read(app, "ben", root)
    # The check before saving gives the same answer, with the parent.
    r = _client(app, "anna").post("/api/circles/audience-preview", json={
        "circle_ids": [], "usernames": ["ben"], "content": "the entry",
        "parent_id": root, "share_path": True})
    assert r.status_code == 409 and r.get_json()["code"] == "quotes_not_shared"
    r = _client(app, "anna").post("/api/circles/audience-preview", json={
        "circle_ids": [], "usernames": ["ben"], "content": "the entry",
        "parent_id": root})
    assert r.status_code == 200
    # Without the discussion the entry is shared alone.
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": "the entry", "privacy_level": "circles", "ai_usage": "chat",
        "circle_usernames": ["ben"], "parent_id": root})
    assert r.status_code == 201, r.get_json()


def test_an_ai_usage_change_is_not_refused_for_a_quote_its_readers_keep(app):  # noqa: F811
    """A reply in her thread quotes her entry X; X is narrowed later (not
    refused). Changing the entry's AI usage for her replies, with the
    same audience sent along as the form does, asks nothing of the
    reply's quote: its readers do not change."""
    x = _share(app, names=["ben", "chidi"], content="X FOR BOTH")
    entry = _share(app, names=["ben", "chidi"])
    reply = _client(app, "anna").post("/api/nodes/", json={
        "content": f"see {{quote:{x}}}", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": entry}).get_json()["id"]
    assert _can_read(app, "chidi", reply)
    assert _put(app, "anna", x, privacy_level="circles",
                circle_usernames=["ben"]).status_code == 200
    r = _put(app, "anna", entry, privacy_level="circles",
             circle_usernames=["ben", "chidi"], ai_usage="train",
             apply_to_descendants=True)
    assert r.status_code == 200, r.get_json()
    assert _db.session.get(Node, reply).ai_usage == "train"
    # A person added to the audience must read the quote: refused.
    r = _put(app, "anna", entry, privacy_level="circles",
             circle_usernames=["ben", "chidi", "dana"], ai_usage="chat",
             apply_to_descendants=True)
    assert r.status_code == 409 and r.get_json()["code"] == "quotes_not_shared"


def test_the_discussion_waits_for_an_ai_reply_still_written(app):  # noqa: F811
    root = _private(app, "anna", "anna thinks")
    r = _client(app, "anna").post(f"/api/nodes/{root}/llm", json={})
    pending = r.get_json()["node_id"]
    entry = _private(app, "anna", "the entry", parent_id=pending)
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["code"] == "reply_in_progress"
    assert _db.session.get(Node, pending).privacy_level == "private"
    assert not _can_read(app, "ben", pending)
    assert not _can_read(app, "ben", entry)
    # Written, it is shared with the discussion.
    _db.session.get(Node, pending).llm_task_status = "completed"
    _db.session.commit()
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "ben", pending)
