"""Tests for Circles: entries shared with people their author chose.

Every access rule of the feature, through the real routes: a reader
reads, a removed reader and a reader who hid the author do not, the
"Only new entries" cut-off, names for one entry only, logged-out and
public routes, replies read by exactly the thread's audience, the notes,
what a model may read (another person's shared entry only for an AI
reply its reader asks for in that thread), the export, and the flag.

anna writes; ben, chidi and dana read; eve has no Circles flag.
In-memory SQLite, ENCRYPTION_DISABLED, the LLM task module mocked: no
model is ever called.
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

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    Circle, CircleMember, CircleMute, Node, NodeCircle, NodeReader, User,
    UserNotification,
)
import backend.models as _real_backend_models    # noqa: E402
# With the real flask_login, before a sibling test module can mock it.
import backend.utils.privacy  # noqa: E402,F401

PEOPLE = ("anna", "ben", "chidi", "dana")
SECRET = "ANNAS-SHARED-WORDS"


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["FRONTEND_BUILD_DIR"] = None
    app.config["PUBLIC_BASE_URL"] = "https://loore.org"
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
        "claude-x": {"provider": "anthropic", "api_model": "claude-x"},
    }
    _db.init_app(app)
    login_manager = LoginManager(app)

    @app.before_request
    def _fresh_login():
        # The fixture's app context outlives a request, and Flask-Login
        # caches the user on g: without this a second client would act
        # as the first one's user.
        from flask import g
        g.pop("_login_user", None)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.circles import circles_bp
    from backend.routes.commons import commons_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.log import log_bp
    from backend.routes.nodes import nodes_bp
    from backend.routes.public_pages import public_pages_bp
    from backend.routes.textmode import textmode_bp
    from backend.routes.updates import updates_bp
    app.register_blueprint(circles_bp, url_prefix="/api/circles")
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(log_bp, url_prefix="/api")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(updates_bp, url_prefix="/api/updates")
    app.register_blueprint(public_pages_bp)
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.tasks.llm_completion"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        users = [User(username=name, default_ai_usage="chat", approved=True,
                      email=f"{name}@example.com")
                 for name in PEOPLE + ("eve",)]
        _db.session.add_all(users)
        _db.session.commit()
        app.config["CIRCLES_V1"] = False
        app.config["CIRCLES_USER_IDS"] = {
            u.id for u in users if u.username != "eve"}
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """The lookup limit lives in Redis; tests never touch one. And each
    test starts without the process-wide memory of whether any thread
    pointer exists (tests change the flag mid-process)."""
    from backend.utils import circles as cz
    from backend.utils import privacy
    privacy._pointers_seen["at"] = 0.0
    monkeypatch.setattr(cz, "lookup_blocked", lambda uid: False)
    monkeypatch.setattr(cz, "record_lookup_miss", lambda uid: None)


def _user(name):
    return User.query.filter_by(username=name).first()


def _client(app, name):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return client


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
    assert r.get_json()["audience_set"] is True
    return r.get_json()["id"]


def _reply(app, who, parent_id, content="a reply", privacy="circles",
           ai_usage="chat"):
    return _client(app, who).post("/api/nodes/", json={
        "content": content, "privacy_level": privacy, "ai_usage": ai_usage,
        "parent_id": parent_id})


def _can_read(app, who, node_id):
    return _client(app, who).get(f"/api/nodes/{node_id}").status_code == 200


def _shared_ids(app, who):
    data = _client(app, who).get("/api/circles/shared-with-me").get_json()
    return [e["id"] for p in data["people"] for e in p["entries"]]


# ── Circles and people ──────────────────────────────────────────────────

def test_only_the_owner_sees_a_circle(app):
    cid = _circle(app, "anna", "Close friends", ["ben", "chidi"])
    mine = _client(app, "anna").get("/api/circles").get_json()["circles"]
    assert [c["name"] for c in mine] == ["Close friends"]
    assert [m["username"] for m in mine[0]["members"]] == ["ben", "chidi"]
    ben = _client(app, "ben")
    assert ben.get("/api/circles").get_json()["circles"] == []
    assert ben.put(f"/api/circles/{cid}", json={"name": "x"}).status_code == 404
    assert ben.delete(f"/api/circles/{cid}").status_code == 404
    assert ben.post(f"/api/circles/{cid}/members", json={
        "username": "dana", "include_existing": True}).status_code == 404
    assert ben.delete(f"/api/circles/{cid}/members/chidi").status_code == 404


def test_create_rename_delete(app):
    c = _client(app, "anna")
    cid = _circle(app, "anna", "Family")
    assert c.post("/api/circles", json={"name": "family"}).status_code == 409
    assert c.post("/api/circles", json={"name": "  "}).status_code == 400
    r = c.put(f"/api/circles/{cid}", json={"name": "Kin"})
    assert r.get_json()["circle"]["name"] == "Kin"
    assert c.delete(f"/api/circles/{cid}").status_code == 200
    assert Circle.query.count() == 0


def test_a_person_is_found_by_exact_name_only(app):
    cid = _circle(app, "anna", "Close friends")
    c = _client(app, "anna")

    def preview(name):
        return c.post(f"/api/circles/{cid}/members/preview",
                      json={"username": name})
    ok = preview("@Ben")
    assert ok.status_code == 200 and ok.get_json()["username"] == "ben"
    # A prefix, an email, herself, an account without Circles and a name
    # nobody has all get the same plain answer.
    answers = set()
    for name in ("be", "ben@example.com", "anna", "eve", "nobody", "%"):
        r = preview(name)
        assert r.status_code == 404, name
        answers.add(json.dumps(r.get_json(), sort_keys=True))
    assert len(answers) == 1
    assert "No one on Loore has that name" in answers.pop()
    # Nothing in any answer carries an email address.
    assert "example.com" not in json.dumps(ok.get_json())
    assert "example.com" not in json.dumps(c.get("/api/circles").get_json())


def test_ai_and_hidden_accounts_cannot_be_added(app):
    cid = _circle(app, "anna", "Close friends")
    llm = User(username="gpt-5", twitter_id="llm-gpt-5")
    gone = User(username="gone", approved=True, deleted_at=datetime.utcnow())
    _db.session.add_all([llm, gone])
    _db.session.commit()
    app.config["CIRCLES_V1"] = True
    c = _client(app, "anna")
    for name in ("gpt-5", "gone"):
        assert c.post(f"/api/circles/{cid}/members", json={
            "username": name, "include_existing": True}).status_code == 404


def test_the_name_lookup_is_limited(app, monkeypatch):
    from backend.utils import circles as cz
    misses = {"n": 0}
    monkeypatch.setattr(cz, "record_lookup_miss",
                        lambda uid: misses.__setitem__("n", misses["n"] + 1))
    monkeypatch.setattr(
        cz, "lookup_blocked",
        lambda uid: misses["n"] >= cz.LOOKUP_MISSES_PER_HOUR)
    cid = _circle(app, "anna", "Close friends")
    c = _client(app, "anna")
    for i in range(cz.LOOKUP_MISSES_PER_HOUR):
        assert c.post(f"/api/circles/{cid}/members/preview",
                      json={"username": f"nobody{i}"}).status_code == 404
    r = c.post(f"/api/circles/{cid}/members/preview", json={"username": "ben"})
    assert r.status_code == 429


def test_the_routes_need_a_login_and_the_flag(app):
    anon = app.test_client()
    assert anon.get("/api/circles").status_code == 401
    assert anon.get("/api/circles/shared-with-me").status_code == 401
    eve = _client(app, "eve")
    assert eve.get("/api/circles").status_code == 404
    assert eve.post("/api/circles", json={"name": "x"}).status_code == 404
    assert eve.get("/api/circles/shared-with-me").status_code == 404


# ── Reading ─────────────────────────────────────────────────────────────

def test_a_reader_reads_and_nobody_else(app):
    cid = _circle(app, "anna", "Close friends", ["ben"])
    nid = _share(app, circle_ids=[cid])
    assert _can_read(app, "anna", nid)
    assert _can_read(app, "ben", nid)
    assert not _can_read(app, "chidi", nid)
    assert not _can_read(app, "eve", nid)
    r = _client(app, "ben").get(f"/api/nodes/{nid}").get_json()
    assert SECRET in r["content"]
    assert r["audience"]["author"] == "anna"
    assert r["audience"]["readers"] == ["ben"]
    assert r["audience"]["is_author"] is False
    # The circle's name and id never reach a reader.
    assert "circle_ids" not in r["audience"]
    assert "Close friends" not in json.dumps(r)
    mine = _client(app, "anna").get(f"/api/nodes/{nid}").get_json()
    assert mine["audience"]["circle_ids"] == [cid]
    assert _shared_ids(app, "ben") == [nid]
    assert _shared_ids(app, "chidi") == []


def test_logged_out_and_public_routes_never_serve_it(app):
    cid = _circle(app, "anna", "Close friends", ["ben"])
    nid = _share(app, circle_ids=[cid])
    anon = app.test_client()
    assert anon.get(f"/api/nodes/{nid}").status_code in (401, 302)
    assert anon.get(f"/api/commons/node/{nid}").status_code == 404
    page = anon.get(f"/node/{nid}")
    assert SECRET not in page.get_data(as_text=True)
    for path in ("/sitemap.xml", "/@anna", "/@anna/feed.xml"):
        r = anon.get(path)
        assert SECRET not in r.get_data(as_text=True), path
        assert f"/node/{nid}" not in r.get_data(as_text=True), path
    for who in ("ben", "chidi", "anna"):
        c = _client(app, who)
        feed = c.get("/api/commons/feed")
        assert SECRET not in feed.get_data(as_text=True)
        # The public thread endpoint stays public-only, also for a reader.
        assert c.get(f"/api/commons/node/{nid}").status_code == 404
    assert _db.session.get(Node, nid).public_slug is None


def test_a_removed_reader_is_closed_out_at_once(app):
    cid = _circle(app, "anna", "Close friends", ["ben", "chidi"])
    nid = _share(app, circle_ids=[cid])
    reply = _reply(app, "chidi", nid, "chidi says").get_json()["id"]
    assert _can_read(app, "ben", nid) and _can_read(app, "ben", reply)
    r = _client(app, "anna").delete(f"/api/circles/{cid}/members/ben")
    assert r.status_code == 200
    assert not _can_read(app, "ben", nid)
    assert not _can_read(app, "ben", reply)
    assert _shared_ids(app, "ben") == []
    assert _can_read(app, "chidi", nid)


def test_deleting_the_circle_or_taking_the_entry_back_closes_it(app):
    cid = _circle(app, "anna", "Close friends", ["ben"])
    nid = _share(app, circle_ids=[cid])
    other = _share(app, content="second", names=["ben"])
    assert _client(app, "anna").delete(f"/api/circles/{cid}").status_code == 200
    assert not _can_read(app, "ben", nid)
    assert _can_read(app, "anna", nid)
    # Back to Only me through the edit path.
    r = _client(app, "anna").put(f"/api/nodes/{other}", json={
        "content": "second", "privacy_level": "private"})
    assert r.status_code == 200
    assert not _can_read(app, "ben", other)
    assert NodeReader.query.filter_by(node_id=other).count() == 0
    # The entry keeps pointing at itself (its author keeps reading the
    # replies written into it); nobody is in its audience.
    assert _db.session.get(Node, other).audience_root_id == other
    assert _db.session.get(Node, other).privacy_level == "private"


def test_only_new_entries_cut_off(app):
    cid = _circle(app, "anna", "Close friends", ["ben"])
    old = _share(app, circle_ids=[cid], content="old entry")
    # The share is a moment in the past; dana joins "Only new entries".
    NodeCircle.query.filter_by(node_id=old).update(
        {"shared_at": datetime.utcnow() - timedelta(hours=1)})
    _db.session.commit()
    c = _client(app, "anna")
    r = c.post(f"/api/circles/{cid}/members/preview", json={"username": "dana"})
    assert r.get_json()["shared_count"] == 1
    assert c.post(f"/api/circles/{cid}/members", json={
        "username": "dana", "include_existing": False}).status_code == 201
    new = _share(app, circle_ids=[cid], content="new entry")
    assert not _can_read(app, "dana", old)
    assert _can_read(app, "dana", new)
    assert _shared_ids(app, "dana") == [new]
    assert _client(app, "ben").get(
        f"/api/nodes/{old}").get_json()["audience"]["readers"] == ["ben"]
    # "Add <name>": chidi also reads what was already shared.
    assert c.post(f"/api/circles/{cid}/members", json={
        "username": "chidi", "include_existing": True}).status_code == 201
    assert _can_read(app, "chidi", old) and _can_read(app, "chidi", new)
    # The choice must be stated.
    assert c.post(f"/api/circles/{cid}/members", json={
        "username": "ben"}).status_code == 400


def test_names_for_one_entry_only(app):
    nid = _share(app, names=["@ben", "dana"])
    assert Circle.query.count() == 0
    assert _can_read(app, "ben", nid) and _can_read(app, "dana", nid)
    assert not _can_read(app, "chidi", nid)
    anna = _client(app, "anna").get(f"/api/nodes/{nid}").get_json()
    assert anna["audience"]["usernames"] == ["ben", "dana"]
    assert anna["audience"]["readers"] == ["ben", "dana"]


def test_sharing_with_nobody_or_an_unknown_name_is_refused(app):
    c = _client(app, "anna")
    empty = _circle(app, "anna", "Family")

    def create(**audience):
        return c.post("/api/nodes/", json={
            "content": "x", "privacy_level": "circles", "ai_usage": "chat",
            **audience})
    r = create(circle_ids=[empty], circle_usernames=[])
    assert r.status_code == 400 and r.get_json()["code"] == "circle_no_readers"
    r = create(circle_ids=[], circle_usernames=["nobody"])
    assert r.status_code == 400
    assert "No one on Loore has that name" in r.get_json()["error"]
    assert create(circle_ids=[99999]).status_code == 400
    assert Node.query.count() == 0
    # Ben's circle is not anna's to share with.
    bens = _circle(app, "ben", "Bens", ["chidi"])
    assert create(circle_ids=[bens]).status_code == 400
    # An existing entry cannot become "circles" with nobody in it either.
    r = c.post("/api/nodes/", json={"content": "mine", "ai_usage": "chat"})
    nid = r.get_json()["id"]
    r = c.put(f"/api/nodes/{nid}", json={
        "content": "mine", "privacy_level": "circles"})
    assert r.status_code == 400
    assert _db.session.get(Node, nid).privacy_level == "private"


def test_circles_without_an_audience_behaves_as_private(app):
    """Old rows and old clients (#588): "circles" and nobody chosen."""
    anna = _user("anna")
    node = Node(user_id=anna.id, human_owner_id=anna.id,
                privacy_level="circles", ai_usage="chat")
    node.set_content("old circles row")
    _db.session.add(node)
    _db.session.commit()
    assert node.audience_root_id is None
    for who in ("ben", "chidi", "eve"):
        assert not _can_read(app, who, node.id)
    assert _can_read(app, "anna", node.id)
    # A new node saved "circles" with no audience fields (a client that
    # shares in a second step) is private until then.
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": "two-step", "privacy_level": "circles", "ai_usage": "chat"})
    assert r.status_code == 201 and r.get_json()["audience_set"] is False
    assert not _can_read(app, "ben", r.get_json()["id"])


def test_an_existing_entry_is_shared_changed_and_cascaded(app):
    c = _client(app, "anna")
    root = c.post("/api/nodes/", json={
        "content": "root", "ai_usage": "chat"}).get_json()["id"]
    child = c.post("/api/nodes/", json={
        "content": "my follow-up", "ai_usage": "chat",
        "parent_id": root}).get_json()["id"]
    r = c.put(f"/api/nodes/{root}", json={
        "content": "root", "privacy_level": "circles",
        "circle_usernames": ["ben"], "apply_to_descendants": True})
    assert r.status_code == 200, r.get_json()
    assert _can_read(app, "ben", root) and _can_read(app, "ben", child)
    # Change the audience: chidi in, ben out.
    r = c.put(f"/api/nodes/{root}", json={
        "content": "root", "privacy_level": "circles",
        "circle_usernames": ["chidi"]})
    assert r.status_code == 200
    assert not _can_read(app, "ben", root) and not _can_read(app, "ben", child)
    assert _can_read(app, "chidi", child)
    # A text-only edit keeps the audience.
    r = c.put(f"/api/nodes/{root}", json={
        "content": "root, edited", "privacy_level": "circles"})
    assert r.status_code == 200 and _can_read(app, "chidi", root)
    # Back to Only me, with the replies.
    r = c.put(f"/api/nodes/{root}", json={
        "content": "root, edited", "privacy_level": "private",
        "apply_to_descendants": True})
    assert r.status_code == 200
    assert not _can_read(app, "chidi", root)
    assert not _can_read(app, "chidi", child)
    assert _db.session.get(Node, child).privacy_level == "private"


def test_soft_deleted_and_hidden_accounts_stay_hidden(app):
    nid = _share(app, names=["ben"])
    reply = _reply(app, "chidi", nid).status_code
    assert reply == 404          # chidi is not a reader: the parent is unseen
    node = _db.session.get(Node, nid)
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    assert not _can_read(app, "ben", nid)
    assert _shared_ids(app, "ben") == []
    node.deleted_at = None
    _user("anna").deleted_at = datetime.utcnow()   # account in its grace period
    _db.session.commit()
    assert not _can_read(app, "ben", nid)
    assert _shared_ids(app, "ben") == []


# ── Replies ─────────────────────────────────────────────────────────────

def test_replies_are_read_by_exactly_the_audience(app):
    cid = _circle(app, "anna", "Close friends", ["ben", "chidi"])
    nid = _share(app, circle_ids=[cid])
    # A reply must be "circles", like a reply in a public thread is public.
    r = _reply(app, "ben", nid, "private try", privacy="private")
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_reply_required"
    r = _reply(app, "ben", nid, "ben replies")
    assert r.status_code == 201
    rid = r.get_json()["id"]
    assert _db.session.get(Node, rid).audience_root_id == nid
    for who in ("anna", "ben", "chidi"):
        assert _can_read(app, who, rid), who
    assert not _can_read(app, "dana", rid)
    assert _reply(app, "dana", nid).status_code == 404
    thread = _client(app, "chidi").get(f"/api/nodes/{nid}").get_json()
    assert [c["id"] for c in thread["children"]] == [rid]
    # A reply to the reply stays in the same audience.
    r2 = _reply(app, "chidi", rid, "chidi answers ben").get_json()["id"]
    assert _db.session.get(Node, r2).audience_root_id == nid
    assert _can_read(app, "anna", r2) and _can_read(app, "ben", r2)


def test_a_reader_cannot_change_the_threads_audience(app):
    nid = _share(app, names=["ben", "chidi"])
    rid = _reply(app, "ben", nid, "ben replies").get_json()["id"]
    ben = _client(app, "ben")
    # Not the entry.
    assert ben.put(f"/api/nodes/{nid}", json={
        "content": SECRET, "privacy_level": "public"}).status_code == 403
    # Not through his own reply: no audience of its own, never public.
    r = ben.put(f"/api/nodes/{rid}", json={
        "content": "ben replies", "privacy_level": "circles",
        "circle_usernames": ["dana"]})
    assert r.status_code == 400
    r = ben.put(f"/api/nodes/{rid}", json={
        "content": "ben replies", "privacy_level": "public"})
    assert r.status_code == 403
    assert not _can_read(app, "dana", rid)
    assert _db.session.get(Node, rid).privacy_level == "circles"
    # He may take his own reply back.
    r = ben.put(f"/api/nodes/{rid}", json={
        "content": "ben replies", "privacy_level": "private"})
    assert r.status_code == 200
    assert not _can_read(app, "chidi", rid)


def test_a_thread_made_public_keeps_readers_replies_out(app):
    nid = _share(app, names=["ben"])
    rid = _reply(app, "ben", nid, "ben in confidence").get_json()["id"]
    r = _client(app, "anna").put(f"/api/nodes/{nid}", json={
        "content": SECRET, "privacy_level": "public",
        "apply_to_descendants": True})
    assert r.status_code == 200
    assert _db.session.get(Node, rid).privacy_level == "circles"
    anon = app.test_client()
    thread = anon.get(f"/api/commons/node/{nid}")
    assert "ben in confidence" not in thread.get_data(as_text=True)
    assert not _can_read(app, "chidi", rid)
    assert _can_read(app, "ben", rid) and _can_read(app, "anna", rid)


# ── Notes and "Don't show <name>'s entries" ─────────────────────────────

def _notes(app, who):
    return [n for n in _client(app, who).get(
        "/api/updates").get_json()["notifications"]
        if n["type"] == "circle_share"]


def test_one_note_per_shared_entry_per_reader(app):
    cid = _circle(app, "anna", "Close friends", ["ben"])
    first = _share(app, circle_ids=[cid], content="first")
    second = _share(app, circle_ids=[cid], names=["ben"], content="second")
    notes = _notes(app, "ben")
    assert sorted(n["node_id"] for n in notes) == [first, second]
    assert notes[0]["title"] == "@anna shared an entry with you."
    assert notes[0]["actor_username"] == "anna"
    assert notes[0]["link"].startswith("/node/")
    assert "first" not in json.dumps(notes) and "second" not in json.dumps(notes)
    # Replies make no note; saving the same audience again makes none.
    _reply(app, "anna", first, "anna adds")
    _client(app, "anna").put(f"/api/nodes/{first}", json={
        "content": "first", "privacy_level": "circles", "circle_ids": [cid]})
    assert UserNotification.query.filter_by(
        user_id=_user("ben").id, type="circle_share").count() == 2
    assert _notes(app, "anna") == []
    # An existing entry newly opened to chidi: his note.
    _client(app, "anna").put(f"/api/nodes/{first}", json={
        "content": "first", "privacy_level": "circles", "circle_ids": [cid],
        "circle_usernames": ["chidi"]})
    assert [n["node_id"] for n in _notes(app, "chidi")] == [first]
    # A note for an entry that was taken back is not shown.
    _client(app, "anna").put(f"/api/nodes/{second}", json={
        "content": "second", "privacy_level": "private"})
    assert [n["node_id"] for n in _notes(app, "ben")] == [first]


def test_hiding_a_person_stops_notes_and_entries_and_can_be_undone(app):
    nid = _share(app, names=["ben", "chidi"])
    ben = _client(app, "ben")
    assert len(_notes(app, "ben")) == 1
    r = ben.post("/api/circles/mutes", json={"username": "anna"})
    assert r.status_code == 200
    assert _notes(app, "ben") == []
    assert not _can_read(app, "ben", nid)
    assert _shared_ids(app, "ben") == []
    later = _share(app, names=["ben"], content="later")
    assert not _can_read(app, "ben", later)
    assert UserNotification.query.filter_by(
        user_id=_user("ben").id, node_id=later).count() == 0
    # anna sees no difference: ben is still listed, nothing names the mute.
    mine = _client(app, "anna").get(f"/api/nodes/{nid}").get_json()
    assert mine["audience"]["readers"] == ["ben", "chidi"]
    assert "mute" not in json.dumps(mine) and "hidden" not in json.dumps(mine)
    assert _can_read(app, "chidi", nid)
    # The list at the bottom of Shared with you, and the undo.
    data = ben.get("/api/circles/shared-with-me").get_json()
    assert data["hidden"] == ["anna"] and data["people"] == []
    assert ben.delete("/api/circles/mutes/anna").status_code == 200
    assert _can_read(app, "ben", nid) and _can_read(app, "ben", later)
    assert CircleMute.query.count() == 0
    # Hiding is no way to test a name.
    assert ben.post("/api/circles/mutes",
                    json={"username": "dana"}).status_code == 404
    assert ben.post("/api/circles/mutes",
                    json={"username": "nobody"}).status_code == 404


def test_shared_with_you_is_grouped_by_person_newest_first(app):
    a1 = _share(app, names=["ben"], content="# Leaving in March\nI told them.")
    c1 = _share(app, owner="chidi", names=["ben"], content="Chapter four")
    a2 = _share(app, names=["ben"], content="What I want")
    for i, nid in enumerate((a1, c1, a2)):
        _db.session.get(Node, nid).created_at = (
            datetime(2026, 11, 1) + timedelta(days=i))
    _db.session.commit()
    _reply(app, "ben", a1, "ben replied here")
    data = _client(app, "ben").get("/api/circles/shared-with-me").get_json()
    assert [p["username"] for p in data["people"]] == ["anna", "chidi"]
    annas = data["people"][0]["entries"]
    assert [e["id"] for e in annas] == [a2, a1]
    assert annas[1]["title"] == "Leaving in March"
    assert annas[1]["preview"] == "I told them."
    assert annas[1]["last_reply_by"] == "ben" and annas[1]["last_reply_by_me"]
    # Nothing that counts or tracks reading.
    blob = json.dumps(data)
    for word in ("unread", "read_at", "count", "seen"):
        assert word not in blob
    # Nor does it enter ben's Log.
    log = _client(app, "ben").get("/api/log").get_json()["nodes"]
    assert all(n["id"] not in (a1, a2, c1) for n in log)


# ── What a model may read ───────────────────────────────────────────────

def _chain(parent, user_id, **kw):
    saved = sys.modules.get("backend.tasks.llm_completion")
    try:
        from backend.tests.test_context_artifact_pinning import _load_node_chain
    finally:
        if saved is not None:
            sys.modules["backend.tasks.llm_completion"] = saved
    return _load_node_chain(parent, user_id, **kw)



def test_no_model_reads_a_shared_entry_outside_its_thread(app):
    from backend.utils.llm_nodes import reply_refusal, voice_turn_refusal
    from backend.utils.privacy import (
        can_ai_access_node, can_user_access_node)
    from backend.utils.quotes import get_quote_data, resolve_quotes
    nid = _share(app, names=["ben"])
    rid = _reply(app, "ben", nid, "ben replies").get_json()["id"]
    ben = _user("ben")
    entry, reply = _db.session.get(Node, nid), _db.session.get(Node, rid)
    assert can_user_access_node(entry, ben.id)
    assert not can_ai_access_node(entry, ben.id)
    # An AI reply in the thread itself may be asked for; a Voice session
    # in someone else's thread may not.
    assert reply_refusal(reply, ben.id, "chat") is None
    assert voice_turn_refusal(ben, reply) is not None
    # Outside a reply written in the thread, the chain of a reply for
    # ben stops below anna's entry.
    assert [n.id for n in _chain(reply, ben.id)] == [rid]
    with pytest.raises(ValueError):
        _chain(entry, ben.id)
    assert [n.id for n in _chain(reply, ben.id, in_shared_thread=True)] == [
        nid, rid]
    # A note of ben's own under his reply is not a node of the thread:
    # no AI reply there (it would read the thread from outside it).
    note = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben's note", "privacy_level": "private",
        "ai_usage": "chat", "parent_id": rid})
    # (a private reply under a shared node is refused by the reply rule)
    assert note.status_code == 400
    # {quote:ID} and read_full from ben's own private notes stay closed,
    # while the thread page may show him the quote.
    text, resolved = resolve_quotes("{quote:%d}" % nid, ben.id, for_llm=True)
    assert SECRET not in text and resolved == []
    assert get_quote_data([nid], ben.id, as_ai=True)[nid] is None
    assert SECRET in get_quote_data([nid], ben.id)[nid]["content"]



