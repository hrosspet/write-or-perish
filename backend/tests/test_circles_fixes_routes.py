"""Circles through the routes: one test per fix of the third review of
#631. anna writes; ben and chidi read. The LLM task module is mocked: no
model is ever called."""
import time

from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _share, _reply, _can_read, _db, Node,
    User, SECRET,
)


def _add(parent, content, owner="anna", **kw):
    """A node written straight into the database (what the session and
    recording routes make)."""
    who = _user(owner)
    n = Node(user_id=kw.pop("user_id", who.id),
             human_owner_id=kw.pop("human_owner_id", who.id),
             parent_id=parent, privacy_level=kw.pop("privacy_level", "private"),
             ai_usage="chat", node_type=kw.pop("node_type", "user"), **kw)
    n.set_content(content)
    _db.session.add(n)
    _db.session.commit()
    return n.id


def _llm_user():
    llm = User.query.filter_by(username="gpt-5").first()
    if llm is None:
        llm = User(username="gpt-5", twitter_id="llm-gpt-5")
        _db.session.add(llm)
        _db.session.commit()
    return llm


def _node(node_id):
    _db.session.expire_all()
    return _db.session.get(Node, node_id)


# ── 1. The agreement, on the route ──────────────────────────────────────

def test_a_public_agentic_prompt_needs_the_agreement_on_the_route(app):  # noqa: F811
    from backend.utils import circles as cz
    # anna's Text-mode thread from a public prompt; her turn in it is
    # shared with ben through Edit.
    prompt = _add(None, "PUBLIC-PROMPT {user_profile}",
                  privacy_level="public", prompt_key="textmode")
    entry = _add(prompt, SECRET)
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["ben"]})
    assert r.status_code == 200, r.get_json()
    ben = _client(app, "ben")
    # ben asks under his own reply in the thread (the public rule).
    mine = _reply(app, "ben", entry, "BENS REPLY").get_json()["id"]
    audience = ben.get(f"/api/nodes/{mine}").get_json()["audience"]
    assert audience["ai_reads"] == [cz.TOOLS_ONLY_NAME]
    key = audience["ai_reads_key"]
    assert key
    r = ben.post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 409
    assert r.get_json()["code"] == cz.AI_READS_CHANGED_CODE
    assert r.get_json()["ai_reads_key"] == key
    r = ben.post(f"/api/nodes/{mine}/llm", json={"ai_reads_ack": key})
    assert r.status_code == 202, r.get_json()


# ── 5. Deleting a shared entry keeps the author's own nodes ─────────────

