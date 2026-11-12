"""Circles through the full create_app(): the review's probes of #631,
kept as tests. Each asserts the rule that must hold.

anna writes; ben, chidi read through "Close"; dana is added to "Close"
with "Only new entries" after E; frank is in anna's other circle "Work";
gina is named for E and later hides anna; stan has the flag and no tie;
eve has no flag. No model is called (the task module is a MagicMock).
"""
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
for m in ("celery", "celery.utils", "celery.utils.log", "celery.result",
          "ffmpeg"):
    sys.modules.setdefault(m, MagicMock())

_mock_llm = MagicMock()
_res = MagicMock()
_res.id = "fake-task-id"
_mock_llm.generate_llm_response.delay.return_value = _res

import pytest  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
import backend.models as _real_models  # noqa: E402
from backend.models import (  # noqa: E402
    CircleMember, Node, User, UserNotification, Draft)

SECRET = "ANNAS-SHARED-WORDS"
PEOPLE = ("anna", "ben", "chidi", "dana", "frank", "gina", "stan", "eve")


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models" or k == "backend.tasks.llm_completion")


@pytest.fixture
def app(monkeypatch):
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_models
    sys.modules["backend.tasks.llm_completion"] = _mock_llm
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()

    def _fresh_login():
        # The fixture's app context outlives a request and Flask-Login
        # caches the user on g: without this every client acts as the
        # first one's user.
        from flask import g
        g.pop("_login_user", None)
    app.before_request_funcs.setdefault(None, []).insert(0, _fresh_login)
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
        "claude-x": {"provider": "anthropic", "api_model": "claude-x"},
    }
    from backend.utils import circles as cz
    monkeypatch.setattr(cz, "lookup_blocked", lambda uid: False)
    monkeypatch.setattr(cz, "record_lookup_miss", lambda uid: None)
    with app.app_context():
        _db.create_all()
        users = [User(username=n, default_ai_usage="chat", approved=True,
                      plan="alpha", email=f"{n}@example.com",
                      public_sharing_enabled=True)
                 for n in PEOPLE]
        _db.session.add_all(users)
        _db.session.commit()
        app.config["CIRCLES_V1"] = False
        app.config["CIRCLES_USER_IDS"] = {
            u.id for u in users if u.username != "eve"}
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _user(name):
    return User.query.filter_by(username=name).first()


def _client(app, name):
    c = app.test_client()
    if name is None:
        return c
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return c


def _circle(app, owner, name, members=(), include_existing=True):
    c = _client(app, owner)
    r = c.post("/api/circles", json={"name": name})
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["circle"]["id"]
    for m in members:
        r = c.post(f"/api/circles/{cid}/members",
                   json={"username": m, "include_existing": include_existing})
        assert r.status_code == 201, r.get_json()
    return cid


def _share(app, owner="anna", content=SECRET, circle_ids=(), names=(),
           ai_usage="chat", parent_id=None):
    r = _client(app, owner).post("/api/nodes/", json={
        "content": content, "privacy_level": "circles", "ai_usage": ai_usage,
        "circle_ids": list(circle_ids), "circle_usernames": list(names),
        "parent_id": parent_id})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _reply(app, who, parent_id, content="a reply", privacy="circles",
           ai_usage="chat"):
    return _client(app, who).post("/api/nodes/", json={
        "content": content, "privacy_level": privacy, "ai_usage": ai_usage,
        "parent_id": parent_id})