def test_a_member_asks_for_an_ai_reply_in_the_thread(app):
    nid = _share(app, names=["ben", "chidi"])
    hidden = _reply(app, "chidi", nid, "chidi none", ai_usage="none"
                    ).get_json()["id"]
    tip = _reply(app, "ben", hidden, "ben replies").get_json()["id"]
    ben = _user("ben")
    ben.preferred_model = "claude-x"
    _db.session.commit()
    r = _client(app, "ben").post(f"/api/nodes/{tip}/llm", json={})
    assert r.status_code == 202, r.get_json()
    answer = _db.session.get(Node, r.get_json()["node_id"])
    # The requester's own model and cost; never 'train'; a reply in the
    # thread, read by its audience.
    assert answer.llm_model == "claude-x"
    assert answer.human_owner_id == ben.id
    assert answer.ai_usage == "chat"
    assert answer.privacy_level == "circles"
    assert answer.audience_root_id == nid
    for who in ("anna", "ben", "chidi"):
        assert _can_read(app, who, answer.id), who
    assert not _can_read(app, "dana", answer.id)
    # Its name on the thread page: the model and who asked.
    data = _client(app, "anna").get(f"/api/nodes/{answer.id}").get_json()
    assert data["llm_label"] == "claude-x via @ben"
    assert data["human_owner_username"] == "ben"
    # A model the requester names in the request is theirs to choose.
    r = _client(app, "ben").post(f"/api/nodes/{tip}/llm",
                                 json={"model": "gpt-5"})
    assert r.status_code == 202
    assert _db.session.get(Node, r.get_json()["node_id"]).llm_model == "gpt-5"
    # What the model gets: the thread ben reads, without the node set to
    # None.
    tip_node = _db.session.get(Node, tip)
    assert [n.id for n in _chain(
        tip_node, ben.id, in_shared_thread=True)] == [nid, tip]
    # Under any node of the thread ben can read: the entry, and chidi's
    # reply (another person's node).
    other = _reply(app, "chidi", nid, "chidi chat").get_json()["id"]
    for node_id in (nid, other):
        r = _client(app, "ben").post(f"/api/nodes/{node_id}/llm", json={})
        assert r.status_code == 202, node_id
        made = _db.session.get(Node, r.get_json()["node_id"])
        assert made.human_owner_id == ben.id
        assert made.audience_root_id == nid
        # anna's entry does not set ben's model.
        assert made.llm_model == "claude-x"
    # The node replied to is None: refused, as in a private thread.
    r = _client(app, "ben").post(f"/api/nodes/{hidden}/llm", json={})
    assert r.status_code == 403
    # Not for someone who cannot read the thread: 403 and no text, the
    # answer for any node that is not theirs.
    before = Node.query.count()
    for node_id in (nid, tip, answer.id):
        r = _client(app, "dana").post(f"/api/nodes/{node_id}/llm", json={})
        assert r.status_code == 403
        assert SECRET not in r.get_data(as_text=True)
        assert "ben replies" not in r.get_data(as_text=True)
    own = _client(app, "anna").post("/api/nodes/", json={
        "content": "anna private", "ai_usage": "chat"}).get_json()["id"]
    assert _client(app, "ben").post(
        f"/api/nodes/{own}/llm", json={}).status_code == 403
    assert Node.query.count() == before + 1          # anna's private node
    # A removed member is refused at once, also under their own reply.
    NodeReader.query.filter_by(node_id=nid, user_id=ben.id).delete()
    _db.session.commit()
    assert _client(app, "ben").post(
        f"/api/nodes/{tip}/llm", json={}).status_code == 403
    assert _client(app, "ben").post(
        f"/api/nodes/{nid}/llm", json={}).status_code == 403


