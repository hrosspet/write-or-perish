"""Circles behind main's account gates (merge of main into feat/circles).

The approval gate and the subscription gate are before_request hooks of
create_app(), so this file builds the real app (sqlite in memory, the
celery glue stubbed, as test_let_in_without_approval.py does), with
Circles on for everyone. Stripe is never called, no model is called.

* A reader the admin blocked, a reader not let in (payments on, no
  subscription) and, payments off, a deactivated reader read nothing
  shared with them, through the node routes or the Circles routes.
* A shared entry of an author the admin blocked (or deactivated, payments
  off) or of an author who deleted the account is not served to its
  readers; it opens again when the author is unblocked.
* A recorded reply under a shared node is "circles" when it is saved
  with the form's settings (main's save-as-node takes them), as a typed
  reply is.
* Payments on, an author held as spam (#713) is treated as blocked: the
  shared entry is not served, and the name cannot be shared with.
* An email account with Circles ties stays when its person moves into
  the account prepared for them (#714).

anna shares; ben and chidi read. Every account has the "pro" plan (set by
hand), so the subscription gate lets it in unless a test changes that.
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ.setdefault("AUDIO_STORAGE_PATH",
                      tempfile.mkdtemp(prefix="wop-audio-test-"))
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import Draft, Node, User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

SECRET = "ANNAS-SHARED-WORDS"


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


def _stubbed_backend_modules():
    return {k: v for k, v in sys.modules.items()
            if k.startswith("backend.") and isinstance(v, MagicMock)}


@pytest.fixture
def real_app(monkeypatch):
    stubs = _stubbed_backend_modules()
    for k in stubs:
        del sys.modules[k]
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["PAYMENTS_ENABLED"] = True
    app.config["DEPLOY_ENV"] = "testing"
    app.config["CIRCLES_V1"] = True
    app.config["CIRCLES_USER_IDS"] = set()
    with app.app_context():
        _db.create_all()
        for name in ("anna", "ben", "chidi"):
            _db.session.add(User(
                username=name, approved=True, plan="pro",
                accepted_terms_version="x", default_ai_usage="chat",
                email=f"{name}@example.com"))
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    sys.modules.update(stubs)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """The name-lookup limit lives in Redis; tests never touch one. Each
    test starts without the process-wide memory of whether any thread
    pointer exists."""
    from backend.utils import circles as cz
    from backend.utils import privacy
    privacy._pointers_seen["at"] = 0.0
    monkeypatch.setattr(cz, "lookup_blocked", lambda uid: False)
    monkeypatch.setattr(cz, "record_lookup_miss", lambda uid, count=1: None)


def _user(name):
    return User.query.filter_by(username=name).first()


def _client(app, name):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return c


def _share(app, names=("ben", "chidi")):
    r = _client(app, "anna").post("/api/nodes/", json={
        "content": SECRET, "privacy_level": "circles", "ai_usage": "chat",
        "circle_ids": [], "circle_usernames": list(names)})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["audience_set"] is True
    return r.get_json()["id"]


def _status(app, who, url):
    return _client(app, who).get(url).status_code


def _shared_ids(app, who):
    r = _client(app, who).get("/api/circles/shared-with-me")
    assert r.status_code == 200, r.get_json()
    return [e["id"] for p in r.get_json()["people"] for e in p["entries"]]


def _block(name):
    from backend.utils.approval import block
    assert block(_user(name))
    _db.session.commit()


def _unblock(name):
    from backend.utils.approval import unblock
    assert unblock(_user(name))
    _db.session.commit()


# ── A reader the gates hold reads nothing shared ─────────────────────────

def test_a_blocked_reader_reads_nothing_shared(real_app):
    nid = _share(real_app)
    assert _status(real_app, "ben", f"/api/nodes/{nid}") == 200
    _block("ben")
    for url in (f"/api/nodes/{nid}", "/api/circles/shared-with-me",
                "/api/circles"):
        r = _client(real_app, "ben").get(url)
        assert r.status_code == 403, url
        assert SECRET not in r.get_data(as_text=True)
    r = _client(real_app, "ben").post("/api/nodes/", json={
        "content": "a reply", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid})
    assert r.status_code == 403
    # The other reader is not affected; ben reads again once unblocked.
    assert _status(real_app, "chidi", f"/api/nodes/{nid}") == 200
    _unblock("ben")
    assert _status(real_app, "ben", f"/api/nodes/{nid}") == 200
    assert _shared_ids(real_app, "ben") == [nid]


def test_a_reader_not_let_in_reads_nothing_shared(real_app):
    nid = _share(real_app)
    # The trial step: approved, plan "free", never a subscription.
    _user("ben").plan = "free"
    _db.session.commit()
    for url in (f"/api/nodes/{nid}", "/api/circles/shared-with-me",
                "/api/circles"):
        r = _client(real_app, "ben").get(url)
        assert r.status_code == 402, url
        assert r.get_json()["error"] == "no_subscription"
        assert SECRET not in r.get_data(as_text=True)


def test_payments_off_a_deactivated_reader_reads_nothing_shared(real_app):
    real_app.config["PAYMENTS_ENABLED"] = False
    nid = _share(real_app)
    ben = _user("ben")
    ben.approved = False
    ben.deactivated_at = datetime.utcnow()
    _db.session.commit()
    for url in (f"/api/nodes/{nid}", "/api/circles/shared-with-me"):
        r = _client(real_app, "ben").get(url)
        assert r.status_code == 403, url
        assert SECRET not in r.get_data(as_text=True)


# ── An author the admin turned off, or gone, is not served ──────────────

def test_a_blocked_authors_shared_entry_is_not_served(real_app):
    nid = _share(real_app)
    reply = _client(real_app, "ben").post("/api/nodes/", json={
        "content": "ben replies", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid})
    assert reply.status_code == 201, reply.get_json()
    reply_id = reply.get_json()["id"]
    assert _status(real_app, "chidi", f"/api/nodes/{reply_id}") == 200
    _block("anna")
    for who in ("ben", "chidi"):
        r = _client(real_app, who).get(f"/api/nodes/{nid}")
        assert r.status_code == 404, who
        assert SECRET not in r.get_data(as_text=True)
        assert _shared_ids(real_app, who) == []
    # The thread closes with its entry: another reader's reply too.
    assert _status(real_app, "chidi", f"/api/nodes/{reply_id}") == 404
    # Nobody writes into it meanwhile.
    r = _client(real_app, "chidi").post("/api/nodes/", json={
        "content": "chidi replies", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid})
    assert r.status_code == 404
    _unblock("anna")
    assert _status(real_app, "chidi", f"/api/nodes/{nid}") == 200
    assert _shared_ids(real_app, "chidi") == [nid]


def test_payments_off_a_deactivated_authors_entry_is_not_served(real_app):
    real_app.config["PAYMENTS_ENABLED"] = False
    nid = _share(real_app)
    anna = _user("anna")
    anna.approved = False
    anna.deactivated_at = datetime.utcnow()
    _db.session.commit()
    assert _status(real_app, "ben", f"/api/nodes/{nid}") == 404
    assert _shared_ids(real_app, "ben") == []


def test_a_deleted_authors_shared_entry_is_not_served(real_app):
    nid = _share(real_app)
    _user("anna").deleted_at = datetime.utcnow()   # in its grace period
    _db.session.commit()
    r = _client(real_app, "ben").get(f"/api/nodes/{nid}")
    assert r.status_code in (404, 410)
    assert SECRET not in r.get_data(as_text=True)
    assert _shared_ids(real_app, "ben") == []


# ── A recorded reply saved with the form's settings ─────────────────────

def _recorded_reply(parent_id, who="ben", stored="circles"):
    """A finished recording under *parent_id*, its level as the session
    started with (*stored*); the URL that saves it as an entry."""
    d = Draft(user_id=_user(who).id, node_id=None, parent_id=parent_id,
              session_id=str(uuid.uuid4()), streaming_status="completed",
              streaming_total_chunks=1, streaming_completed_chunks=1,
              privacy_level=stored, ai_usage="chat")
    d.set_content("recorded words")
    _db.session.add(d)
    _db.session.commit()
    return f"/api/drafts/streaming/{d.session_id}/save-as-node"


@pytest.mark.parametrize("who,stored,form", [
    # The form's level (main's save with the form's settings).
    ("ben", "circles", {"privacy_level": "private", "ai_usage": "chat"}),
    ("ben", "circles", {"privacy_level": "public", "ai_usage": "chat"}),
    ("anna", "circles", {"privacy_level": "public", "ai_usage": "chat"}),
    # No form settings (an older iPhone build): the level the recording
    # started with, the entry having been shared since.
    ("ben", "private", {}),
    ("ben", "public", {}),
    ("anna", "public", {}),
])
def test_a_recorded_reply_under_a_shared_node_is_only_saved_as_circles(
        real_app, who, stored, form):
    nid = _share(real_app)
    url = _recorded_reply(nid, who=who, stored=stored)
    r = _client(real_app, who).post(
        url, json={"content": "recorded words", **form})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["code"] == "circle_reply_required"
    assert Node.query.filter_by(parent_id=nid).count() == 0
    # The session stays: the recording can be saved again.
    assert Draft.query.count() == 1


@pytest.mark.parametrize("form", [
    {"privacy_level": "circles", "ai_usage": "chat"},
    {},   # no form settings: the level the recording started with
])
def test_control_a_recorded_reply_saved_as_circles_joins_the_thread(
        real_app, form):
    nid = _share(real_app)
    url = _recorded_reply(nid, stored="circles")
    r = _client(real_app, "ben").post(
        url, json={"content": "recorded words", **form})
    assert r.status_code == 201, r.get_json()
    node = _db.session.get(Node, r.get_json()["id"])
    assert node.privacy_level == "circles"
    assert node.audience_root_id == nid
    assert _status(real_app, "chidi", f"/api/nodes/{node.id}") == 200


def test_control_a_recorded_reply_under_an_unshared_node_keeps_its_level(
        real_app):
    r = _client(real_app, "anna").post("/api/nodes/", json={
        "content": "a private note", "privacy_level": "private",
        "ai_usage": "chat"})
    assert r.status_code == 201, r.get_json()
    parent = r.get_json()["id"]
    url = _recorded_reply(parent, who="anna", stored="private")
    r = _client(real_app, "anna").post(url, json={"content": "words"})
    assert r.status_code == 201, r.get_json()
    node = _db.session.get(Node, r.get_json()["id"])
    assert node.privacy_level == "private"
    assert node.audience_root_id is None


# ── Someone else's thread is never made public, by any create path ──────
# (review of the merge, finding 2). ben's reply in anna's thread is taken
# back to Only me, ben answers it Only me: the branch is his alone, its
# parent is not shared, and the thread is still anna's.

def _only_me_branch(app):
    """(entry, ben's taken-back reply, ben's Only-me answer to it)."""
    nid = _share(app)
    r = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben replies", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid})
    assert r.status_code == 201, r.get_json()
    reply = r.get_json()["id"]
    r = _client(app, "ben").put(f"/api/nodes/{reply}", json={
        "content": "ben replies", "privacy_level": "private"})
    assert r.status_code == 200, r.get_json()
    r = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben goes on, Only me", "privacy_level": "private",
        "ai_usage": "chat", "parent_id": reply})
    assert r.status_code == 201, r.get_json()
    deeper = r.get_json()["id"]
    assert _db.session.get(Node, reply).privacy_level == "private"
    assert _db.session.get(Node, deeper).audience_root_id is None
    return nid, reply, deeper


def _create(app, route, parent, level):
    """One create path, *parent* and *level*; the response."""
    import io
    c = _client(app, "ben")
    if route == "typed":
        return c.post("/api/nodes/", json={
            "content": "words", "privacy_level": level, "ai_usage": "chat",
            "parent_id": parent})
    if route == "upload":
        return c.post("/api/nodes/", data={
            "audio_file": (io.BytesIO(b"\x00" * 64), "note.webm"),
            "parent_id": str(parent), "privacy_level": level,
            "ai_usage": "chat"}, content_type="multipart/form-data")
    if route == "upload_init":
        return c.post("/api/nodes/upload/init", json={
            "filename": "note.m4a", "filesize": 1000, "total_chunks": 1,
            "upload_id": f"up-{uuid.uuid4().hex[:8]}", "parent_id": parent,
            "privacy_level": level, "ai_usage": "chat"})
    if route == "streaming_init":
        return c.post("/api/nodes/streaming/init", json={
            "parent_id": parent, "privacy_level": level, "ai_usage": "chat"})
    if route == "recording_start":
        return c.post("/api/drafts/streaming/init", json={
            "parent_id": parent, "privacy_level": level, "ai_usage": "chat"})
    if route == "save_form":
        url = _recorded_reply(parent, stored="private")
        return c.post(url, json={"content": "words", "privacy_level": level,
                                 "ai_usage": "chat"})
    if route == "save_stored":
        url = _recorded_reply(parent, stored=level)
        return c.post(url, json={"content": "words"})
    raise AssertionError(route)


ROUTES = ("typed", "upload", "upload_init", "streaming_init",
          "recording_start", "save_form", "save_stored")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("depth", ["reply", "deeper"])
def test_a_reply_in_someone_elses_thread_is_never_everyone(
        real_app, route, depth):
    nid, reply, deeper = _only_me_branch(real_app)
    parent = reply if depth == "reply" else deeper
    before = Node.query.count()
    r = _create(real_app, route, parent, "public")
    assert r.status_code == 403, (route, r.status_code, r.get_json())
    assert r.get_json()["code"] == "circle_thread_public"
    _db.session.expire_all()
    assert Node.query.count() == before
    assert not Node.query.filter_by(privacy_level="public").count()


@pytest.mark.parametrize("route", ROUTES)
def test_control_only_me_under_ones_own_only_me_node_is_allowed(
        real_app, route):
    nid, reply, deeper = _only_me_branch(real_app)
    r = _create(real_app, route, deeper, "private")
    assert r.status_code in (200, 201), (route, r.status_code, r.get_json())


@pytest.mark.parametrize("route", ["typed", "save_form", "recording_start"])
def test_circles_in_ones_own_only_me_branch_is_refused_on_every_path(
        real_app, route):
    """The typed path's rule: the thread's people do not read this branch,
    so a node in it stays Only me."""
    nid, reply, deeper = _only_me_branch(real_app)
    r = _create(real_app, route, deeper, "circles")
    assert r.status_code == 400, (route, r.status_code, r.get_json())
    assert r.get_json()["code"] == "circle_only_me_branch"


def test_control_the_author_may_publish_under_her_own_entry(real_app):
    """The rule is about someone else's thread: anna's own Only-me
    answer in her own thread may get a public reply from her."""
    nid = _share(real_app)
    r = _client(real_app, "anna").post("/api/nodes/", json={
        "content": "anna answers", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid})
    assert r.status_code == 201, r.get_json()
    mine = r.get_json()["id"]
    r = _client(real_app, "anna").put(f"/api/nodes/{mine}", json={
        "content": "anna answers", "privacy_level": "private"})
    assert r.status_code == 200, r.get_json()
    r = _client(real_app, "anna").post("/api/nodes/", json={
        "content": "anna, public", "privacy_level": "public",
        "ai_usage": "chat", "parent_id": mine})
    assert r.status_code == 201, r.get_json()


# ── The download holds all of the reader's writing in a shared thread ───
# (review of the merge, finding 3)

def _download(app, who):
    r = _client(app, who).get("/api/export/threads")
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_the_download_holds_a_readers_only_me_branch(real_app):
    nid, reply, deeper = _only_me_branch(real_app)
    r = _client(real_app, "chidi").post("/api/nodes/", json={
        "content": "CHIDI REPLIES", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": nid})
    assert r.status_code == 201, r.get_json()
    text = _download(real_app, "ben")
    assert "ben replies" in text                 # taken back, pointer kept
    assert "ben goes on, Only me" in text        # no pointer, below it
    assert SECRET not in text and "CHIDI REPLIES" not in text


def test_the_download_holds_a_session_started_under_someone_elses_entry(
        real_app):
    """Text mode started under anna's entry: ben's message hangs directly
    under her node with no pointer (prompt nodes never join a thread)."""
    nid = _share(real_app)
    ben = _user("ben")
    prompt = Node(user_id=ben.id, human_owner_id=ben.id, parent_id=nid,
                  node_type="user", privacy_level="private",
                  ai_usage="chat", prompt_key="textmode")
    prompt.set_content("BEN'S PROMPT TEXT")
    _db.session.add(prompt)
    _db.session.flush()
    msg = Node(user_id=ben.id, human_owner_id=ben.id, parent_id=prompt.id,
               node_type="user", privacy_level="private", ai_usage="chat")
    msg.set_content("BEN THINKS ABOUT IT")
    _db.session.add(msg)
    _db.session.commit()
    text = _download(real_app, "ben")
    assert "BEN THINKS ABOUT IT" in text
    assert "BEN'S PROMPT TEXT" not in text       # a prompt, not a reply
    assert SECRET not in text
    # anna's download holds none of ben's writing.
    theirs = _download(real_app, "anna")
    assert SECRET in theirs
    assert "BEN THINKS ABOUT IT" not in theirs


# ── Held as spam (#713, merge of train #730) ────────────────────────────
# Payments on, an account the admin marked as spam that the gate let in by
# itself (let_in_at) is held: the gate treats it as blocked
# (approval.not_active). Circles does the same: its shared entries close
# to their readers, and its name cannot be shared with. The admin's own
# approval (no let_in_at) wins over the mark; payments off the mark holds
# nobody.

def _mark_spam(name, let_in=True):
    user = _user(name)
    user.spam = True
    user.let_in_at = datetime.utcnow() if let_in else None
    _db.session.commit()


def test_a_spam_held_authors_shared_entry_is_not_served(real_app):
    nid = _share(real_app)
    _mark_spam("anna")
    for who in ("ben", "chidi"):
        r = _client(real_app, who).get(f"/api/nodes/{nid}")
        assert r.status_code == 404, who
        assert SECRET not in r.get_data(as_text=True)
        assert _shared_ids(real_app, who) == []
    # Activated by the admin's hand: no let_in_at, the mark holds no more.
    _user("anna").let_in_at = None
    _db.session.commit()
    assert _status(real_app, "ben", f"/api/nodes/{nid}") == 200
    assert _shared_ids(real_app, "ben") == [nid]


def test_control_payments_off_the_spam_mark_closes_nothing(real_app):
    real_app.config["PAYMENTS_ENABLED"] = False
    nid = _share(real_app)
    _mark_spam("anna")
    assert _status(real_app, "ben", f"/api/nodes/{nid}") == 200
    assert _shared_ids(real_app, "ben") == [nid]


@pytest.mark.parametrize("payments,let_in,shared", [
    (True, True, False),    # held as spam: no such name
    (True, False, True),    # approved by the admin's hand: the mark yields
    (False, True, True),    # payments off: the mark holds nobody
])
def test_a_spam_held_account_cannot_be_named(real_app, payments, let_in,
                                             shared):
    real_app.config["PAYMENTS_ENABLED"] = payments
    _mark_spam("ben", let_in=let_in)
    r = _client(real_app, "anna").post("/api/nodes/", json={
        "content": SECRET, "privacy_level": "circles", "ai_usage": "chat",
        "circle_ids": [], "circle_usernames": ["ben"]})
    if shared:
        assert r.status_code == 201, r.get_json()
    else:
        assert r.status_code == 400, r.get_json()
        assert "No one on Loore has that name" in r.get_json()["error"]
        assert Node.query.count() == 0


# ── The move into a prepared account keeps an account Circles ties ──────
# (#714, merge of train #730): an email account with no writing is
# deleted when its person opens the account prepared for them; one with a
# circle of its own, or a place in someone else's circle or entry, stays.

def _empty_email_account():
    dana = User(username="dana", approved=True, plan="free",
                accepted_terms_version="x", default_ai_usage="chat",
                email="dana@example.com")
    _db.session.add(dana)
    _db.session.commit()
    return dana


def test_an_account_circles_tie_stays_on_the_move(real_app):
    from backend.models import Circle, CircleMember, NodeReader
    from backend.utils.prepared_accounts import STAYS_KEPT, stays_reason
    dana = _empty_email_account()
    assert stays_reason(dana) is None            # deleted on the move
    anna = _user("anna")
    # A place in someone else's circle.
    circle = Circle(user_id=anna.id, name="Friends")
    _db.session.add(circle)
    _db.session.flush()
    member = CircleMember(circle_id=circle.id, user_id=dana.id)
    _db.session.add(member)
    _db.session.commit()
    assert stays_reason(dana) == STAYS_KEPT
    _db.session.delete(member)
    _db.session.commit()
    assert stays_reason(dana) is None
    # Named for one entry.
    nid = _share(real_app, names=("ben",))
    reader = NodeReader(node_id=nid, user_id=dana.id)
    _db.session.add(reader)
    _db.session.commit()
    assert stays_reason(dana) == STAYS_KEPT
    _db.session.delete(reader)
    _db.session.commit()
    assert stays_reason(dana) is None
    # A circle of its own.
    _db.session.add(Circle(user_id=dana.id, name="Mine"))
    _db.session.commit()
    assert stays_reason(dana) == STAYS_KEPT