def _world(app):
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    _circle(app, "anna", "Work", ["frank"])
    e = _share(app, circle_ids=[close], names=["gina"])
    # dana joins Close with "Only new entries" after E.
    c = _client(app, "anna")
    r = c.post(f"/api/circles/{close}/members",
               json={"username": "dana", "include_existing": False})
    assert r.status_code == 201
    # Make sure E's share time is strictly older than dana's cut-off.
    from backend.models import NodeCircle
    NodeCircle.query.filter_by(node_id=e).update(
        {"shared_at": datetime.utcnow() - timedelta(days=1)})
    _db.session.commit()
    r = _reply(app, "ben", e, "BEN-REPLY-WORDS")
    assert r.status_code == 201, r.get_json()
    ben_reply = r.get_json()["id"]
    # His AI reply, under his own reply (the public rule).
    r = _client(app, "ben").post(f"/api/nodes/{ben_reply}/llm",
                                 json={})
    assert r.status_code == 202, r.get_json()
    answer = r.get_json()["node_id"]
    a = _db.session.get(Node, answer)
    a.set_content("ANSWER-DERIVED-FROM-ANNA")
    a.llm_task_status = "completed"
    _db.session.commit()
    # chidi removed; gina hides anna.
    assert c.delete(f"/api/circles/{close}/members/chidi").status_code == 200
    assert _client(app, "gina").post(
        "/api/circles/mutes", json={"username": "anna"}).status_code == 200
    control = _client(app, "anna").post("/api/nodes/", json={
        "content": "ANNA PRIVATE CONTROL", "privacy_level": "private",
        "ai_usage": "chat"}).get_json()["id"]
    return dict(close=close, e=e, ben_reply=ben_reply, answer=answer,
                control=control)


OUTSIDERS = (None, "stan", "chidi", "dana", "frank", "gina", "eve")

GET_ROUTES = (
    "/api/nodes/{id}", "/api/nodes/{id}/audience",
    "/api/nodes/{id}/resolve-quotes", "/api/nodes/titles?ids={id}",
    "/api/nodes/{id}/audio", "/api/nodes/{id}/audio-chunks",
    "/api/nodes/{id}/audio-download", "/api/nodes/{id}/tts-status",
    "/api/nodes/{id}/tts-chapters", "/api/nodes/{id}/llm-status",
    "/api/nodes/{id}/transcription-status", "/api/nodes/{id}/streaming-status",
    "/api/nodes/{id}/suggested-model", "/api/nodes/{id}/delete-impact",
    "/api/nodes/{id}/feed-picks", "/api/textmode/from-node/{id}",
    "/api/commons/node/{id}", "/node/{id}",
    "/api/circles/shared-with-me?before={id}",
)
POST_ROUTES = (
    ("POST", "/api/nodes/{id}/tts", {}),
    ("POST", "/api/nodes/{id}/llm", {}),
    ("POST", "/api/nodes/{id}/llm", {}),
    ("POST", "/api/nodes/", {"content": "x", "parent_id": "{id}",
                             "privacy_level": "circles", "ai_usage": "chat"}),
    ("POST", "/api/nodes/", {"content": "x", "parent_id": "{id}",
                             "privacy_level": "private", "ai_usage": "chat"}),
    ("POST", "/api/nodes/", {"content": "x", "parent_id": "{id}",
                             "privacy_level": "public", "ai_usage": "chat"}),
    ("POST", "/api/nodes/{id}/link", {"content": "x"}),
    ("PUT", "/api/nodes/{id}", {"content": "x"}),
    ("PUT", "/api/nodes/{id}/audience", {"circle_usernames": ["ben"]}),
    ("POST", "/api/nodes/{id}/pin", {}),
    ("PUT", "/api/nodes/{id}/thread-name", {"name": "x"}),
    ("POST", "/api/textmode/from-node/{id}", {"content": "x"}),
    ("POST", "/api/voice/from-node/{id}", {}),
    ("POST", "/api/read/from-node/{id}", {}),
    ("POST", "/api/drafts/streaming/init", {"parent_id": "{id}"}),
    ("POST", "/api/drafts/", {"parent_id": "{id}", "content": "x"}),
    ("POST", "/api/share/save-proposal", {"node_id": "{id}"}),
    ("POST", "/api/nodes/upload/init", {"parent_id": "{id}",
                                        "filename": "a.webm",
                                        "total_chunks": 1,
                                        "privacy_level": "circles"}),
    ("POST", "/api/nodes/streaming/init", {"parent_id": "{id}",
                                           "privacy_level": "circles"}),
    ("DELETE", "/api/nodes/{id}", {}),
)

MISSING = 987654


def _fill(obj, nid):
    if isinstance(obj, str):
        return obj.replace("{id}", str(nid))
    if isinstance(obj, dict):
        return {k: (nid if v == "{id}" else _fill(v, nid))
                for k, v in obj.items()}
    return obj


def _call(c, method, path, body):
    if method == "GET":
        r = c.get(path)
    else:
        r = c.open(path, method=method, json=body)
    try:
        data = r.get_json(silent=True)
    except Exception:  # noqa: BLE001
        data = None
    text = r.get_data(as_text=True)
    return r.status_code, data, text