def test_an_ai_reply_can_be_kept_private_before_or_after(app):
    """Its requester may keep an AI reply to themselves, when asking for
    it or later; never public, never another audience. Private, nobody
    else reads it: not the other readers, not the entry's author."""
    nid = _share(app, names=["ben", "chidi"])
    # Before: asked for as private.
    r = _client(app, "ben").post(f"/api/nodes/{nid}/llm",
                                 json={"privacy_level": "private"})
    assert r.status_code == 202
    mine = r.get_json()["node_id"]
    node = _db.session.get(Node, mine)
    assert node.privacy_level == "private" and node.audience_root_id == nid
    assert _can_read(app, "ben", mine)
    for who in ("anna", "chidi", "dana"):
        assert not _can_read(app, who, mine), who
    # No other level can be asked for.
    r = _client(app, "ben").post(f"/api/nodes/{nid}/llm",
                                 json={"privacy_level": "public"})
    shared = r.get_json()["node_id"]
    assert _db.session.get(Node, shared).privacy_level == "circles"
    assert _can_read(app, "anna", shared) and _can_read(app, "chidi", shared)
    # After: taken back, and put back in the thread.
    assert _put(app, "ben", shared, privacy_level="private").status_code == 200
    for who in ("anna", "chidi"):
        assert not _can_read(app, who, shared), who
    assert _can_read(app, "ben", shared)
    assert _put(app, "ben", shared, privacy_level="public").status_code == 403
    assert _put(app, "ben", shared, privacy_level="circles",
                circle_usernames=["dana"]).status_code == 400
    assert _put(app, "ben", mine, privacy_level="circles").status_code == 200
    assert _can_read(app, "anna", mine) and _can_read(app, "chidi", mine)
    # The author's own AI reply: shared by default, private when she
    # says so, before or after.
    r = _client(app, "anna").post(f"/api/nodes/{nid}/llm", json={})
    hers = r.get_json()["node_id"]
    assert _db.session.get(Node, hers).audience_root_id == nid
    assert _can_read(app, "ben", hers)
    data = _client(app, "ben").get(f"/api/nodes/{hers}").get_json()
    assert data["llm_label"] == "gpt-5 via @anna"
    assert _put(app, "anna", hers, privacy_level="private").status_code == 200
    assert not _can_read(app, "ben", hers)
    r = _client(app, "anna").post(f"/api/nodes/{nid}/llm",
                                  json={"privacy_level": "private"})
    assert not _can_read(app, "ben", r.get_json()["node_id"])
    assert _can_read(app, "anna", r.get_json()["node_id"])