def test_deleting_the_shared_entry_gives_the_path_back_to_its_author(app):  # noqa: F811
    from backend.tasks.node_cleanup import finalize_deleted_nodes
    from backend.utils.privacy import can_user_access_node
    llm = _llm_user()
    prompt = _add(None, "THE-PROMPT", prompt_key="textmode")
    turn = _add(prompt, "ANNAS-FIRST-TURN")
    her_ai = _add(turn, "ANNAS-AI-REPLY", user_id=llm.id, node_type="llm",
                  llm_model="gpt-5")
    entry = _add(her_ai, SECRET)
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    path = (prompt, turn, her_ai)
    for nid in path:
        assert _can_read(app, "ben", nid), nid
    # anna's AI reply on a side branch of the shared path, and ben's.
    r = _client(app, "anna").post(f"/api/nodes/{turn}/llm", json={})
    assert r.status_code == 202, r.get_json()
    her_side = r.get_json()["node_id"]
    ben = _client(app, "ben")
    mine = _reply(app, "ben", entry, "BENS REPLY").get_json()["id"]
    key = ben.get(f"/api/nodes/{mine}").get_json()["audience"]["ai_reads_key"]
    r = ben.post(f"/api/nodes/{mine}/llm", json={"ai_reads_ack": key})
    assert r.status_code == 202, r.get_json()
    his = r.get_json()["node_id"]
    assert _node(her_side).audience_root_id == entry

    # The entry is deleted: the path, and her AI reply beside it, are
    # hers alone again.
    assert _client(app, "anna").delete(f"/api/nodes/{entry}").status_code == 200
    anna = _user("anna")
    for nid in path + (her_side,):
        node = _node(nid)
        assert node.privacy_level == "private", nid
        assert node.audience_root_id is None, nid
        assert can_user_access_node(node, anna.id), nid
        assert not _can_read(app, "ben", nid), nid
    assert not _can_read(app, "ben", his)

    # Past the grace period the cleanup purges the entry's text and ben's
    # AI reply (his after the entry's content is gone); ben's typed reply,
    # under which he asked, stays his (so the entry's row may stay as a
    # tombstone above it); everything of anna's above the entry stays,
    # open to her.
    later = time.time() + 31 * 86400
    for _ in range(3):
        finalize_deleted_nodes(now=later)
    _db.session.expire_all()
    gone = _db.session.get(Node, his)
    assert gone is None or gone.content is None
    gone_entry = _db.session.get(Node, entry)
    assert gone_entry is None or gone_entry.content is None
    assert _node(mine).get_content() == "BENS REPLY"
    for nid in path + (her_side,):
        node = _node(nid)
        assert node is not None and node.deleted_at is None, nid
        assert node.get_content(), nid
        assert can_user_access_node(node, anna.id), nid
        assert _can_read(app, "anna", nid), nid


# ── 7. A prompt is shared only with the discussion leading up to an entry

def test_a_prompt_under_a_shared_entry_cannot_be_shared_on_its_own(app):  # noqa: F811
    entry = _share(app, names=["ben"])
    prompt = _add(entry, "A PROMPT {user_profile}", prompt_key="textmode")
    r = _client(app, "anna").put(f"/api/nodes/{prompt}", json={
        "content": "A PROMPT {user_profile}", "privacy_level": "circles"})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["code"] == "circle_prompt_node"
    node = _node(prompt)
    assert node.privacy_level == "private" and node.audience_root_id is None
    assert not _can_read(app, "ben", prompt)
    # ben's own Text-mode prompt under the entry: not into the thread
    # either.
    his = _add(entry, "HIS PROMPT", owner="ben", prompt_key="textmode")
    r = _client(app, "ben").put(f"/api/nodes/{his}", json={
        "content": "HIS PROMPT", "privacy_level": "circles"})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["code"] == "circle_prompt_node"
    assert not _can_read(app, "anna", his)
    # A plain reply still joins the thread.
    reply = _add(entry, "ANNAS REPLY")
    r = _client(app, "anna").put(f"/api/nodes/{reply}", json={
        "content": "ANNAS REPLY", "privacy_level": "circles"})
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "ben", reply)


# ── 11. A reader's own recent writing on a shared prompt ────────────────

def test_a_reader_sees_his_own_recent_writing_on_a_shared_prompt(app):  # noqa: F811
    from backend.models import UserProfile, UserRecentContext
    ben = _user("ben")
    profile = UserProfile(user_id=ben.id, generated_by="test",
                          ai_usage="chat")
    profile.set_content("BENS-PROFILE")
    _db.session.add(profile)
    _db.session.flush()
    rc = UserRecentContext(user_id=ben.id, profile_id=profile.id,
                           generated_by="test", ai_usage="chat")
    rc.set_content("BENS-RECENT-SUMMARY")
    _db.session.add(rc)
    _db.session.commit()
    _reply(app, "ben", None, content="ben writes something of his own",
           privacy="private")
    prompt = _add(None, "THE-PROMPT {user_recent} {user_recent_raw}",
                  prompt_key="textmode")
    entry = _add(prompt, SECRET)
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    data = _client(app, "ben").get(f"/api/nodes/{prompt}").get_json()
    artifacts = data["context_artifacts"] or {}
    assert artifacts.get("recent", {}).get("content") == "BENS-RECENT-SUMMARY"
    assert artifacts.get("recent_raw", {}).get("covers_start")


