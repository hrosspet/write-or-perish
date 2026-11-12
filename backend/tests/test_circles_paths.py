"""Circles: what is created under a shared entry and is not a reply into
the thread stays its writer's. The review's second round of probes of
#631, kept as tests, plus one walk over every node-creating path."""
import pytest

from backend.tests.test_circles_review import (  # noqa: F401 (fixture)
    app, _user, _client, _circle, _share, _reply, _answer, _db, Node, User,
    SECRET, PEOPLE)


def _seen(app, who, nid):
    r = _client(app, who).get(f"/api/nodes/{nid}")
    return r.status_code


def test_authors_voice_session_from_her_shared_entry(app):
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    anna = _client(app, "anna")
    r = anna.post(f"/api/voice/from-node/{e}", json={})
    print("voice from-node:", r.status_code, r.get_json())
    under = Node.query.filter(Node.parent_id == e).all()
    rows = [(n.id, n.privacy_level, n.audience_root_id, n.get_prompt_key(),
             n.node_type, _seen(app, "ben", n.id)) for n in under]
    print("nodes under E (id, level, pointer, prompt_key, type, ben GET):",
          rows)
    sysnode = next((n for n in under if n.get_prompt_key() == "voice"), None)
    if sysnode is not None:
        # anna's spoken turn in that voice session
        r = anna.post("/api/voice/", json={
            "content": "ANNA SPOKEN TURN", "parent_id": sysnode.id})
        print("voice turn:", r.status_code)
        turn = Node.query.filter(Node.parent_id == sysnode.id,
                                 Node.node_type == "user").first()
        if turn is not None:
            print("anna's spoken turn:", turn.privacy_level,
                  turn.audience_root_id, "ben GET:", _seen(app, "ben", turn.id))
    for n in Node.query.filter(Node.node_type == "llm").all():
        print("voice llm node:", n.id, n.parent_id, n.privacy_level,
              n.audience_root_id, "ben GET:", _seen(app, "ben", n.id))
    leaked = [x for x in rows if x[5] == 200]
    assert not leaked, leaked


def test_authors_glean_from_her_shared_entry(app, monkeypatch):
    app.config["GLEAN_FOR_ALL"] = True
    import backend.routes.read as rd
    monkeypatch.setattr(rd, "_resolve_model", lambda node: ("claude-x", None))
    if User.query.filter_by(username="claude-x").first() is None:
        _db.session.add(User(username="claude-x", twitter_id="llm-claude-x"))
        _db.session.commit()
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    anna_u = _user("anna")
    anna_u.glean_enabled = True
    _db.session.commit()
    r = _client(app, "anna").post(f"/api/read/from-node/{e}", json={})
    print("glean from-node:", r.status_code, r.get_json())
    under = Node.query.filter(Node.parent_id == e).all()
    rows = []
    for n in under:
        rows.append((n.id, n.privacy_level, n.audience_root_id,
                     n.get_prompt_key(), _seen(app, "ben", n.id)))
        for c in Node.query.filter(Node.parent_id == n.id):
            rows.append((c.id, c.privacy_level, c.audience_root_id,
                         c.node_type, _seen(app, "ben", c.id)))
    print("glean nodes (id, level, pointer, key/type, ben GET):", rows)
    assert not [x for x in rows if x[4] == 200]


@pytest.mark.parametrize("ben_pref,author_model,expect", [
    (None, "claude-x", "gpt-5"),      # no model set: server default
    ("claude-x", "gpt-5", "claude-x"),
])
def test_ask_model_variants(app, ben_pref, author_model, expect):
    close = _circle(app, "anna", "Close", ["ben"])
    anna = _user("anna")
    for name in ("claude-x", "gpt-5"):
        if User.query.filter_by(username=name).first() is None:
            _db.session.add(User(username=name, twitter_id=f"llm-{name}"))
    _db.session.flush()
    llm_user = User.query.filter_by(username=author_model).first()
    e = _share(app, circle_ids=[close])
    # The author's AI reply under her entry, the thread's last AI node
    # (shared with the audience by the cascade's rule: circles).
    her_ai = Node(user_id=llm_user.id, human_owner_id=anna.id, parent_id=e,
                  node_type="llm", llm_model=author_model,
                  privacy_level="circles", ai_usage="chat",
                  audience_root_id=e)   # she shared it with the thread
    her_ai.set_content("her AI reply")
    _db.session.add(her_ai)
    ben = _user("ben")
    ben.preferred_model = ben_pref
    _db.session.commit()
    # ben asks under his own reply to it (the public rule). He names no
    # model: his own, never the model of anna's reply above.
    mine = _reply(app, "ben", her_ai.id, "BEN R").get_json()["id"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    a = _db.session.get(Node, r.get_json()["node_id"])
    assert a.llm_model == expect
    assert a.human_owner_id == ben.id and a.audience_root_id == e
    # A model ben names himself is his choice.
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm",
                                 json={"model": author_model})
    assert _db.session.get(
        Node, r.get_json()["node_id"]).llm_model == author_model


