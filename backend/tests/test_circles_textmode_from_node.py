"""Circles: typed replies under a Glean follow the Circles reply rules.

POST /api/textmode/from-node/<id> is what the reply box under a Glean
sends. Its message follows the rules of a typed reply (POST /api/nodes/)
and a recorded one (save-as-node): under a shared node it is "circles"
and joins the thread, where the thread's people read it; in someone
else's shared thread it is never Everyone; where those routes refuse, it
is refused too, before anything is written. With Circles off the route
is as it was before Circles. A reply recorded in the same box
(save-as-node with ``agentic``) is placed the same way.

The route stores the message at one level (``message_level``, the
node's). Tests that send a level in the body hold whatever the route
does with it: refused with the code the other routes give, or stored at
a level the rule allows. The spy on circles.new_reply_refusal checks
that the rule is asked about the level that is stored.

anna shares with "Close" (ben, chidi); stan has Circles and no tie. No
model is called (the task module is a MagicMock)."""
import uuid

import pytest

from backend.tests.test_circles_review import (  # noqa: F401 (fixture)
    app, _user, _client, _circle, _share, _reply, _db, Node, User, Draft)


def _seen(app, who, nid):  # noqa: F811
    return _client(app, who).get(f"/api/nodes/{nid}").status_code


def _count():
    _db.session.expire_all()
    return Node.query.count()


def _from_node(app, who, nid, **body):  # noqa: F811
    body.setdefault("content", "what I make of these picks")
    body.setdefault("ai_usage", "chat")
    body.setdefault("auto_generate", False)
    return _client(app, who).post(f"/api/textmode/from-node/{nid}",
                                  json=body)