def test_no_ai_reply_when_the_entry_is_none(app):
    """For anyone: the readers and the author, as in a private thread."""
    nid = _share(app, names=["ben"], ai_usage="none")
    rid = _reply(app, "ben", nid, "ben replies").get_json()["id"]
    for who, node_id in (("ben", nid), ("ben", rid), ("anna", nid),
                         ("anna", rid)):
        r = _client(app, who).post(f"/api/nodes/{node_id}/llm", json={})
        assert r.status_code == 403, (who, node_id)
    r = _client(app, "ben").post(f"/api/nodes/{rid}/llm", json={})
    assert "None" in r.get_json()["error"]
    assert Node.query.filter_by(node_type="llm").count() == 0
    data = _client(app, "ben").get(f"/api/nodes/{nid}").get_json()
    assert data["audience"]["entry_ai_usage"] == "none"



def test_an_ai_reply_respects_the_members_spend_cap_and_account_setting(
        app, monkeypatch):
    nid = _share(app, names=["ben", "chidi"])
    import backend.utils.spend as spend
    monkeypatch.setattr(spend, "user_is_capped", lambda u: True)
    r = _client(app, "ben").post(f"/api/nodes/{nid}/llm", json={})
    assert r.status_code == 402
    monkeypatch.undo()
    _user("chidi").default_ai_usage = "none"
    _db.session.commit()
    r = _client(app, "chidi").post(f"/api/nodes/{nid}/llm", json={})
    assert r.status_code == 403
    assert Node.query.filter_by(node_type="llm").count() == 0



def test_circle_text_never_goes_out_on_the_training_key(app):
    saved = sys.modules.get("backend.tasks.llm_completion")
    try:
        from backend.tests.test_context_artifact_pinning import (
            _load_node_chain)
    finally:
        if saved is not None:
            sys.modules["backend.tasks.llm_completion"] = saved
    carries = _load_node_chain.__globals__["_carries_circle_text"]
    nid = _share(app, names=["ben"], ai_usage="train")
    rid = _reply(app, "ben", nid, "ben replies", ai_usage="train"
                 ).get_json()["id"]
    own = _client(app, "anna").post("/api/nodes/", json={
        "content": "anna private", "ai_usage": "train"}).get_json()["id"]
    chain = [_db.session.get(Node, nid), _db.session.get(Node, rid)]
    # Whoever asks, the author on her own entry included, whatever the
    # entry's AI usage: a prompt with circle text uses the chat key.
    assert carries(chain) is True
    assert carries(chain[:1]) is True
    assert carries([_db.session.get(Node, own)]) is False



def test_the_authors_model_reads_replies_by_their_own_ai_usage(app):
    from backend.utils.llm_nodes import reply_refusal
    from backend.utils.privacy import can_ai_access_node
    nid = _share(app, names=["ben"])
    ok = _reply(app, "ben", nid, "ben chat").get_json()["id"]
    none = _reply(app, "ben", nid, "ben none", ai_usage="none"
                  ).get_json()["id"]
    anna = _user("anna")
    assert can_ai_access_node(_db.session.get(Node, ok), anna.id)
    assert [n.id for n in _chain(_db.session.get(Node, ok), anna.id)] == [
        nid, ok]
    # The public-thread rule: a reply marked None refuses the reply.
    assert reply_refusal(_db.session.get(Node, none), anna.id) is not None
    assert reply_refusal(_db.session.get(Node, ok), anna.id) is None
    # A reply on her own shared entry is a reply in the thread: its
    # readers read it (Peter, 2026-11-11), also under a reader's reply.
    for node_id in (nid, ok):
        r = _client(app, "anna").post(f"/api/nodes/{node_id}/llm", json={})
        assert r.status_code == 202
        answer = _db.session.get(Node, r.get_json()["node_id"])
        assert answer.audience_root_id == nid
        assert answer.human_owner_id == anna.id
        assert _can_read(app, "ben", answer.id)
        assert not _can_read(app, "dana", answer.id)


def test_another_persons_model_never_sets_my_default(app):
    from backend.utils.llm_nodes import resolve_chat_model
    nid = _share(app, names=["ben"])
    ben, anna = _user("ben"), _user("anna")
    ben.preferred_model = "claude-x"
    _db.session.commit()
    r = _client(app, "ben").post(f"/api/nodes/{nid}/llm",
                                 json={})
    answer = _db.session.get(Node, r.get_json()["node_id"])
    answer.llm_task_status = "completed"
    _db.session.commit()
    assert answer.llm_model == "claude-x"
    # anna's next reply under ben's AI answer stays on her own default.
    assert resolve_chat_model(answer, anna) == ("gpt-5", "default")