def test_take_back_semantics(app):
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    e = _share(app, circle_ids=[close])
    rb = _reply(app, "ben", e, "BEN R").get_json()["id"]
    rc = _reply(app, "chidi", rb, "CHIDI R").get_json()["id"]
    aid = _answer(app, "ben", rc)
    anna = _client(app, "anna")
    assert anna.put(f"/api/nodes/{e}", json={
        "privacy_level": "private", "content": SECRET}).status_code == 200
    out = {}
    for who in ("anna", "ben", "chidi", "dana", None):
        out[who] = {k: (_client(app, who).get(f"/api/nodes/{nid}").status_code)
                    for k, nid in (("E", e), ("benR", rb), ("chidiR", rc),
                                   ("answer", aid))}
    for k, v in out.items():
        print("TAKEBACK", k, v)
    # anna made it public afterwards
    assert anna.put(f"/api/nodes/{e}", json={
        "privacy_level": "public", "content": SECRET}).status_code == 200
    out2 = {who: {k: _client(app, who).get(f"/api/nodes/{nid}").status_code
                  for k, nid in (("benR", rb), ("chidiR", rc), ("answer", aid))}
            for who in ("anna", "ben", "chidi", "stan")}
    for k, v in out2.items():
        print("PUBLIC-AFTER", k, v)
    assert out["ben"]["E"] == 404 and out["chidi"]["E"] == 404
    assert out["chidi"]["benR"] == 404 and out["ben"]["chidiR"] == 404
    assert out["anna"]["benR"] == 200 and out["anna"]["chidiR"] == 200
    assert out2["stan"] == {"benR": 404, "chidiR": 404, "answer": 404}
    assert out2["chidi"]["benR"] == 404


def test_glean_prompt_node_shape_under_shared_entry(app):
    """The node read.py's _attach_prompt_node builds (private, read
    prompt), under the author's shared entry, through the ORM."""
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    anna = _user("anna")
    n = Node(user_id=anna.id, human_owner_id=anna.id, parent_id=e,
             node_type="user", privacy_level="private", ai_usage="chat")
    n.set_content("read prompt stand-in")
    _db.session.add(n)
    _db.session.commit()
    print("GLEAN-SHAPE private node under E became:", n.privacy_level,
          n.audience_root_id, "ben GET:", _seen(app, "ben", n.id))
    assert n.privacy_level == "private"


def test_a_node_inserted_under_a_shared_node_is_not_read_by_its_readers(app):
    """The generic rule: joining a thread's audience is an explicit act
    (circles.join_reply). A node of any level and type that is merely
    inserted under a shared node, by any code, is its writer's alone."""
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    reply = _reply(app, "ben", e, "BEN R").get_json()["id"]
    anna, ben = _user("anna"), _user("ben")
    llm = User(username="claude-x", twitter_id="llm-claude-x")
    _db.session.add(llm)
    _db.session.flush()
    made = []
    for parent in (e, reply):
        for level in ("private", "circles", "public"):
            for kind, author in (("user", anna), ("llm", llm), ("user", ben)):
                n = Node(user_id=author.id, human_owner_id=(
                             anna.id if author is llm else author.id),
                         parent_id=parent, node_type=kind,
                         privacy_level=level, ai_usage="chat")
                n.set_content("INSERTED UNDER A SHARED NODE")
                _db.session.add(n)
                made.append((n, level, author))
    _db.session.commit()
    for n, level, author in made:
        assert n.audience_root_id is None
        assert n.privacy_level == level
        owner = "ben" if author is ben else "anna"
        other = "anna" if owner == "ben" else "ben"
        if level != "public":
            assert _seen(app, other, n.id) == 404, (n.id, level, owner)
            assert _seen(app, "chidi", n.id) == 404
        assert _seen(app, owner, n.id) == 200