def test_outsiders_cannot_tell_shared_nodes_from_missing(app):
    w = _world(app)
    problems = []
    for who in OUTSIDERS:
        for key in ("e", "ben_reply", "answer"):
            nid = w[key]
            for path in GET_ROUTES:
                c = _client(app, who)
                got = _call(c, "GET", _fill(path, nid), None)
                c = _client(app, who)
                miss = _call(c, "GET", _fill(path, w["control"]), None)
                MISS = w["control"]
                leak = any(s in got[2] for s in (
                    SECRET, "BEN-REPLY-WORDS", "ANSWER-DERIVED"))
                same = (got[0] == miss[0]
                        and got[2].replace(str(nid), "N")
                        == miss[2].replace(str(MISS), "N"))
                if path == "/node/{id}":
                    # The server-rendered shell: a built frontend's file
                    # hashes hold digits the id replacement would touch.
                    same = got[0] == miss[0]
                if leak or not same:
                    problems.append((who, key, "GET", path, got[0], miss[0],
                                     got[2][:160], miss[2][:160]))
            for method, path, body in POST_ROUTES:
                c = _client(app, who)
                got = _call(c, method, _fill(path, nid), _fill(body, nid))
                _db.session.rollback()
                c = _client(app, who)
                MISS = w["control"]
                miss = _call(c, method, _fill(path, MISS),
                             _fill(body, MISS))
                _db.session.rollback()
                leak = any(s in got[2] for s in (
                    SECRET, "BEN-REPLY-WORDS", "ANSWER-DERIVED"))
                same = (got[0] == miss[0]
                        and got[2].replace(str(nid), "N")
                        == miss[2].replace(str(MISS), "N"))
                if leak or not same:
                    problems.append((who, key, method, path, got[0], miss[0],
                                     got[2][:160], miss[2][:160]))
    # Nothing was created or changed by an outsider.
    out = "\n".join(repr(p) for p in problems)
    assert not problems, out[:4000]


def test_shared_with_me_before_anchor_reacts_to_any_node_id(app):
    """?before=<id> loads any node by id, readable or not."""
    close = _circle(app, "anna", "Close", ["ben"])
    old = _share(app, circle_ids=[close], content="OLD")
    # stan's private node, created between the two shares.
    r = _client(app, "stan").post("/api/nodes/", json={
        "content": "stan private", "privacy_level": "private",
        "ai_usage": "chat"})
    stan_private = r.get_json()["id"]
    new = _share(app, circle_ids=[close], content="NEW")
    t0 = datetime.utcnow()
    for i, nid in enumerate((old, stan_private, new)):
        n = _db.session.get(Node, nid)
        n.created_at = t0 + timedelta(minutes=i)
    _db.session.commit()
    ben = _client(app, "ben")

    def ids(q):
        d = ben.get("/api/circles/shared-with-me" + q).get_json()
        return [e["id"] for p in d["people"] for e in p["entries"]]
    assert ids(f"?before={stan_private}") == ids(f"?before={MISSING}"), (
        "an unreadable node id changes the page: it exists, and its time "
        "falls between two of ben's entries")


def test_reader_can_create_a_public_reply_under_the_entry_via_recording(app):
    """Decision 8: a reader cannot make a reply public. The recording
    path (drafts streaming -> save-as-node) never asks
    reply_requires_circles."""
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    ben = _client(app, "ben")
    r = ben.post("/api/drafts/streaming/init", json={
        "parent_id": e, "privacy_level": "public", "ai_usage": "chat"})
    assert r.status_code == 201
    sid = r.get_json()["session_id"]
    d = Draft.query.filter_by(session_id=sid).one()
    d.streaming_status = "completed"
    d.set_content("BEN SPOKEN REPLY")
    _db.session.commit()
    r = ben.post(f"/api/drafts/streaming/{sid}/save-as-node", json={})
    print("save", r.status_code, (r.get_json() or {}).keys())
    made = Node.query.filter(Node.parent_id == e,
                             Node.user_id == _user("ben").id).all()
    assert not any(n.privacy_level == "public" for n in made), (
        "a reader's reply under a shared entry was saved as public")