def _private(app, who, content):  # noqa: F811
    r = _client(app, who).post("/api/nodes/", json={
        "content": content, "privacy_level": "private", "ai_usage": "chat"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


@pytest.fixture
def asked(monkeypatch):
    """(node id, level) for each call of circles.new_reply_refusal."""
    from backend.utils import circles as cz
    real = cz.new_reply_refusal
    calls = []

    def spy(parent, level, user):
        calls.append((parent.id, level))
        return real(parent, level, user)
    monkeypatch.setattr(cz, "new_reply_refusal", spy)
    return calls


@pytest.fixture
def glean(app, monkeypatch):  # noqa: F811
    """A function: *who*'s finished Glean under their own node *under*
    (the read prompt, private, and the gleaning below it), its id."""
    app.config["GLEAN_FOR_ALL"] = True
    # A model a glean may run on (the "read" flag), chosen by the user.
    models = dict(app.config["SUPPORTED_MODELS"])
    models["claude-x"] = dict(models["claude-x"], read=True)
    app.config["SUPPORTED_MODELS"] = models
    import backend.routes.read as rd
    monkeypatch.setattr(rd, "_resolve_model", lambda node: ("claude-x", None))
    if User.query.filter_by(username="claude-x").first() is None:
        _db.session.add(User(username="claude-x", twitter_id="llm-claude-x"))
        _db.session.commit()

    def make(who, under):
        _user(who).glean_enabled = True
        _db.session.commit()
        r = _client(app, who).post(f"/api/read/from-node/{under}", json={})
        assert r.status_code == 202, r.get_json()
        node = _db.session.get(Node, r.get_json()["llm_node_id"])
        node.set_content("THE PICKS")
        node.llm_task_status = "completed"
        _db.session.commit()
        return node.id
    return make


def _bens_glean_in_annas_thread(app, glean):  # noqa: F811
    """(anna's shared entry, ben's reply to it, ben's Glean under his
    reply): a Glean is private, so his Glean is his own Only-me node in
    her thread."""
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    gid = glean("ben", reply)
    g = _db.session.get(Node, gid)
    assert g.privacy_level == "private" and g.audience_root_id == entry
    return close, entry, reply, gid


# ── Never Everyone in someone else's thread ─────────────────────────────

@pytest.mark.parametrize("sent", [None, "public"])
def test_a_members_reply_under_his_only_me_glean_is_never_everyone(
        app, glean, asked, sent):  # noqa: F811
    _, entry, _, gid = _bens_glean_in_annas_thread(app, glean)
    asked.clear()
    before = _count()
    body = {} if sent is None else {"privacy_level": sent}
    r = _from_node(app, "ben", gid, **body)
    if sent == "public" and r.status_code == 403:
        # A route that takes the level it is sent refuses Everyone here.
        assert r.get_json()["code"] == "circle_thread_public"
        assert asked == [(gid, "public")]
        assert _count() == before
        return
    assert r.status_code == 202, r.get_json()
    msg = _db.session.get(Node, r.get_json()["user_node_id"])
    # The rule was asked about the level the message is stored with.
    assert asked == [(gid, msg.privacy_level)]
    # Only me, under his own Text-mode prompt, read by him alone.
    assert msg.privacy_level == "private"
    assert msg.audience_root_id is None
    prompt = _db.session.get(Node, r.get_json()["prompt_node_id"])
    assert (prompt.parent_id, prompt.privacy_level,
            prompt.audience_root_id) == (gid, "private", None)
    assert msg.parent_id == prompt.id
    assert Node.query.filter_by(privacy_level="public").count() == 0
    for other in ("anna", "chidi"):
        assert _seen(app, other, msg.id) == 404
        assert _seen(app, other, prompt.id) == 404
    assert _seen(app, "ben", msg.id) == 200


def test_everyone_in_someone_elses_thread_is_refused_before_anything_is_written(
        app, glean, asked):  # noqa: F811
    """No route makes ben's node in anna's thread Everyone. One that is
    Everyone in the database still gets no Everyone message here: the
    rule is asked about the level the message would be stored with."""
    _, _, _, gid = _bens_glean_in_annas_thread(app, glean)
    g = _db.session.get(Node, gid)
    g.privacy_level = "public"
    _db.session.commit()
    asked.clear()
    before = _count()
    r = _from_node(app, "ben", gid)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["code"] == "circle_thread_public"
    assert asked == [(gid, "public")]
    assert _count() == before


# ── Only me under a shared node ─────────────────────────────────────────

@pytest.mark.parametrize("who", ["anna", "ben"])
@pytest.mark.parametrize("sent", [None, "private"])
def test_only_me_under_a_shared_node_is_refused_while_circles_is_on(
        app, asked, who, sent):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    # The node replied to: anna's shared entry, or ben's reply to it.
    nid = entry if who == "anna" else _reply(
        app, "ben", entry, "ben replies").get_json()["id"]
    # The rule the route asks refuses Only me under it.
    from backend.utils import circles as cz
    resp, status = cz.new_reply_refusal(
        _db.session.get(Node, nid), "private", _user(who))
    assert (status, resp.get_json()["code"]) == (400, "circle_reply_required")
    asked.clear()

    before = _count()
    body = {} if sent is None else {"privacy_level": sent}
    r = _from_node(app, who, nid, **body)
    if sent == "private" and r.status_code == 400:
        # A route that takes the level it is sent refuses it here.
        assert r.get_json()["code"] == "circle_reply_required"
        assert asked == [(nid, "private")]
        assert _count() == before
        return
    assert r.status_code == 202, r.get_json()
    msg = _db.session.get(Node, r.get_json()["user_node_id"])
    assert asked == [(nid, msg.privacy_level)]
    assert msg.privacy_level == "circles"
    assert msg.audience_root_id == entry


# ── A Circles message joins the thread ──────────────────────────────────

def test_a_circles_message_under_a_shared_glean_joins_and_its_readers_read_it(
        app, glean, asked):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    gid = glean("anna", _private(app, "anna", "anna's note"))
    r = _client(app, "anna").put(f"/api/nodes/{gid}/audience",
                                 json={"circle_ids": [close]})
    assert r.status_code == 200, r.get_json()
    asked.clear()
    r = _from_node(app, "anna", gid, content="ANNA ON HER PICKS")
    assert r.status_code == 202, r.get_json()
    out = r.get_json()
    # Directly under the Glean: no Text-mode prompt of hers in between,
    # which the circle would not read.
    assert out["prompt_node_id"] is None
    msg = _db.session.get(Node, out["user_node_id"])
    assert (msg.parent_id, msg.privacy_level, msg.audience_root_id) == (
        gid, "circles", gid)
    assert asked == [(gid, "circles")]
    for reader in ("ben", "chidi"):
        got = _client(app, reader).get(f"/api/nodes/{msg.id}")
        assert got.status_code == 200
        assert got.get_json()["content"] == "ANNA ON HER PICKS"
        # ... and in the thread as they open it.
        tree = _client(app, reader).get(f"/api/nodes/{gid}").get_json()
        assert msg.id in [c["id"] for c in tree["children"]]
    assert _seen(app, "stan", msg.id) == 404


def test_a_circles_message_gets_no_automatic_reply(app, glean):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben"])
    gid = glean("anna", _private(app, "anna", "anna's note"))
    assert _client(app, "anna").put(f"/api/nodes/{gid}/audience", json={
        "circle_ids": [close]}).status_code == 200
    before = _count()
    r = _from_node(app, "anna", gid, auto_generate=True)
    assert r.status_code == 202, r.get_json()
    out = r.get_json()
    assert out["llm_error_code"] == "auto_generate_off"
    assert "llm_node_id" not in out
    assert _count() == before + 1      # the message only
    assert _db.session.get(Node, out["user_node_id"]).audience_root_id == gid


def test_a_members_typed_message_under_his_reply_joins_the_thread(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    r = _from_node(app, "ben", reply, content="BEN GOES ON")
    assert r.status_code == 202, r.get_json()
    msg = _db.session.get(Node, r.get_json()["user_node_id"])
    assert (msg.parent_id, msg.privacy_level, msg.audience_root_id) == (
        reply, "circles", entry)
    for reader in ("anna", "chidi"):
        assert _seen(app, reader, msg.id) == 200
    assert _seen(app, "stan", msg.id) == 404


# ── Refused where the other reply routes refuse ─────────────────────────

def _typed(app, who, parent, content):  # noqa: F811
    return _client(app, who).post("/api/nodes/", json={
        "content": content, "privacy_level": "circles", "ai_usage": "chat",
        "parent_id": parent})


def test_a_member_removed_from_the_circle_is_refused_as_on_a_typed_reply(
        app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    assert _client(app, "anna").delete(
        f"/api/circles/{close}/members",
        json={"username": "ben"}).status_code == 200
    before = _count()
    typed = _typed(app, "ben", reply, "ben again")
    r = _from_node(app, "ben", reply, content="ben again")
    assert typed.status_code == 403
    assert (r.status_code, r.get_json()["code"]) == (
        typed.status_code, typed.get_json()["code"]) == (
        403, "circle_thread_closed")
    assert _count() == before


@pytest.mark.parametrize("auto", [False, True])
def test_a_removed_member_cannot_write_under_his_glean_closed_to_him(
        app, glean, auto):  # noqa: F811
    """His Glean in her thread is closed to him once he is removed: the
    same 404 as a typed or a recorded reply under it, nothing written."""
    close, _, _, gid = _bens_glean_in_annas_thread(app, glean)
    assert _client(app, "anna").delete(
        f"/api/circles/{close}/members",
        json={"username": "ben"}).status_code == 200
    assert _seen(app, "ben", gid) == 404
    before = _count()
    typed = _client(app, "ben").post("/api/nodes/", json={
        "content": "words", "privacy_level": "private", "ai_usage": "chat",
        "parent_id": gid})
    recorded = _save_agentic(app, "ben", _recorded("ben", gid, "private"),
                             "private", auto=auto)
    r = _from_node(app, "ben", gid, auto_generate=auto)
    for answer in (typed, recorded, r):
        assert (answer.status_code, answer.get_json()["error"]) == (
            404, "Parent node not found")
    assert _count() == before


def test_a_message_quoting_what_the_circle_cannot_read_is_refused(
        app, glean):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    secret = _private(app, "anna", "ANNA'S OWN NOTE")
    gid = glean("anna", _private(app, "anna", "anna's note"))
    assert _client(app, "anna").put(f"/api/nodes/{gid}/audience", json={
        "circle_ids": [close]}).status_code == 200
    before = _count()
    text = f"as I wrote {{quote:{secret}}}"
    typed = _typed(app, "anna", gid, text)
    r = _from_node(app, "anna", gid, content=text)
    assert typed.status_code == 409, typed.get_json()
    assert (r.status_code, r.get_json()["code"]) == (
        409, typed.get_json()["code"])
    assert [q["id"] for q in r.get_json()["quoted"]] == [secret]
    assert _count() == before
    # The thread's own Glean is read by everyone in it.
    r = _from_node(app, "anna", gid, content=f"you picked {{quote:{gid}}}")
    assert r.status_code == 202, r.get_json()


# ── With Circles off, as before Circles ─────────────────────────────────

def test_with_circles_off_the_route_is_as_before(app, asked, monkeypatch):  # noqa: F811
    """CIRCLES_V1 off (and nobody on the list): the nodes and the answer
    are those of the route without Circles. The Text-mode prompt goes
    under the node at the level prompt_entry_privacy gives it (Public
    only for a Public message of an account whose default is Public,
    2026-11-24) and the message under it; inside an agentic thread the
    message goes directly under the node; a public thread gets no
    automatic reply; nothing carries a thread pointer."""
    from backend.utils import privacy
    monkeypatch.setitem(privacy._pointers_seen, "at", 0.0)
    monkeypatch.setitem(privacy._pointers_seen, "any", False)
    app.config["CIRCLES_V1"] = False
    app.config["CIRCLES_USER_IDS"] = set()
    anna = _client(app, "anna")

    def note(level):
        r = anna.post("/api/nodes/", json={
            "content": f"a {level} note", "privacy_level": level,
            "ai_usage": "chat"})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]

    for level in ("private", "public"):
        nid = note(level)
        r = _from_node(app, "anna", nid)
        assert r.status_code == 202, r.get_json()
        out = r.get_json()
        assert set(out) == {"prompt_node_id", "user_node_id"}
        prompt = _db.session.get(Node, out["prompt_node_id"])
        msg = _db.session.get(Node, out["user_node_id"])
        # anna's default level is not Public: her prompt entry is Private
        # under either message (session_helpers.prompt_entry_privacy).
        assert _user("anna").default_privacy_level != "public"
        assert (prompt.parent_id, prompt.privacy_level,
                prompt.get_prompt_key()) == (nid, "private", "textmode")
        assert (msg.parent_id, msg.privacy_level) == (prompt.id, level)

    # The automatic reply: made in a private thread, not in a public one.
    r = _from_node(app, "anna", note("private"), auto_generate=True)
    assert r.status_code == 202, r.get_json()
    assert {"llm_node_id", "task_id"} <= set(r.get_json())
    r = _from_node(app, "anna", note("public"), auto_generate=True)
    assert r.status_code == 202, r.get_json()
    out = r.get_json()
    assert out["llm_error_code"] == "auto_generate_off"
    assert "llm_node_id" not in out

    # Inside an agentic thread: no new prompt, the message right under.
    r = anna.post("/api/textmode/start", json={
        "content": "a Text-mode entry", "ai_usage": "chat",
        "privacy_level": "private", "auto_generate": False})
    assert r.status_code == 202, r.get_json()
    first = r.get_json()["user_node_id"]
    r = _from_node(app, "anna", first)
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["prompt_node_id"] is None
    msg = _db.session.get(Node, r.get_json()["user_node_id"])
    assert (msg.parent_id, msg.privacy_level) == (first, "private")

    # The rule was asked on each of the five messages, at the level each
    # was stored with, and refused nothing; nothing carries a pointer.
    assert [level for _, level in asked] == [
        "private", "public", "private", "public", "private"]
    _db.session.expire_all()
    assert Node.query.filter(Node.audience_root_id.isnot(None)).count() == 0


# ── A reply recorded in the same box is placed the same way ─────────────

def _recorded(who, parent_id, stored="circles"):
    """A finished recording of *who*'s under *parent_id*; the URL that
    saves it as an entry."""
    d = Draft(user_id=_user(who).id, node_id=None, parent_id=parent_id,
              session_id=str(uuid.uuid4()), streaming_status="completed",
              streaming_total_chunks=1, streaming_completed_chunks=1,
              privacy_level=stored, ai_usage="chat")
    d.set_content("recorded words")
    _db.session.add(d)
    _db.session.commit()
    return f"/api/drafts/streaming/{d.session_id}/save-as-node"


def _save_agentic(app, who, url, level, auto=True):  # noqa: F811
    """What the box under a Glean sends for a recording (NodeDetail
    submitReadReplyMessage, ThreadModel.submitReadReply)."""
    return _client(app, who).post(url, json={
        "content": "RECORDED ON THE PICKS", "privacy_level": level,
        "ai_usage": "chat", "agentic": True, "auto_generate": auto,
        "model": "gpt-5"})


def test_a_recorded_circles_reply_under_a_shared_glean_joins_like_a_typed_one(
        app, glean):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    gid = glean("anna", _private(app, "anna", "anna's note"))
    assert _client(app, "anna").put(f"/api/nodes/{gid}/audience", json={
        "circle_ids": [close]}).status_code == 200
    before = _count()
    r = _save_agentic(app, "anna", _recorded("anna", gid), "circles")
    assert r.status_code == 201, r.get_json()
    out = r.get_json()
    # Directly under the Glean, no Text-mode prompt in between, and no
    # automatic reply in a shared thread.
    assert "conversation_id" not in out
    assert out["llm_error_code"] == "auto_generate_off"
    assert "llm_node_id" not in out
    assert _count() == before + 1
    node = _db.session.get(Node, out["id"])
    assert (node.parent_id, node.privacy_level, node.audience_root_id) == (
        gid, "circles", gid)
    # Placed as the typed one is.
    typed = _from_node(app, "anna", gid).get_json()
    t = _db.session.get(Node, typed["user_node_id"])
    assert (t.parent_id, t.audience_root_id) == (node.parent_id,
                                                 node.audience_root_id)
    for reader in ("ben", "chidi"):
        assert _seen(app, reader, node.id) == 200
        tree = _client(app, reader).get(f"/api/nodes/{gid}").get_json()
        assert node.id in [c["id"] for c in tree["children"]]
    assert _seen(app, "stan", node.id) == 404


def test_a_members_recorded_reply_under_his_reply_joins_the_thread(app):  # noqa: F811
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    entry = _share(app, circle_ids=[close])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    r = _save_agentic(app, "ben", _recorded("ben", reply), "circles")
    assert r.status_code == 201, r.get_json()
    node = _db.session.get(Node, r.get_json()["id"])
    assert (node.parent_id, node.privacy_level, node.audience_root_id) == (
        reply, "circles", entry)
    for reader in ("anna", "chidi"):
        assert _seen(app, reader, node.id) == 200


def test_a_recorded_reply_under_his_only_me_glean_keeps_its_prompt(
        app, glean):  # noqa: F811
    """Not a reply that joins: the Text-mode prompt is attached under the
    Glean as before, and the entry is his alone."""
    _, entry, _, gid = _bens_glean_in_annas_thread(app, glean)
    r = _save_agentic(app, "ben", _recorded("ben", gid, stored="private"),
                      "private", auto=False)
    assert r.status_code == 201, r.get_json()
    out = r.get_json()
    prompt = _db.session.get(Node, out["conversation_id"])
    node = _db.session.get(Node, out["id"])
    assert (prompt.parent_id, prompt.privacy_level,
            prompt.get_prompt_key()) == (gid, "private", "textmode")
    assert (node.parent_id, node.privacy_level, node.audience_root_id) == (
        prompt.id, "private", None)
    for other in ("anna", "chidi"):
        assert _seen(app, other, node.id) == 404


@pytest.mark.parametrize("level", ["circles", "public"])
def test_a_recorded_reply_refused_in_someone_elses_thread_writes_nothing(
        app, level):  # noqa: F811
    """Refused before anything is placed, as a typed reply is: Circles
    where the thread is closed to him, Everyone under a shared node. The
    session stays."""
    close = _circle(app, "anna", "Close", ["ben"])
    entry = _share(app, circle_ids=[close])
    reply = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    assert _client(app, "anna").delete(
        f"/api/circles/{close}/members",
        json={"username": "ben"}).status_code == 200
    url = _recorded("ben", reply)
    before = _count()
    typed = _client(app, "ben").post("/api/nodes/", json={
        "content": "words", "privacy_level": level, "ai_usage": "chat",
        "parent_id": reply})
    r = _save_agentic(app, "ben", url, level)
    expected = ((403, "circle_thread_closed") if level == "circles"
                else (400, "circle_reply_required"))
    assert (r.status_code, r.get_json()["code"]) == expected
    assert (typed.status_code, typed.get_json()["code"]) == expected
    assert _count() == before
    assert Draft.query.count() == 1


def test_with_circles_off_a_recorded_reply_keeps_its_prompt(
        app, monkeypatch):  # noqa: F811
    from backend.utils import privacy
    monkeypatch.setitem(privacy._pointers_seen, "at", 0.0)
    monkeypatch.setitem(privacy._pointers_seen, "any", False)
    app.config["CIRCLES_V1"] = False
    app.config["CIRCLES_USER_IDS"] = set()
    for level in ("private", "public"):
        r = _client(app, "anna").post("/api/nodes/", json={
            "content": f"a {level} note", "privacy_level": level,
            "ai_usage": "chat"})
        assert r.status_code == 201, r.get_json()
        nid = r.get_json()["id"]
        r = _client(app, "anna").post(
            _recorded("anna", nid, stored=level),
            json={"content": "words", "privacy_level": level,
                  "ai_usage": "chat", "agentic": True})
        assert r.status_code == 201, r.get_json()
        out = r.get_json()
        prompt = _db.session.get(Node, out["conversation_id"])
        node = _db.session.get(Node, out["id"])
        # anna's default level is not Public: her prompt entry is Private
        # under either message (session_helpers.prompt_entry_privacy).
        assert (prompt.parent_id, prompt.privacy_level) == (nid, "private")
        assert (node.parent_id, node.privacy_level,
                node.audience_root_id) == (prompt.id, level, None)
