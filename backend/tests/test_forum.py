"""Tests for Public Forum v1 (#228, SHARE_V1 family).

Covers: publish→public-node extraction (+ back-link), revoke/delete taking
the node down, the members feed (public roots only), the unauthenticated
thread endpoint (public subtree only, 404 parity for private/deleted),
public-reply enforcement on node creation, and the LLM ownership guard.

Mirrors test_textmode.py's mocking pattern (celery + LLM task module).
"""
import os
import sys
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
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node, ShareDraft  # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.share import share_bp
    from backend.routes.forum import forum_bp
    from backend.routes.nodes import nodes_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")
    app.register_blueprint(forum_bp, url_prefix="/api/forum")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        author = User(username="author", default_ai_usage="chat")
        visitor = User(username="visitor", default_ai_usage="chat")
        _db.session.add_all([author, visitor])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


def _client_for(app, username):
    client = app.test_client()
    user = User.query.filter_by(username=username).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _mk_share_draft(username="author", content="a public thought"):
    user = User.query.filter_by(username=username).first()
    share = ShareDraft(user_id=user.id, share_type="insight", status="draft")
    share.set_content(content)
    _db.session.add(share)
    _db.session.commit()
    return share


def _mk_node(username, content, parent=None, privacy="public",
             ai_usage="chat", node_type="user"):
    user = User.query.filter_by(username=username).first()
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None,
                node_type=node_type, privacy_level=privacy,
                ai_usage=ai_usage)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


# ── Publish → public node ────────────────────────────────────────────────

def test_publish_creates_public_root_node(app):
    client = _client_for(app, "author")
    share = _mk_share_draft()
    r = client.post(f"/api/share/{share.id}/publish")
    assert r.status_code == 200
    body = r.get_json()
    assert body["public_node_id"] is not None
    node = Node.query.get(body["public_node_id"])
    assert node.parent_id is None
    assert node.privacy_level == "public"
    assert node.get_content() == "a public thought"
    assert node.ai_usage == "chat"  # follows the author's default


def test_publish_backlinks_source_node(app):
    client = _client_for(app, "author")
    origin = _mk_node("author", "proposal origin", privacy="private")
    share = _mk_share_draft()
    share.source_node_id = origin.id
    _db.session.commit()
    r = client.post(f"/api/share/{share.id}/publish")
    assert r.status_code == 200
    assert Node.query.get(origin.id).linked_node_id == \
        r.get_json()["public_node_id"]


def test_revoke_soft_deletes_public_node(app):
    client = _client_for(app, "author")
    share = _mk_share_draft()
    node_id = client.post(
        f"/api/share/{share.id}/publish").get_json()["public_node_id"]
    r = client.post(f"/api/share/{share.id}/revoke")
    assert r.status_code == 200
    # Pointer kept for identity-follows-content republish.
    assert r.get_json()["public_node_id"] == node_id
    assert Node.query.get(node_id).deleted_at is not None


def test_delete_published_share_takes_node_down(app):
    client = _client_for(app, "author")
    share = _mk_share_draft()
    node_id = client.post(
        f"/api/share/{share.id}/publish").get_json()["public_node_id"]
    assert client.delete(f"/api/share/{share.id}").status_code == 200
    assert Node.query.get(node_id).deleted_at is not None


def test_deleting_public_node_via_node_ui_revokes_share(app):
    """Deleting the public node directly (kebab menu) must reconcile the
    ShareDraft — deleting IS revoking."""
    client = _client_for(app, "author")
    share = _mk_share_draft()
    node_id = client.post(
        f"/api/share/{share.id}/publish").get_json()["public_node_id"]
    r = client.delete(f"/api/nodes/{node_id}")
    assert r.status_code == 200
    _db.session.expire_all()
    reconciled = ShareDraft.query.get(share.id)
    assert reconciled.status == "revoked"
    assert reconciled.public_node_id == node_id  # kept for republish


def test_republish_unchanged_restores_same_node_and_discussion(app):
    """Identity follows content: unchanged republish undeletes the SAME
    node, so existing discussion reattaches."""
    client = _client_for(app, "author")
    share = _mk_share_draft()
    node_id = client.post(
        f"/api/share/{share.id}/publish").get_json()["public_node_id"]
    reply = _mk_node("visitor", "a reply", parent=Node.query.get(node_id))
    client.post(f"/api/share/{share.id}/revoke")
    assert Node.query.get(node_id).deleted_at is not None

    r = client.post(f"/api/share/{share.id}/publish")
    assert r.status_code == 200
    assert r.get_json()["public_node_id"] == node_id  # same node
    assert Node.query.get(node_id).deleted_at is None  # restored

    anon = app.test_client()
    thread = anon.get(f"/api/forum/node/{node_id}").get_json()["thread"]
    assert [c["id"] for c in thread["children"]] == [reply.id]


def test_republish_edited_creates_new_node(app):
    """Edited content severs the old discussion — replies referred to the
    old text."""
    client = _client_for(app, "author")
    share = _mk_share_draft()
    old_node_id = client.post(
        f"/api/share/{share.id}/publish").get_json()["public_node_id"]
    client.post(f"/api/share/{share.id}/revoke")
    r = client.patch(f"/api/share/{share.id}",
                     json={"content": "a public thought, sharpened"})
    assert r.status_code == 200

    r = client.post(f"/api/share/{share.id}/publish")
    new_node_id = r.get_json()["public_node_id"]
    assert new_node_id != old_node_id
    assert Node.query.get(old_node_id).deleted_at is not None  # stays down
    assert Node.query.get(new_node_id).get_content() == \
        "a public thought, sharpened"