def test_reader_textmode_from_own_reply_shares_his_prompt_node(app):
    """Decision 7: a thread's system prompt is never shared. A reader's
    Text-mode continuation from his own reply (auto_generate off) hangs
    his textmode prompt node under it as 'circles', which then joins the
    thread's audience by the before_insert hook."""
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    e = _share(app, circle_ids=[close])
    r = _reply(app, "ben", e, "ben reply")
    rid = r.get_json()["id"]
    ben = _client(app, "ben")
    r = ben.post(f"/api/textmode/from-node/{rid}", json={
        "content": "BEN TEXTMODE MESSAGE", "auto_generate": False,
        "ai_usage": "chat"})
    prompt_nodes = Node.query.filter(Node.parent_id == rid).all()
    rows = [(n.id, n.privacy_level, n.audience_root_id, n.is_system_prompt,
             n.get_prompt_key()) for n in prompt_nodes]
    shared_prompts = [n for n in prompt_nodes
                      if n.get_prompt_key() and n.privacy_level == "circles"
                      and n.audience_root_id == e]
    if shared_prompts:
        p = shared_prompts[0]
        seen = _client(app, "chidi").get(f"/api/nodes/{p.id}")
        print("chidi reads ben's prompt node:", seen.status_code,
              (seen.get_json() or {}).get("prompt_title"),
              repr(((seen.get_json() or {}).get("content") or "")[:200]),
              (seen.get_json() or {}).get("context_artifacts"))
        seen = _client(app, "anna").get(f"/api/nodes/{p.id}")
        print("anna reads ben's prompt node:", seen.status_code)
    assert not shared_prompts, rows


def test_removing_an_author_from_the_list_leaves_her_entries_open(app):
    """The flag gates the feature, not what is already shared: an author
    taken off CIRCLES_USER_IDS keeps her shared entries open to readers
    who still have the flag (stated in the PR). What she can still do is
    take an entry back, and that closes it for good: her audience rows
    go, so a later "circles" edit does not reopen it."""
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    assert _client(app, "ben").get(f"/api/nodes/{e}").status_code == 200
    # Rollout list changes: anna no longer has Circles; ben still does.
    app.config["CIRCLES_USER_IDS"] = {
        _user(n).id for n in PEOPLE if n not in ("eve", "anna")}
    st = _client(app, "ben").get(f"/api/nodes/{e}").status_code
    # anna takes it back to Only me, then edits it to circles again
    # (her client no longer offers Circles; the API accepts it).
    c = _client(app, "anna")
    assert c.get(f"/api/nodes/{e}/audience").status_code == 404
    r1 = c.put(f"/api/nodes/{e}", json={"privacy_level": "private", "content": SECRET})
    from backend.models import NodeCircle
    c.put(f"/api/nodes/{e}", json={"privacy_level": "circles", "content": SECRET})
    st2 = _client(app, "ben").get(f"/api/nodes/{e}").status_code
    assert st == 200
    assert r1.status_code == 200
    assert NodeCircle.query.filter_by(node_id=e).count() == 0
    assert st2 == 404


def test_a_person_removed_from_one_circle_reads_on_through_another(app):
    """Access is the union of the entry's circles and names: removing a
    person from one circle closes what they read through that circle
    only. The Remove dialog says so."""
    close = _circle(app, "anna", "Close", ["ben"])
    work = _circle(app, "anna", "Work", ["ben"])
    e = _share(app, circle_ids=[close, work])
    c = _client(app, "anna")
    assert c.delete(f"/api/circles/{close}/members/ben").status_code == 200
    st = _client(app, "ben").get(f"/api/nodes/{e}").status_code
    assert st == 200
    assert c.delete(f"/api/circles/{work}/members/ben").status_code == 200
    assert _client(app, "ben").get(f"/api/nodes/{e}").status_code == 404