# ── 11. The reply form under a member's own private AI reply ────────────

def test_a_reply_under_a_members_own_private_ai_reply_is_only_me(app):  # noqa: F811
    entry = _share(app, names=["ben"])
    ben = _client(app, "ben")
    mine = _reply(app, "ben", entry, "BENS REPLY").get_json()["id"]
    # Every AI reply is created private (Peter, 2026-11-12).
    r = ben.post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 202, r.get_json()
    private_ai = r.get_json()["node_id"]
    r = ben.post(f"/api/nodes/{mine}/llm", json={})
    shared_ai = r.get_json()["node_id"]
    node = _node(shared_ai)
    node.llm_task_status = "completed"
    _db.session.commit()
    r = ben.put(f"/api/nodes/{shared_ai}", json={
        "content": node.get_content(), "privacy_level": "circles"})
    assert r.status_code == 200, r.get_json()
    assert ben.get(f"/api/nodes/{private_ai}").get_json().get(
        "reply_only_private") is True
    assert "reply_only_private" not in ben.get(
        f"/api/nodes/{shared_ai}").get_json()
    # What the field says is what the server accepts.
    assert _reply(app, "ben", private_ai, privacy="public").status_code == 403
    assert _reply(app, "ben", private_ai, privacy="private").status_code == 201
    # anna's own private AI reply in her own thread: hers to share.
    r = _client(app, "anna").post(f"/api/nodes/{entry}/llm", json={})
    hers = r.get_json()["node_id"]
    assert "reply_only_private" not in _client(app, "anna").get(
        f"/api/nodes/{hers}").get_json()


# ── Second review ───────────────────────────────────────────────────────

def _agentic_shared(app):  # noqa: F811
    """anna's Text-mode thread shared with ben with the discussion
    leading up to her entry: its prompt (edited: not one of Loore's
    defaults) asks for the member's data. ben's own reply in the thread,
    where he asks for AI replies (the public rule), and the key."""
    prompt = _add(None, "THE-PROMPT {user_profile}", prompt_key="textmode")
    entry = _add(prompt, SECRET)
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    mine = _reply(app, "ben", entry, "BENS REPLY").get_json()["id"]
    key = _client(app, "ben").get(
        f"/api/nodes/{mine}").get_json()["audience"]["ai_reads_key"]
    assert key
    return entry, mine, key


def test_a_members_only_me_ai_reply_tells_the_client_what_to_agree_to(app):  # noqa: F811,E501
    """2. Under his own Only-me AI reply ben's next AI reply needs the
    agreement too: the node says what it reads and the key, so the
    client can ask (in the private wording) instead of being refused."""
    entry, reply, key = _agentic_shared(app)
    ben = _client(app, "ben")
    r = ben.post(f"/api/nodes/{reply}/llm", json={
        "privacy_level": "private", "ai_reads_ack": f"private:{key}"})
    assert r.status_code == 202, r.get_json()
    mine = r.get_json()["node_id"]
    data = ben.get(f"/api/nodes/{mine}").get_json()
    assert "audience" not in data
    from backend.utils import circles as cz
    assert data["thread_ai_reads"] == {
        "entry_id": entry, "ai_reads": ["profile", cz.TOOLS_NAME],
        "ai_reads_key": key}
    assert ben.post(f"/api/nodes/{mine}/llm", json={}).status_code == 409
    r = ben.post(f"/api/nodes/{mine}/llm",
                 json={"ai_reads_ack": f"private:{key}"})
    assert r.status_code == 202, r.get_json()
    again = _node(r.get_json()["node_id"])
    assert again.privacy_level == "private"
    assert again.audience_root_id == entry
    # No other node carries it: the author's shared entry, a private note.
    assert "thread_ai_reads" not in _client(app, "anna").get(
        f"/api/nodes/{entry}").get_json()
    alone = _add(None, "A PRIVATE NOTE")
    assert "thread_ai_reads" not in _client(app, "anna").get(
        f"/api/nodes/{alone}").get_json()


