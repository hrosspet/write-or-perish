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
    monkeypatch.setattr(cz, "record_lookup_miss", lambda uid: None)


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

def _recorded_reply(parent_id, who="ben"):
    d = Draft(user_id=_user(who).id, node_id=None, parent_id=parent_id,
              session_id=str(uuid.uuid4()), streaming_status="completed",
              streaming_total_chunks=1, streaming_completed_chunks=1,
              privacy_level="circles", ai_usage="chat")
    d.set_content("recorded words")
    _db.session.add(d)
    _db.session.commit()
    return f"/api/drafts/streaming/{d.session_id}/save-as-node"


def test_a_recorded_reply_in_a_shared_thread_stays_circles(real_app):
    nid = _share(real_app)
    url = _recorded_reply(nid)
    r = _client(real_app, "ben").post(url, json={
        "content": "recorded words", "privacy_level": "private",
        "ai_usage": "chat"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_reply_required"
    assert Node.query.filter_by(parent_id=nid).count() == 0
    r = _client(real_app, "ben").post(url, json={
        "content": "recorded words", "privacy_level": "circles",
        "ai_usage": "chat"})
    assert r.status_code == 201, r.get_json()
    node = _db.session.get(Node, r.get_json()["id"])
    assert node.privacy_level == "circles"
    assert node.audience_root_id == nid
    assert _status(real_app, "chidi", f"/api/nodes/{node.id}") == 200