def test_pinned_answer_stays_in_readers_log_after_entry_deleted(app):
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    ben = _client(app, "ben")
    mine = _reply(app, "ben", e, "BEN R").get_json()["id"]
    r = ben.post(f"/api/nodes/{mine}/llm", json={})
    aid = r.get_json()["node_id"]
    a = _db.session.get(Node, aid)
    a.set_content("ANSWER-DERIVED-FROM-ANNA")
    a.llm_task_status = "completed"
    _db.session.commit()
    pr = ben.post(f"/api/nodes/{aid}/pin")
    log = ben.get("/api/log").get_data(as_text=True)
    # anna deletes the entry (this node only).
    d = _client(app, "anna").delete(f"/api/nodes/{e}")
    assert ben.get(f"/api/nodes/{aid}").status_code == 404
    log = ben.get("/api/log").get_data(as_text=True)
    assert "ANSWER-DERIVED" not in log


def _answer(app, who, parent):
    """*who*'s AI reply under *parent*, written, then shared with the
    thread by *who* (every AI reply is created private and shared by its
    requester once it is written, Peter 2026-11-12). Under someone
    else's node *who* first writes a reply of their own and asks under
    it (the public rule, Peter 2026-11-12)."""
    node = _db.session.get(Node, parent)
    if (node.human_owner_id or node.user_id) != _user(who).id:
        r = _reply(app, who, parent, f"{who.upper()}-OWN-REPLY")
        assert r.status_code == 201, r.get_json()
        parent = r.get_json()["id"]
    r = _client(app, who).post(f"/api/nodes/{parent}/llm",
                               json={})
    assert r.status_code == 202, r.get_json()
    aid = r.get_json()["node_id"]
    a = _db.session.get(Node, aid)
    a.set_content("ANSWER-DERIVED-FROM-ANNA")
    a.llm_task_status = "completed"
    _db.session.commit()
    r = _client(app, who).put(f"/api/nodes/{aid}", json={
        "content": "ANSWER-DERIVED-FROM-ANNA", "privacy_level": "circles"})
    assert r.status_code == 200, r.get_json()
    return aid


@pytest.mark.parametrize("how", ["removed", "taken_back", "other_audience",
                                 "circle_deleted"])
def test_answer_after_the_reader_loses_the_entry(app, how):
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    aid = _answer(app, "ben", e)
    anna = _client(app, "anna")
    if how == "removed":
        assert anna.delete(
            f"/api/circles/{close}/members/ben").status_code == 200
    elif how == "taken_back":
        assert anna.put(f"/api/nodes/{e}", json={
            "privacy_level": "private", "content": SECRET}).status_code == 200
    elif how == "other_audience":
        assert anna.put(f"/api/nodes/{e}", json={
            "privacy_level": "circles", "circle_ids": [], "content": SECRET,
            "circle_usernames": ["chidi"]}).status_code == 200
    else:
        assert anna.delete(f"/api/circles/{close}").status_code == 200
    ben = _client(app, "ben")
    assert ben.get(f"/api/nodes/{e}").status_code == 404
    r = ben.get(f"/api/nodes/{aid}")
    assert r.status_code == 404, (
        "the entry is closed to ben, its derived answer is not")
    assert "ANSWER-DERIVED" not in ben.get(
        "/api/search?q=ANSWER").get_data(as_text=True)
    # It opens again when the entry does.
    if how == "circle_deleted":
        return
    if how == "removed":
        assert anna.post(f"/api/circles/{close}/members", json={
            "username": "ben", "include_existing": True}).status_code == 201
    else:
        assert anna.put(f"/api/nodes/{e}", json={
            "privacy_level": "circles", "circle_ids": [close],
            "circle_usernames": [], "content": SECRET}).status_code == 200
    assert ben.get(f"/api/nodes/{e}").status_code == 200
    assert ben.get(f"/api/nodes/{aid}").status_code == 200


def test_closed_answer_on_owner_routes_after_entry_deleted(app):
    close = _circle(app, "anna", "Close", ["ben"])
    e = _share(app, circle_ids=[close])
    aid = _answer(app, "ben", e)
    ben = _client(app, "ben")
    assert _client(app, "anna").delete(f"/api/nodes/{e}").status_code == 200
    assert ben.get(f"/api/nodes/{aid}").status_code == 404
    out = {}
    for path in (f"/api/nodes/{aid}/llm-status", "/api/search?q=ANSWER",
                 f"/api/textmode/from-node/{aid}",
                 f"/api/nodes/titles?ids={aid}",
                 f"/api/nodes/{aid}/delete-impact"):
        r = ben.get(path)
        out[path] = (r.status_code, "ANSWER-DERIVED" in r.get_data(
            as_text=True))
    for k, v in out.items():
        print("CLOSED-ANSWER", k, v)
    assert not any(v[1] for v in out.values())