def test_a_members_only_me_branch_works_like_his_private_thread(app):  # noqa: F811,E501
    """3. ben replies under his own Only-me AI reply and asks for an AI
    reply there: it is made (Only me, in the thread), never joins the
    audience, and closes and goes with the entry like his other AI
    replies there. His typed replies stay his."""
    from backend.tasks.node_cleanup import finalize_deleted_nodes
    entry, reply, key = _agentic_shared(app)
    ben = _client(app, "ben")
    ack = {"ai_reads_ack": f"private:{key}"}
    r = ben.post(f"/api/nodes/{reply}/llm",
                 json={"privacy_level": "private", **ack})
    mine = r.get_json()["node_id"]
    r = _reply(app, "ben", mine, content="BENS PRIVATE NOTE",
               privacy="private")
    assert r.status_code == 201, r.get_json()
    note = r.get_json()["id"]
    r = ben.post(f"/api/nodes/{note}/llm", json=ack)
    assert r.status_code == 202, r.get_json()
    deeper = r.get_json()["node_id"]
    data = ben.get(f"/api/nodes/{note}").get_json()
    assert data["reply_only_private"] is True
    assert data["thread_ai_reads"]["ai_reads_key"] == key
    node = _node(deeper)
    assert node.privacy_level == "private" and node.audience_root_id == entry
    assert not _can_read(app, "anna", deeper)
    assert _can_read(app, "ben", deeper)
    # Never into the audience: not the note, not a reply under it.
    r = ben.put(f"/api/nodes/{note}", json={
        "content": "BENS PRIVATE NOTE", "privacy_level": "circles"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_only_me_branch"
    r = _reply(app, "ben", note, content="to the thread?", privacy="circles")
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_only_me_branch"
    assert not _can_read(app, "anna", note)
    # It closes with ben's access...
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["chidi"]})
    assert r.status_code == 200, r.get_json()
    assert not _can_read(app, "ben", deeper)
    assert _can_read(app, "ben", note)              # his own writing
    # ... and goes with the entry.
    assert _client(app, "anna").delete(f"/api/nodes/{entry}").status_code == 200
    later = time.time() + 31 * 86400
    for _ in range(3):
        finalize_deleted_nodes(now=later)
    _db.session.expire_all()
    gone = _db.session.get(Node, deeper)
    assert gone is None or gone.content is None
    assert _db.session.get(Node, note).deleted_at is None


def test_the_readers_sentence_names_only_who_can_read_the_entry(app):  # noqa: F811,E501
    """5. A person added to a circle with "Only new entries" after the
    entry was shared with it does not read the entry: the sentence of
    the edit form does not name them, and an audience only such people
    are in is refused."""
    from backend.tests.test_circles import _circle
    close = _circle(app, "anna", "Close", ["ben"])
    entry = _share(app, circle_ids=[close])
    anna = _client(app, "anna")
    r = anna.post(f"/api/circles/{close}/members",
                  json={"username": "dana", "include_existing": False})
    assert r.status_code == 201, r.get_json()
    assert not _can_read(app, "dana", entry)

    def preview(**extra):
        return anna.post("/api/circles/audience-preview", json={
            "circle_ids": [close], "usernames": [], **extra}).get_json()
    assert preview(node_id=entry)["readers"] == ["ben"]
    assert preview()["readers"] == ["ben", "dana"]     # a new entry
    readers = _client(app, "ben").get(
        f"/api/nodes/{entry}").get_json()["audience"]["readers"]
    assert readers == ["ben"]
    # ben leaves the circle: nobody who can read the entry is left in it.
    r = anna.delete(f"/api/circles/{close}/members/ben")
    assert r.status_code == 200, r.get_json()
    assert preview(node_id=entry)["readers"] == []
    r = anna.put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_ids": [close], "circle_usernames": []})
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_no_readers"