# ── Feed ─────────────────────────────────────────────────────────────────

def test_feed_lists_public_roots_only(app):
    client = _client_for(app, "visitor")
    pub = _mk_node("author", "public root")
    _mk_node("author", "private root", privacy="private")
    reply = _mk_node("visitor", "public reply", parent=pub)
    deleted = _mk_node("author", "deleted root")
    from datetime import datetime
    deleted.deleted_at = datetime.utcnow()
    _db.session.commit()

    r = client.get("/api/forum/feed")
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert [i["id"] for i in items] == [pub.id]
    assert items[0]["reply_count"] == 1
    assert reply.id not in [i["id"] for i in items]


# ── Public thread endpoint (the funnel) ──────────────────────────────────

def test_public_thread_serves_public_subtree_anonymously(app):
    pub = _mk_node("author", "root piece")
    _mk_node("visitor", "public reply", parent=pub)
    llm = _mk_node("visitor", "an AI response", parent=pub,
                   node_type="llm")
    llm.llm_model = "gpt-5"
    _mk_node("author", "private aside", parent=pub, privacy="private")
    _db.session.commit()

    anon = app.test_client()
    r = anon.get(f"/api/forum/node/{pub.id}")
    assert r.status_code == 200
    thread = r.get_json()["thread"]
    assert thread["id"] == pub.id
    contents = [c["content"] for c in thread["children"]]
    assert "public reply" in contents
    assert "an AI response" in contents
    assert "private aside" not in contents


def test_llm_nodes_attribute_to_human_owner(app):
    """The public thread shows 'model · via <human>' — the username field
    for LLM nodes must be the HUMAN owner, not the synthetic model
    account."""
    pub = _mk_node("author", "root piece")
    reply = _mk_node("visitor", "public reply", parent=pub)
    llm_account = User(username="claude-opus-4.6")
    _db.session.add(llm_account)
    _db.session.commit()
    visitor = User.query.filter_by(username="visitor").first()
    llm = Node(user_id=llm_account.id, human_owner_id=visitor.id,
               parent_id=reply.id, node_type="llm",
               privacy_level="public", ai_usage="chat")
    llm.llm_model = "claude-opus-4.6"
    llm.set_content("a response")
    _db.session.add(llm)
    _db.session.commit()

    anon = app.test_client()
    thread = anon.get(f"/api/forum/node/{pub.id}").get_json()["thread"]
    llm_ser = thread["children"][0]["children"][0]
    assert llm_ser["node_type"] == "llm"
    assert llm_ser["username"] == "visitor"  # the human, not the model


def test_public_thread_deep_link_resolves_to_root(app):
    pub = _mk_node("author", "root piece")
    reply = _mk_node("visitor", "public reply", parent=pub)
    anon = app.test_client()
    r = anon.get(f"/api/forum/node/{reply.id}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["thread"]["id"] == pub.id
    assert body["focus_id"] == reply.id


def test_public_thread_404_parity(app):
    private = _mk_node("author", "secret", privacy="private")
    deleted = _mk_node("author", "gone")
    from datetime import datetime
    deleted.deleted_at = datetime.utcnow()
    _db.session.commit()
    anon = app.test_client()
    for nid in (private.id, deleted.id, 999999):
        assert anon.get(f"/api/forum/node/{nid}").status_code == 404


# ── Public-reply enforcement ─────────────────────────────────────────────

def test_reply_under_public_node_must_be_public(app):
    client = _client_for(app, "visitor")
    pub = _mk_node("author", "root piece")
    r = client.post("/api/nodes/",
                    json={"content": "sneaky private reply",
                          "parent_id": pub.id,
                          "privacy_level": "private",
                          "ai_usage": "chat"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "public_reply_required"

    r = client.post("/api/nodes/",
                    json={"content": "an honest public reply",
                          "parent_id": pub.id,
                          "privacy_level": "public",
                          "ai_usage": "chat"})
    assert r.status_code == 201


def test_reply_under_private_node_unrestricted(app):
    client = _client_for(app, "author")
    root = _mk_node("author", "private root", privacy="private")
    r = client.post("/api/nodes/",
                    json={"content": "private reply",
                          "parent_id": root.id,
                          "privacy_level": "private",
                          "ai_usage": "chat"})
    assert r.status_code == 201


# ── LLM ownership guard ──────────────────────────────────────────────────

def test_llm_generation_forbidden_on_foreign_nodes(app):
    client = _client_for(app, "visitor")
    pub = _mk_node("author", "root piece")
    r = client.post(f"/api/nodes/{pub.id}/llm", json={})
    assert r.status_code == 403


def test_llm_generation_allowed_on_own_public_reply(app):
    client = _client_for(app, "visitor")
    pub = _mk_node("author", "root piece")
    reply = _mk_node("visitor", "public reply", parent=pub)
    r = client.post(f"/api/nodes/{reply.id}/llm", json={})
    assert r.status_code == 202  # accepted — generation enqueued