def _export(user, **kw):
    from backend.routes.export_data import build_user_export_content
    r = build_user_export_content(user, return_metadata=True, **kw)
    return (r or {}).get("content") or ""


def test_a_shared_entry_never_enters_the_readers_export(app):
    """The export feeds the data download, profile builds and recent
    context: for a reader it holds their own reply, never the entry."""
    from backend.routes.export_data import _export_visible_filter
    nid = _share(app, names=["ben"])
    rid = _reply(app, "ben", nid, "BENS-OWN-REPLY").get_json()["id"]
    _client(app, "ben").post("/api/nodes/", json={
        "content": "BENS-PRIVATE-NOTE {quote:%d}" % nid, "ai_usage": "chat"})
    ben, anna = _user("ben"), _user("anna")
    for kw in ({"filter_ai_usage": False},
               {"filter_ai_usage": True, "max_tokens": 10000},
               {"filter_ai_usage": True,
                "created_after": datetime(2020, 1, 1)}):
        out = _export(ben, **kw)
        assert SECRET not in out, kw
        assert "BENS-PRIVATE-NOTE" in out, kw
    visible = {i for (i,) in _db.session.query(Node.id).filter(
        _export_visible_filter(Node, ben.id))}
    assert nid not in visible and rid in visible
    # The author's export holds her entry.
    assert SECRET in _export(anna, filter_ai_usage=False)



def test_a_members_ai_reply_is_theirs(app):
    """ben's AI reply under anna's entry is ben's: in his profile builds
    and recent context (the export for a model), his search index and
    his data download. anna's entry and the other people's replies are
    in none of them (Peter, 2026-11-11)."""
    from backend.routes.export_data import (
        _export_visible_filter, iter_user_export)
    from backend.utils.privacy import (
        can_ai_access_node, can_user_access_node, is_others_thread_answer)
    nid = _share(app, names=["ben", "chidi"])
    _reply(app, "chidi", nid, "CHIDIS-REPLY")
    typed = _reply(app, "ben", nid, "BENS-TYPED-REPLY").get_json()["id"]
    r = _client(app, "ben").post(f"/api/nodes/{nid}/llm", json={})
    answer = _db.session.get(Node, r.get_json()["node_id"])
    answer.set_content("BENS-AI-REPLY")
    answer.llm_task_status = "completed"
    theirs = _client(app, "chidi").post(f"/api/nodes/{nid}/llm", json={})
    other = _db.session.get(Node, theirs.get_json()["node_id"])
    other.set_content("CHIDIS-AI-REPLY")
    other.llm_task_status = "completed"
    _db.session.commit()
    ben, anna = _user("ben"), _user("anna")
    assert is_others_thread_answer(answer)
    assert can_user_access_node(answer, ben.id)
    assert can_ai_access_node(answer, ben.id)
    assert not can_ai_access_node(other, ben.id)
    visible = {i for (i,) in _db.session.query(Node.id).filter(
        _export_visible_filter(Node, ben.id))}
    assert {answer.id, typed} <= visible
    assert nid not in visible and other.id not in visible
    # Profile builds and recent context read this export.
    model_export = dict(filter_ai_usage=True, max_tokens=10000,
                        include_strategy="engaged_threads")
    out = _export(ben, **model_export)
    assert "BENS-AI-REPLY" in out and "BENS-TYPED-REPLY" in out
    for words in (SECRET, "CHIDIS-REPLY", "CHIDIS-AI-REPLY"):
        assert words not in out, words
    # The data download: both in the section of his replies in other
    # people's threads, and nobody else's text.
    download = "".join(iter_user_export(ben))
    assert "Your replies in threads shared with you" in download
    assert "BENS-AI-REPLY" in download and "BENS-TYPED-REPLY" in download
    assert "in a thread shared by @anna" in download
    for words in (SECRET, "CHIDIS-REPLY", "CHIDIS-AI-REPLY"):
        assert words not in download, words
    # anna's own AI reply on her own entry is hers like any reply.
    own = _client(app, "anna").post(f"/api/nodes/{nid}/llm", json={})
    hers = _db.session.get(Node, own.get_json()["node_id"])
    assert not is_others_thread_answer(hers)
    assert can_ai_access_node(hers, anna.id)
    # Removed, ben keeps his typed reply; his AI reply closes for him
    # and leaves his download, his export and his Log until he is back.
    NodeReader.query.filter_by(node_id=nid, user_id=ben.id).delete()
    _db.session.commit()
    download = "".join(iter_user_export(ben))
    assert "BENS-TYPED-REPLY" in download and "BENS-AI-REPLY" not in download
    assert "@anna" not in download
    out = _export(ben, **model_export)
    assert "BENS-AI-REPLY" not in out and "BENS-TYPED-REPLY" in out
    assert not _can_read(app, "ben", answer.id)
    _db.session.add(NodeReader(node_id=nid, user_id=ben.id))
    _db.session.commit()
    assert _can_read(app, "ben", answer.id)
    assert "BENS-AI-REPLY" in "".join(iter_user_export(ben))


def test_search_scope_is_the_users_own(app):
    """Semantic search and embeddings are keyed by the owner: a shared
    entry has no row for a reader."""
    from backend.models import NodeEmbedding
    cols = {c.name for c in NodeEmbedding.__table__.columns}
    assert "user_id" in cols


# ── The flag ────────────────────────────────────────────────────────────

def test_with_the_flag_off_circles_is_private(app):
    nid = _share(app, names=["ben"])
    assert _can_read(app, "ben", nid)
    app.config["CIRCLES_USER_IDS"] = set()
    assert not _can_read(app, "ben", nid)
    assert _can_read(app, "anna", nid)
    c = _client(app, "anna")
    assert c.get("/api/circles").status_code == 404
    assert "audience" not in c.get(f"/api/nodes/{nid}").get_json()
    # As before the feature: "circles" is accepted and reads as private.
    r = c.post("/api/nodes/", json={
        "content": "x", "privacy_level": "circles", "ai_usage": "chat",
        "circle_usernames": ["ben"]})
    assert r.status_code == 201
    assert NodeReader.query.filter_by(node_id=r.get_json()["id"]).count() == 0
    app.config["CIRCLES_V1"] = True
    assert _can_read(app, "ben", nid)
    assert not _can_read(app, "ben", r.get_json()["id"])


def test_dashboard_carries_the_flag(app):
    from backend.utils.circles import circles_user_fields
    assert circles_user_fields(_user("anna")) == {"circles_enabled": True}
    assert circles_user_fields(_user("eve")) == {"circles_enabled": False}


# ── #588 ────────────────────────────────────────────────────────────────

def test_textmode_uses_the_shared_privacy_levels(app):
    import backend.routes.textmode as textmode
    assert not hasattr(textmode, "VALID_PRIVACY_LEVELS")
    c = _client(app, "anna")
    r = c.post("/api/textmode/start", json={
        "content": "hello", "privacy_level": "anonymous", "ai_usage": "chat"})
    assert r.status_code == 400
    assert "Invalid privacy_level" in r.get_json()["error"]


# ── Membership rows ─────────────────────────────────────────────────────

def test_tables_have_indexes_on_their_foreign_keys(app):
    for model in (Circle, CircleMember, NodeCircle, NodeReader, CircleMute):
        for col in model.__table__.columns:
            if col.foreign_keys:
                assert col.index, f"{model.__tablename__}.{col.name}"
    assert Node.__table__.c.audience_root_id.index
    assert UserNotification.__table__.c.node_id.index
    assert UserNotification.__table__.c.actor_user_id.index
    assert CircleMember.query.count() == 0


# ── The second step: PUT /nodes/<id>/audience ───────────────────────────

def test_an_entry_created_without_an_audience_is_shared_in_a_second_step(app):
    c = _client(app, "anna")
    nid = c.post("/api/nodes/", json={
        "content": "recorded entry", "privacy_level": "circles",
        "ai_usage": "chat"}).get_json()["id"]
    child = c.post("/api/nodes/", json={
        "content": "its reply", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid}).get_json()["id"]
    assert not _can_read(app, "ben", nid)
    r = c.put(f"/api/nodes/{nid}/audience", json={"circle_usernames": []})
    assert r.status_code == 400
    assert not _can_read(app, "ben", nid)
    r = c.put(f"/api/nodes/{nid}/audience", json={
        "circle_usernames": ["ben"], "apply_to_descendants": True})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["audience"]["readers"] == ["ben"]
    assert _can_read(app, "ben", nid) and _can_read(app, "ben", child)
    assert len(_notes(app, "ben")) == 1
    # Only the entry's own writer; a reader gets "not found".
    assert _client(app, "ben").put(f"/api/nodes/{nid}/audience", json={
        "circle_usernames": ["dana"]}).status_code == 404
    assert not _can_read(app, "dana", nid)