@pytest.mark.parametrize("flag_off", [False, True])
@pytest.mark.parametrize("entry_state", ["shared", "taken_back", "deleted",
                                         "purged"])
def test_reader_nodes_never_widen(app, flag_off, entry_state):
    close = _circle(app, "anna", "Close", ["ben", "chidi"])
    e = _share(app, circle_ids=[close])
    r = _reply(app, "ben", e, "BEN R").get_json()["id"]
    r2 = _reply(app, "ben", r, "BEN R2").get_json()["id"]
    aid = _answer(app, "ben", r2)
    # a nested private note under the answer
    r3 = _reply(app, "ben", aid, "BEN R3", privacy="circles").get_json()["id"]
    anna = _client(app, "anna")
    if entry_state == "taken_back":
        assert anna.put(f"/api/nodes/{e}", json={
            "privacy_level": "private", "content": SECRET}).status_code == 200
    elif entry_state == "deleted":
        assert anna.delete(f"/api/nodes/{e}").status_code == 200
    elif entry_state == "purged":
        from sqlalchemy import text
        _db.session.execute(text("UPDATE node SET parent_id = NULL "
                                 "WHERE parent_id = :e"), {"e": e})
        from backend.models import NodeCircle, NodeReader
        NodeCircle.query.filter_by(node_id=e).delete()
        UserNotification.query.filter_by(node_id=e).delete()
        _db.session.execute(text("DELETE FROM node WHERE id = :e"), {"e": e})
        _db.session.commit()
    if flag_off:
        app.config["CIRCLES_USER_IDS"] = {
            _user(n).id for n in PEOPLE if n not in ("eve", "ben")}
    ben = _client(app, "ben")
    widened = []
    for nid in (r, r2, aid, r3):
        content = _db.session.get(Node, nid).get_content()
        for body in ({"privacy_level": "public"},
                     {"privacy_level": "public", "apply_to_descendants": True},
                     {"privacy_level": "circles",
                      "circle_usernames": ["chidi", "dana"]},
                     {"privacy_level": "private"},
                     {"privacy_level": "public"},
                     {"privacy_level": "private",
                      "apply_to_descendants": True},
                     {"privacy_level": "public",
                      "apply_to_descendants": True}):
            body = dict(body, content=content)
            ben.put(f"/api/nodes/{nid}", json=body)
            _db.session.rollback()
        ben.put(f"/api/nodes/{nid}/audience",
                json={"circle_usernames": ["dana"],
                      "apply_to_descendants": True})
        _db.session.rollback()
    from backend.models import NodeReader
    for nid in (r, r2, aid, r3):
        n = _db.session.get(Node, nid)
        if n.privacy_level == "public" or (
                n.audience_root_id == n.id) or NodeReader.query.filter_by(
                    node_id=nid).count():
            widened.append((nid, n.privacy_level, n.audience_root_id))
    assert not widened


def test_a_members_model_through_the_route(app):
    close = _circle(app, "anna", "Close", ["ben"])
    anna = _user("anna")
    llm_user = User(username="claude-x", twitter_id="llm-claude-x")
    _db.session.add(llm_user)
    _db.session.flush()
    root = Node(user_id=anna.id, human_owner_id=anna.id, node_type="user",
                privacy_level="private", ai_usage="chat")
    root.set_content("anna private")
    _db.session.add(root)
    _db.session.flush()
    her_ai = Node(user_id=llm_user.id, human_owner_id=anna.id,
                  parent_id=root.id, node_type="llm", llm_model="claude-x",
                  privacy_level="private", ai_usage="chat")
    her_ai.set_content("her private AI reply")
    _db.session.add(her_ai)
    ben = _user("ben")
    ben.preferred_model = "gpt-5"
    _db.session.commit()
    e = _share(app, circle_ids=[close], parent_id=her_ai.id)
    mine = _reply(app, "ben", e, "BEN R").get_json()["id"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm",
                                 json={})
    a = _db.session.get(Node, r.get_json()["node_id"])
    seen = _client(app, "ben").get(f"/api/nodes/{a.id}").get_json()
    assert a.llm_model == "gpt-5"