def test_every_path_that_creates_a_node_under_a_shared_entry(app, monkeypatch):
    """One walk: the explicit reply paths join the thread (the reader
    and the author read the reply); every other path leaves what it
    creates with its writer."""
    from backend.models import Draft
    app.config["GLEAN_FOR_ALL"] = True
    import backend.routes.read as rd
    monkeypatch.setattr(rd, "_resolve_model", lambda node: ("claude-x", None))
    _db.session.add(User(username="claude-x", twitter_id="llm-claude-x"))
    anna_u = _user("anna")
    anna_u.glean_enabled = True
    _db.session.commit()
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    anna, ben = _client(app, "anna"), _client(app, "ben")

    def new_under(parent, before):
        return [n for n in Node.query.filter(Node.id.notin_(before)).all()]

    def ids():
        return {i for (i,) in _db.session.query(Node.id)}

    joined, kept = [], []

    # 1. Replies into the thread: typed, recorded (two routes), uploaded.
    before = ids()
    assert _reply(app, "ben", e, "typed").status_code == 201
    r = ben.post("/api/nodes/streaming/init", json={
        "parent_id": e, "privacy_level": "circles", "ai_usage": "chat"})
    assert r.status_code in (200, 201), r.get_json()
    r = ben.post("/api/nodes/upload/init", json={
        "filename": "a.mp3", "filesize": 10, "total_chunks": 1,
        "upload_id": "0123456789abcdef0123456789abcdef", "parent_id": e,
        "privacy_level": "circles", "ai_usage": "chat"})
    assert r.status_code in (200, 201), r.get_json()
    r = ben.post("/api/drafts/streaming/init", json={
        "parent_id": e, "privacy_level": "circles", "ai_usage": "chat"})
    assert r.status_code == 201
    d = Draft.query.filter_by(session_id=r.get_json()["session_id"]).one()
    d.streaming_status = "completed"
    d.set_content("recorded reply")
    _db.session.commit()
    assert ben.post(f"/api/drafts/streaming/{d.session_id}/save-as-node",
                    json={}).status_code in (200, 201)
    #    ... and an AI reply, the reader's (under his typed reply: the
    #    public rule) and the author's: in the thread (its pointer), but
    #    private like every new AI reply (Peter, 2026-11-12), read by its
    #    requester alone until shared.
    typed = Node.query.filter(Node.id.notin_(before), Node.parent_id == e,
                              Node.user_id == _user("ben").id,
                              Node.node_type == "user").order_by(
                                  Node.id).first().id
    assert ben.post(f"/api/nodes/{typed}/llm", json={}).status_code == 202
    assert anna.post(f"/api/nodes/{e}/llm", json={}).status_code == 202
    joined = new_under(e, before)
    assert len(joined) == 6, [(n.id, n.node_type) for n in joined]
    for n in joined:
        assert n.audience_root_id == e, n.id
        if n.node_type == "llm":
            assert n.privacy_level == "private", n.id
            asker = "ben" if n.human_owner_id == _user("ben").id else "anna"
            other = "anna" if asker == "ben" else "ben"
            assert _seen(app, asker, n.id) == 200
            assert _seen(app, other, n.id) == 404, n.id
        else:
            assert n.privacy_level == "circles", n.id
            assert _seen(app, "anna", n.id) == 200

    # 2. Everything else the author starts from her shared entry.
    before = ids()
    anna.post(f"/api/voice/from-node/{e}", json={})
    anna.post(f"/api/read/from-node/{e}", json={})
    anna.post(f"/api/textmode/from-node/{e}", json={
        "content": "ANNA TEXT MODE", "auto_generate": False,
        "ai_usage": "chat"})
    anna.post(f"/api/nodes/{e}/link", json={"content": "a link"})
    kept = new_under(e, before)
    assert len(kept) >= 3, [(n.id, n.node_type, n.parent_id) for n in kept]
    for n in kept:
        assert n.audience_root_id is None, (n.id, n.node_type, n.parent_id)
        assert _seen(app, "ben", n.id) == 404, (n.id, n.node_type)
        assert _seen(app, "anna", n.id) == 200

    # 3. And what a reader starts from his own reply in her thread.
    before = ids()
    rid = _reply(app, "ben", e, "ben again").get_json()["id"]
    ben.post(f"/api/textmode/from-node/{rid}", json={
        "content": "BEN TEXT MODE", "auto_generate": False,
        "ai_usage": "chat"})
    ben.post(f"/api/voice/from-node/{rid}", json={})
    for n in new_under(rid, before):
        if n.id == rid:
            continue
        assert n.audience_root_id is None, (n.id, n.node_type)
        assert _seen(app, "anna", n.id) == 404, (n.id, n.node_type)