def test_a_long_entrys_parts_and_its_first_ai_reply_are_shared_with_it(app):
    """The second step that gives a new entry its audience: its parts
    are the entry, and the AI reply made along with it (created with the
    entry's level before the audience existed) is a reply in the thread.
    A reply that is private stays private."""
    from datetime import timedelta as _td
    from backend.utils.node_split import PART_STEP
    anna = _user("anna")
    first = Node(user_id=anna.id, human_owner_id=anna.id,
                 privacy_level="circles", ai_usage="chat")
    first.set_content("part one")
    _db.session.add(first)
    _db.session.commit()
    part = Node(user_id=anna.id, human_owner_id=anna.id, parent_id=first.id,
                privacy_level="circles", ai_usage="chat",
                created_at=first.created_at + PART_STEP)
    part.set_content("part two")
    llm = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add_all([part, llm])
    _db.session.commit()
    reply = Node(user_id=llm.id, human_owner_id=anna.id, parent_id=part.id,
                 node_type="llm", llm_model="gpt-5",
                 privacy_level="circles", ai_usage="chat",
                 created_at=part.created_at + _td(seconds=5))
    reply.set_content("the AI reply made with the entry")
    kept = Node(user_id=llm.id, human_owner_id=anna.id, parent_id=part.id,
                node_type="llm", llm_model="gpt-5",
                privacy_level="private", ai_usage="chat",
                created_at=part.created_at + _td(seconds=6))
    kept.set_content("an AI reply she keeps private")
    _db.session.add_all([reply, kept])
    _db.session.commit()
    r = _client(app, "anna").put(f"/api/nodes/{first.id}/audience", json={
        "circle_usernames": ["ben"]})
    assert r.status_code == 200
    assert _can_read(app, "ben", first.id) and _can_read(app, "ben", part.id)
    assert _can_read(app, "ben", reply.id)
    assert not _can_read(app, "ben", kept.id)
    assert _can_read(app, "anna", reply.id) and _can_read(app, "anna", kept.id)



def _prompt_thread(app, text="THE-PROMPT {user_profile} {user_memory}"):
    """anna's private agentic thread: a prompt node, her first turn, her
    AI's reply, a second turn E (the one she will share), and a side
    branch under the first turn. Returns their ids as a dict."""
    anna = _user("anna")
    llm = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add(llm)
    _db.session.commit()

    def add(parent, content, **kw):
        n = Node(user_id=kw.pop("user_id", anna.id), human_owner_id=anna.id,
                 parent_id=parent, privacy_level="private", ai_usage="chat",
                 node_type=kw.pop("node_type", "user"), **kw)
        n.set_content(content)
        _db.session.add(n)
        _db.session.commit()
        return n.id

    root = add(None, text, prompt_key="textmode")
    first = add(root, "ANNAS-FIRST-TURN")
    ai = add(first, "ANNAS-AI-REPLY", user_id=llm.id, node_type="llm",
             llm_model="gpt-5")
    entry = add(ai, SECRET)
    side = add(first, "ANNAS-SIDE-BRANCH")
    return dict(root=root, first=first, ai=ai, entry=entry, side=side)


def test_the_discussion_leading_up_to_an_entry_is_shared_only_when_asked(app):
    """Peter, 2026-11-11: "Share also the discussion leading up to this
    node?" Yes shares the whole path down to the node, the prompt
    included; no shares only the node. A prompt is never shared alone,
    and a reader (or their model) gets a prompt only when it is shared
    with them."""
    t = _prompt_thread(app)
    root, entry = t["root"], t["entry"]
    path = [t["root"], t["first"], t["ai"]]
    ben = _user("ben")
    node = _db.session.get(Node, entry)

    def chain():
        return [n.id for n in _chain(node, ben.id, in_shared_thread=True)]

    # No: only the node. Nothing above it for ben or his model.
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"]})
    assert r.status_code == 200
    assert _can_read(app, "ben", entry)
    for node_id in path + [t["side"]]:
        assert not _can_read(app, "ben", node_id), node_id
        assert _db.session.get(Node, node_id).privacy_level == "private"
    data = _client(app, "ben").get(f"/api/nodes/{entry}").get_json()
    for words in ("THE-PROMPT", "ANNAS-FIRST-TURN", "ANNAS-AI-REPLY"):
        assert words not in json.dumps(data), words
    assert data["audience"]["ai_reads"] == []
    assert chain() == [entry]
    # The author is asked; a node with nothing above it has no question.
    info = _client(app, "anna").get(f"/api/nodes/{entry}/audience").get_json()
    assert info["thread_path"] == {"count": 3, "shared": False}
    assert _client(app, "ben").get(
        f"/api/nodes/{entry}/audience").get_json()["thread_path"] is None
    alone = _share(app, names=["ben"], content="no ancestors")
    assert _client(app, "anna").get(
        f"/api/nodes/{alone}/audience").get_json()["thread_path"] is None
    # A prompt is never shared alone.
    r = _client(app, "anna").put(f"/api/nodes/{root}/audience", json={
        "circle_usernames": ["ben"]})
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_prompt_node"
    assert not _can_read(app, "ben", root)
    # Yes: exactly the path, with the same audience; not the side branch.
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    for node_id in path:
        assert _can_read(app, "ben", node_id), node_id
        assert not _can_read(app, "chidi", node_id), node_id
    assert not _can_read(app, "ben", t["side"])
    assert _client(app, "anna").get(
        f"/api/nodes/{entry}/audience").get_json()["thread_path"] == {
        "count": 3, "shared": True}
    assert chain() == path + [entry]
    # The path is not a set of entries: one note, one listed entry.
    assert _shared_ids(app, "ben") == [alone, entry] or _shared_ids(
        app, "ben") == [entry, alone]
    data = _client(app, "ben").get(f"/api/nodes/{entry}").get_json()
    assert data["audience"]["ai_reads"][:2] == ["profile", "memory"]
    # The request for a reply the thread will read carries the key of
    # what the dialog named; without it, or with another, nothing is made.
    key = data["audience"]["ai_reads_key"]
    assert key and data["audience"]["ai_reads"][-1].startswith("anything else")
    before = Node.query.count()
    for body in ({}, {"ai_reads_ack": "something-else"}):
        r = _client(app, "ben").post(f"/api/nodes/{entry}/llm", json=body)
        assert r.status_code == 409, body
        assert r.get_json()["code"] == "circle_ai_reads_changed"
        assert r.get_json()["ai_reads_key"] == key
        assert r.get_json()["ai_reads"] == data["audience"]["ai_reads"]
    assert Node.query.count() == before
    r = _client(app, "ben").post(f"/api/nodes/{entry}/llm",
                                 json={"ai_reads_ack": key})
    assert r.status_code == 202
    # A reply ben keeps to himself needs no dialog.
    r = _client(app, "ben").post(f"/api/nodes/{entry}/llm",
                                 json={"privacy_level": "private"})
    assert r.status_code == 202
    # anna's own prompt asks nothing of her to agree to.
    hers = _client(app, "anna").get(f"/api/nodes/{entry}").get_json()
    assert hers["audience"]["ai_reads_key"] == ""
    assert hers["audience"]["ai_reads"][:2] == ["profile", "memory"]
    assert _client(app, "anna").post(
        f"/api/nodes/{entry}/llm", json={}).status_code == 202
    # anna's AI reply on the path is named as hers.
    shown = _client(app, "ben").get(f"/api/nodes/{t['ai']}").get_json()
    assert shown["llm_label"] == "gpt-5 via @anna"
    # A gap made later: the chain stops below it, so the prompt above is
    # not used, though ben can still open it on its own.
    r = _put(app, "anna", t["first"], privacy_level="private")
    assert r.status_code == 200
    assert not _can_read(app, "ben", t["first"])
    assert _can_read(app, "ben", root)
    assert chain() == [t["ai"], entry]
    assert _client(app, "ben").get(
        f"/api/nodes/{entry}").get_json()["audience"]["ai_reads"] == []
    # Taken back: the whole path closes.
    r = _client(app, "anna").put(f"/api/nodes/{entry}/audience", json={
        "circle_usernames": ["ben"], "share_path": False})
    assert r.status_code == 200
    for node_id in path:
        assert not _can_read(app, "ben", node_id), node_id
    assert _can_read(app, "ben", entry)
    assert chain() == [entry]


def test_a_shared_prompt_is_shown_with_the_viewers_own_artifacts(app):
    """Peter, 2026-11-11: "it should embed Ben's artifacts where
    possible (Anna may have artifacts that Ben doesn't, in which case
    just the placeholder should be displayed)". anna's artifacts and the
    versions she pinned never reach ben, on any route."""
    from backend.models import NodeContextArtifact, UserArtifact, UserProfile
    t = _prompt_thread(app)
    anna, ben = _user("anna"), _user("ben")
    hers = UserProfile(user_id=anna.id, generated_by="test", ai_usage="chat")
    hers.set_content("ANNAS-PROFILE")
    her_memory = UserArtifact(user_id=anna.id, kind="memory", title="Memory", generated_by="test",
                              ai_usage="chat")
    her_memory.set_content("ANNAS-MEMORY")
    his = UserProfile(user_id=ben.id, generated_by="test", ai_usage="chat")
    his.set_content("BENS-PROFILE")
    _db.session.add_all([hers, her_memory, his])
    _db.session.commit()
    _db.session.add_all([
        NodeContextArtifact(node_id=t["root"], artifact_type="profile",
                            artifact_id=hers.id),
        NodeContextArtifact(node_id=t["root"], artifact_type="user_artifact",
                            artifact_id=her_memory.id)])
    _db.session.commit()
    r = _client(app, "anna").put(f"/api/nodes/{t['entry']}/audience", json={
        "circle_usernames": ["ben"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    # The owner sees her pinned versions, as before.
    own = _client(app, "anna").get(f"/api/nodes/{t['root']}").get_json()
    assert own["context_artifacts"]["profile"]["content"] == "ANNAS-PROFILE"
    assert own["context_artifacts"]["memory"]["content"] == "ANNAS-MEMORY"
    # ben: the stored text, his own profile, and no memory (he has
    # none), so the page shows that placeholder itself.
    shown = _client(app, "ben").get(f"/api/nodes/{t['root']}").get_json()
    assert "{user_profile}" in shown["content"]
    assert "{user_memory}" in shown["content"]
    assert shown["context_artifacts"]["profile"]["content"] == "BENS-PROFILE"
    assert "memory" not in shown["context_artifacts"]
    # Nothing of anna's on any page ben can open in the thread.
    ben_client = _client(app, "ben")
    for node_id in (t["root"], t["first"], t["ai"], t["entry"]):
        for url in (f"/api/nodes/{node_id}",
                    f"/api/nodes/{node_id}/audience"):
            body = ben_client.get(url).get_data(as_text=True)
            assert "ANNAS-PROFILE" not in body, url
            assert "ANNAS-MEMORY" not in body, url
    # chidi is not in the audience: nothing at all.
    assert not _can_read(app, "chidi", t["root"])


# ── Where a reply came from decides who may widen it ────────────────────

def _put(app, who, node_id, **body):
    node = _db.session.get(Node, node_id)
    body.setdefault("content", node.get_content())
    return _client(app, who).put(f"/api/nodes/{node_id}", json=body)


def _thread_with_answer(app):
    """anna's entry for ben and chidi; ben's reply R, the AI reply A he
    asked for under R, chidi's reply N under R."""
    entry = _share(app, names=["ben", "chidi"])
    r = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    a = _client(app, "ben").post(
        f"/api/nodes/{r}/llm", json={}).get_json()["node_id"]
    answer = _db.session.get(Node, a)
    answer.set_content("ANSWER-FROM-ANNAS-ENTRY")
    answer.llm_task_status = "completed"
    _db.session.commit()
    n = _reply(app, "chidi", r, "chidi under ben").get_json()["id"]
    return entry, r, a, n


def test_an_answer_and_a_nested_reply_are_never_widened(app):
    entry, r, a, n = _thread_with_answer(app)
    assert _db.session.get(Node, a).audience_root_id == entry
    anon = app.test_client()

    def closed(node_id):
        node = _db.session.get(Node, node_id)
        _db.session.refresh(node)
        assert node.privacy_level != "public", node_id
        assert node.audience_root_id == entry, node_id
        assert anon.get(f"/api/commons/node/{node_id}").status_code == 404
        assert not _can_read(app, "dana", node_id)

    # Directly: not public, not another audience (edit and second step).
    for who, node_id in (("ben", a), ("ben", r), ("chidi", n)):
        assert _put(app, who, node_id,
                    privacy_level="public").status_code == 403
        assert _put(app, who, node_id, privacy_level="circles",
                    circle_usernames=["dana"]).status_code == 400
        assert _client(app, who).put(
            f"/api/nodes/{node_id}/audience",
            json={"circle_usernames": ["dana"]}).status_code == 400
        closed(node_id)

    # ben takes his reply back, with his replies below it. The answer and
    # chidi's nested reply now sit under a private parent: still not
    # theirs to widen.
    assert _put(app, "ben", r, privacy_level="private",
                apply_to_descendants=True).status_code == 200
    assert _db.session.get(Node, a).privacy_level == "private"
    for who, node_id in (("ben", a), ("ben", r), ("chidi", n)):
        assert _put(app, who, node_id,
                    privacy_level="public").status_code == 403, node_id
        assert _put(app, who, node_id, privacy_level="circles",
                    circle_usernames=["dana"]).status_code == 400, node_id
        closed(node_id)
    # Nor by a cascade from ben's reply.
    assert _put(app, "ben", r, privacy_level="public",
                apply_to_descendants=True).status_code == 403
    closed(a)
    # A note ben writes under his private reply comes from the same thread.
    note = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben's later note", "privacy_level": "private",
        "ai_usage": "chat", "parent_id": r}).get_json()["id"]
    assert _put(app, "ben", note, privacy_level="public").status_code == 403
    assert _put(app, "ben", note, privacy_level="circles",
                circle_usernames=["dana"]).status_code == 400
    # Also with Circles switched off for ben.
    app.config["CIRCLES_USER_IDS"] = app.config["CIRCLES_USER_IDS"] - {
        _user("ben").id}
    assert _put(app, "ben", a, privacy_level="public").status_code == 403
    closed(a)



def test_an_answer_taken_back_is_its_owners_alone(app):
    from backend.utils.privacy import can_ai_access_node
    entry, r, a, n = _thread_with_answer(app)
    assert _put(app, "ben", a, privacy_level="private").status_code == 200
    ben = _user("ben")
    answer = _db.session.get(Node, a)
    assert answer.privacy_level == "private" and answer.audience_root_id == entry
    for who in ("anna", "chidi", "dana"):
        assert not _can_read(app, who, a), who
    # Still ben's: on screen, for his models, in his export.
    assert _can_read(app, "ben", a) and can_ai_access_node(answer, ben.id)
    assert a in _corpus_ids(ben) and r in _corpus_ids(ben)
    # Back in the thread, it is read by the thread's audience again.
    assert _put(app, "ben", a, privacy_level="circles").status_code == 200
    assert _can_read(app, "chidi", a) and _can_read(app, "anna", a)


def test_an_answer_is_closed_to_a_removed_or_muted_reader(app):
    entry, r, a, n = _thread_with_answer(app)
    assert _can_read(app, "chidi", a)
    assert _client(app, "chidi").post(
        "/api/circles/mutes", json={"username": "anna"}).status_code == 200
    assert not _can_read(app, "chidi", a) and not _can_read(app, "chidi", r)
    assert _client(app, "chidi").delete(
        "/api/circles/mutes/anna").status_code == 200
    assert _can_read(app, "chidi", a)
    # anna removes chidi (the entry is now for ben only).
    assert _put(app, "anna", entry, privacy_level="circles",
                circle_usernames=["ben"]).status_code == 200
    assert not _can_read(app, "chidi", a) and not _can_read(app, "chidi", r)
    assert _can_read(app, "chidi", n)          # his own reply stays his
    assert _can_read(app, "ben", a) and _can_read(app, "anna", a)


def test_the_author_may_still_make_her_own_thread_public(app):
    entry, r, a, n = _thread_with_answer(app)
    own = _reply(app, "anna", entry, "anna adds").get_json()["id"]
    assert _put(app, "anna", entry, privacy_level="public",
                apply_to_descendants=True).status_code == 200
    assert _db.session.get(Node, own).privacy_level == "public"
    for node_id in (r, a, n):
        assert _db.session.get(Node, node_id).privacy_level == "circles"


# ── A node the caller cannot read answers like one that does not exist ──

def test_a_shared_entry_is_not_told_apart_from_a_missing_one(app):
    entry = _share(app, names=["ben"])
    private = _client(app, "anna").post("/api/nodes/", json={
        "content": "anna private", "ai_usage": "chat"}).get_json()["id"]
    missing = 987654
    dana = _client(app, "dana")

    def same(call):
        answers = set()
        for node_id in (entry, private, missing):
            r = call(node_id)
            answers.add((r.status_code,
                         json.dumps(r.get_json(), sort_keys=True)))
        assert len(answers) == 1, answers
        assert next(iter(answers))[0] == 404

    for privacy in ("private", "circles", "public"):
        same(lambda i: dana.post("/api/nodes/", json={
            "content": "x", "privacy_level": privacy, "ai_usage": "chat",
            "parent_id": i}))
        same(lambda i: dana.post("/api/nodes/streaming/init", json={
            "privacy_level": privacy, "ai_usage": "chat", "parent_id": i}))
        same(lambda i: dana.post("/api/nodes/upload/init", json={
            "filename": "a.mp3", "filesize": 10, "total_chunks": 1,
            "upload_id": "0123456789abcdef0123456789abcdef",
            "privacy_level": privacy, "ai_usage": "chat", "parent_id": i}))
    # The AI reply route answers 403 for every node that is not the
    # caller's, shared or not; a missing one is 404 as before.
    for i in (entry, private):
        r = dana.post(f"/api/nodes/{i}/llm", json={})
        assert r.status_code == 403 and SECRET not in r.get_data(as_text=True)
    assert len({json.dumps(dana.post(f"/api/nodes/{i}/llm", json={}
                                     ).get_json(), sort_keys=True)
                for i in (entry, private)}) == 1
    same(lambda i: dana.get(f"/api/nodes/{i}/audience"))
    same(lambda i: dana.put(f"/api/nodes/{i}/audience",
                            json={"circle_usernames": ["ben"]}))
    same(lambda i: dana.get(f"/api/nodes/{i}"))
    same(lambda i: dana.get(f"/api/nodes/{i}/audio"))


# ── An answer goes with the entry it was written from ───────────────────

def _corpus_ids(user):
    from backend.routes.export_data import _export_visible_filter
    return {i for (i,) in _db.session.query(Node.id).filter(
        _export_visible_filter(Node, user.id))}


def test_an_answer_is_hidden_while_its_entry_is_deleted(app):
    """The author deletes the entry: the reader's AI reply is
    closed at once, to the reader too, and opens again on a restore. The
    reader's own typed reply stays the reader's."""
    from backend.utils.privacy import can_user_access_node
    entry, r, a, n = _thread_with_answer(app)
    ben = _user("ben")
    assert _can_read(app, "ben", a)
    node = _db.session.get(Node, entry)
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    assert not _can_read(app, "ben", a)
    assert not _can_read(app, "chidi", a)
    # The author still reads what was written into her own thread, as
    # she reads the typed replies under her deleted entry.
    assert _can_read(app, "anna", a)
    assert _can_read(app, "ben", r)                 # his own words
    assert r in _corpus_ids(ben) and a not in _corpus_ids(ben)
    assert "ANSWER-FROM-ANNAS-ENTRY" not in _export(ben, filter_ai_usage=False)
    thread = _client(app, "ben").get(f"/api/nodes/{r}").get_json()
    assert "ANSWER-FROM-ANNAS-ENTRY" not in json.dumps(thread)
    # Still not his to widen.
    assert _put(app, "ben", a, privacy_level="public").status_code == 403
    node.deleted_at = None
    _db.session.commit()
    assert can_user_access_node(_db.session.get(Node, a), ben.id)
    assert _can_read(app, "ben", a)


def test_an_answer_whose_entry_is_gone_stays_someone_elses(app):
    """The record of where an answer came from no longer resolves (the
    entry's row is gone): nothing opens. Not public, no new audience,
    not in the owner's corpus, not readable."""
    from backend.utils.circles import thread_source
    from backend.utils.privacy import (
        can_ai_access_node, is_others_thread_answer)
    entry, r, a, n = _thread_with_answer(app)
    ben = _user("ben")
    NodeReader.query.filter_by(node_id=entry).delete()
    Node.query.filter_by(id=entry).delete()
    _db.session.commit()
    _db.session.expire_all()
    answer = _db.session.get(Node, a)
    assert answer.audience_root_id == entry        # the record stays
    source = thread_source(answer)
    assert source is not None and source.is_someone_elses(ben.id)
    assert is_others_thread_answer(answer)
    assert not can_ai_access_node(answer, ben.id)
    assert not _can_read(app, "ben", a)
    assert a not in _corpus_ids(ben)
    for node_id in (a, r):
        assert _put(app, "ben", node_id,
                    privacy_level="public").status_code == 403
        assert _put(app, "ben", node_id, privacy_level="circles",
                    circle_usernames=["dana"]).status_code == 400
        assert _client(app, "ben").put(
            f"/api/nodes/{node_id}/audience",
            json={"circle_usernames": ["dana"]}).status_code == 400
    assert _put(app, "ben", r, privacy_level="private",
                apply_to_descendants=True).status_code == 200
    assert _put(app, "ben", a, privacy_level="public").status_code == 403
    assert not _can_read(app, "dana", a) and not _can_read(app, "dana", r)





def test_the_cleanup_deletes_answers_of_deleted_entries(app):
    from backend.tasks.node_cleanup import sweep_answers_of_deleted_entries
    entry, r, a, n = _thread_with_answer(app)
    cutoff = datetime.utcnow() - timedelta(days=30)
    # Deleted yesterday: inside the grace period, nothing is marked.
    node = _db.session.get(Node, entry)
    node.deleted_at = datetime.utcnow() - timedelta(days=1)
    _db.session.commit()
    assert sweep_answers_of_deleted_entries(cutoff) == 0
    # Past it: the answer is marked, replies people typed are not.
    node.deleted_at = cutoff - timedelta(days=1)
    _db.session.commit()
    assert sweep_answers_of_deleted_entries(cutoff) == 1
    _db.session.expire_all()
    assert _db.session.get(Node, a).deleted_at == cutoff
    assert _db.session.get(Node, r).deleted_at is None
    assert _db.session.get(Node, n).deleted_at is None
    assert sweep_answers_of_deleted_entries(cutoff) == 0
    # anna's own AI reply in her own thread is not an answer of this kind.
    other = _share(app, names=["ben"], content="another")
    own = _client(app, "anna").post(f"/api/nodes/{other}/llm", json={})
    _db.session.get(Node, other).deleted_at = cutoff - timedelta(days=1)
    _db.session.commit()
    assert sweep_answers_of_deleted_entries(cutoff) == 0
    assert _db.session.get(Node, own.get_json()["node_id"]).deleted_at is None


def test_an_answer_of_a_wiped_or_missing_entry_is_swept_at_once(app):
    from backend.tasks.node_cleanup import sweep_answers_of_deleted_entries
    entry, r, a, n = _thread_with_answer(app)
    cutoff = datetime.utcnow() - timedelta(days=30)
    node = _db.session.get(Node, entry)
    node.deleted_at = datetime.utcnow()     # a tombstone left by a purge
    node.content = None
    _db.session.commit()
    assert sweep_answers_of_deleted_entries(cutoff) == 1
    entry2, r2, a2, n2 = _thread_with_answer(app)
    NodeReader.query.filter_by(node_id=entry2).delete()
    Node.query.filter_by(id=entry2).delete()
    _db.session.commit()
    assert sweep_answers_of_deleted_entries(cutoff) == 1
    _db.session.expire_all()
    assert _db.session.get(Node, a2).deleted_at == cutoff


# ── What the queries look like ──────────────────────────────────────────

def _sql(expr):
    return str(expr.compile(_db.engine, compile_kwargs={"literal_binds": True}))


def _main_filter(user_id):
    """The access expression from before Circles."""
    from sqlalchemy import or_
    from backend.utils.privacy import PrivacyLevel
    return or_(Node.user_id == user_id, Node.human_owner_id == user_id,
               Node.privacy_level == PrivacyLevel.PUBLIC)


def test_a_user_circles_does_not_concern_gets_the_old_query(app):
    """With the flag on or off, and whether or not shared entries and
    AI replies in shared threads exist in the database, the access expression for a
    user who shares nothing, reads nothing shared and owns no such
    answer is the one from before the feature, and it never carries a
    subquery for anyone."""
    from backend.utils.privacy import (
        accessible_nodes_filter_ignoring_deleted as flt, forget_circle_access)
    entry, r, a, n = _thread_with_answer(app)       # anna, ben, chidi
    dana, eve, ben, anna = (_user(x) for x in ("dana", "eve", "ben", "anna"))
    for config in ({"CIRCLES_V1": True},
                   {"CIRCLES_V1": False},
                   {"CIRCLES_V1": False, "CIRCLES_USER_IDS": set()}):
        app.config.update(config)
        forget_circle_access()
        for user in (dana, eve):
            for as_ai in (False, True):
                assert _sql(flt(Node, user.id, as_ai=as_ai)) == _sql(
                    _main_filter(user.id)), (config, user.username, as_ai)
        for user in (ben, anna):
            for as_ai in (False, True):
                assert "SELECT" not in _sql(flt(Node, user.id, as_ai=as_ai))
    # Off for everyone: anna's query is the old one too (nothing is
    # shared by her then); ben's differs only by leaving his answer out.
    assert _sql(flt(Node, anna.id)) == _sql(_main_filter(anna.id))
    assert f"node.id NOT IN ({a})" in _sql(flt(Node, ben.id))


def test_the_sets_are_read_once_per_session_and_dropped_on_a_write(app):
    from sqlalchemy import event
    from backend.utils.privacy import circle_sets
    entry = _share(app, names=["ben"])
    ben = _user("ben")
    seen = []

    def count(conn, cursor, statement, *_):
        seen.append(statement)
    event.listen(_db.engine, "before_cursor_execute", count)
    try:
        assert entry in circle_sets(ben.id).readable
        first = len(seen)
        assert first > 0
        for _ in range(5):
            assert entry in circle_sets(ben.id).readable
        assert len(seen) == first               # remembered
        NodeReader.query.filter_by(node_id=entry).delete()
        _db.session.commit()                    # a write drops it
        assert entry not in circle_sets(ben.id).readable
    finally:
        event.remove(_db.engine, "before_cursor_execute", count)


# ── The model of a reply ────────────────────────────────────────────────

def _ai_reply(parent_id, owner, model, privacy, pointer=None):
    llm = User.query.filter_by(username=model).first()
    if llm is None:
        llm = User(username=model, twitter_id=f"llm-{model}")
        _db.session.add(llm)
        _db.session.flush()
    n = Node(user_id=llm.id, human_owner_id=owner.id, parent_id=parent_id,
             node_type="llm", llm_model=model, privacy_level=privacy,
             ai_usage="chat", audience_root_id=pointer)
    n.set_content("an AI reply")
    _db.session.add(n)
    _db.session.commit()
    return n


def test_a_public_or_private_thread_resolves_the_model_as_before(app):
    """Outside a circle-shared thread the default model of a reply is
    resolved exactly as before this feature: the closest earlier AI
    reply in the thread decides, whoever it was made for, then the
    user's own model, then the server default. With the flag on or off."""
    from backend.utils.llm_nodes import resolve_chat_model
    anna, ben = _user("anna"), _user("ben")
    ben.preferred_model = "gpt-5"
    _db.session.commit()
    root = _client(app, "anna").post("/api/nodes/", json={
        "content": "public root", "privacy_level": "public",
        "ai_usage": "chat"}).get_json()["id"]
    annas_ai = _ai_reply(root, anna, "claude-x", "public")
    bens = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben in public", "privacy_level": "public",
        "ai_usage": "chat", "parent_id": annas_ai.id}).get_json()["id"]
    own_root = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben private", "ai_usage": "chat"}).get_json()["id"]
    for config in ({"CIRCLES_V1": True}, {"CIRCLES_V1": False,
                                          "CIRCLES_USER_IDS": set()}):
        app.config.update(config)
        assert resolve_chat_model(_db.session.get(Node, bens), ben) == (
            "claude-x", "predecessor")
        assert resolve_chat_model(_db.session.get(Node, own_root), ben) == (
            "gpt-5", "user_preference")
        assert resolve_chat_model(_db.session.get(Node, root), anna) == (
            "gpt-5", "default")


def test_in_a_shared_thread_only_my_own_replies_set_my_default(app):
    from backend.utils.llm_nodes import resolve_chat_model
    anna, ben = _user("anna"), _user("ben")
    entry = _share(app, names=["ben"])
    bens_answer = _ai_reply(entry, ben, "claude-x", "circles", pointer=entry)
    # anna's next reply below ben's answer: not moved to his model.
    assert resolve_chat_model(bens_answer, anna) == ("gpt-5", "default")
    # ben's own earlier reply does count for him.
    assert resolve_chat_model(bens_answer, ben) == ("claude-x", "predecessor")
