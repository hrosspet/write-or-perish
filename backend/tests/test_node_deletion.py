"""Tests for soft-delete + tombstones + cleanup task.

Covers the core behavior from the soft-delete plan without requiring
concurrent transactions (those need Postgres + threading patterns the
codebase doesn't currently exercise; the FK-vs-FOR-UPDATE locking is
verified by inspection / staging soak instead).

Patterned after test_search.py: sqlite in-memory, minimal Flask app,
ENCRYPTION_DISABLED.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

# ── Force-import real modules ────────────────────────────────────────────
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node, NodeVersion  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")

    return app


@pytest.fixture
def app():
    # Same isolation pattern as test_search.py — only flask_login,
    # backend.routes.*, and backend.models are touched. The route handler
    # in nodes.py:delete_node passes `current_user.id` explicitly to
    # can_user_edit_node, so we don't need to reload backend.utils.privacy
    # (which would unbind references that other test files rely on).
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
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def alice(app):
    u = User(username="alice", twitter_id="alice-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def bob(app):
    u = User(username="bob", twitter_id="bob-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(app_client, user):
    with app_client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _make_node(user, parent=None, content="hello", node_type="user"):
    node = Node(
        user_id=user.id,
        human_owner_id=user.id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        privacy_level="private",
        ai_usage="none",
        token_count=1,
    )
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


# ── 1. Single node, no children ─────────────────────────────────────────

def test_soft_delete_no_children_sets_deleted_at(app, alice):
    n = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    resp = client.delete(f"/nodes/{n.id}")
    assert resp.status_code == 200
    assert resp.json["scheduled"] == 1
    assert resp.json["grace_days"] == 30
    refreshed = Node.query.get(n.id)
    assert refreshed.deleted_at is not None


# ── 2. Single node with children, no descendants ────────────────────────

def test_soft_delete_root_only_leaves_children_alive(app, alice):
    parent = _make_node(alice)
    child = _make_node(alice, parent=parent)
    client = app.test_client()
    _login(client, alice)
    resp = client.delete(f"/nodes/{parent.id}")
    assert resp.status_code == 200
    assert resp.json["scheduled"] == 1  # only the root
    assert Node.query.get(parent.id).deleted_at is not None
    assert Node.query.get(child.id).deleted_at is None  # still alive


# ── 3. Delete with descendants, mixed ownership ─────────────────────────

def test_delete_with_descendants_skips_other_users(app, alice, bob):
    a_root = _make_node(alice)
    a_mid = _make_node(alice, parent=a_root)
    b_reply = _make_node(bob, parent=a_mid)
    a_grand = _make_node(alice, parent=a_mid)
    client = app.test_client()
    _login(client, alice)
    resp = client.delete(
        f"/nodes/{a_root.id}?delete_descendants=true",
    )
    assert resp.status_code == 200
    # a_root + a_mid + a_grand = 3 owned nodes; b_reply preserved.
    assert resp.json["scheduled"] == 3
    assert Node.query.get(a_root.id).deleted_at is not None
    assert Node.query.get(a_mid.id).deleted_at is not None
    assert Node.query.get(a_grand.id).deleted_at is not None
    assert Node.query.get(b_reply.id).deleted_at is None  # preserved


# ── 5. Reply blocked on soft-deleted parent ─────────────────────────────

def test_create_blocked_when_parent_soft_deleted(app, alice):
    parent = _make_node(alice)
    parent.deleted_at = datetime.utcnow()
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)
    resp = client.post(
        "/nodes/",
        json={"content": "child", "parent_id": parent.id},
    )
    assert resp.status_code == 410


# ── 6. Permission denied on others' nodes ───────────────────────────────

def test_delete_others_node_returns_403(app, alice, bob):
    n = _make_node(alice)
    client = app.test_client()
    _login(client, bob)
    resp = client.delete(f"/nodes/{n.id}")
    assert resp.status_code == 403
    # Should not have set deleted_at.
    assert Node.query.get(n.id).deleted_at is None


# ── 8. Pinned node clears pinned_at on delete ───────────────────────────

def test_delete_clears_pinned_at(app, alice):
    n = _make_node(alice)
    n.pinned_at = datetime.utcnow()
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)
    client.delete(f"/nodes/{n.id}")
    assert Node.query.get(n.id).pinned_at is None


# ── 14. Cleanup task wipes content after grace ──────────────────────────

def test_cleanup_wipes_content_for_tombstones(app, alice):
    parent = _make_node(alice, content="parent body")
    child = _make_node(alice, parent=parent, content="child body")
    # Add a NodeVersion so we can verify it gets deleted.
    nv = NodeVersion(node_id=parent.id, content="prior")
    _db.session.add(nv)
    # Soft-delete only the parent.
    parent.deleted_at = datetime.utcnow() - timedelta(days=31)
    _db.session.commit()
    # Run cleanup.
    from backend.tasks.node_cleanup import (
        _wipe_content_and_versions, _full_purge,
    )
    # Manually invoke the per-node logic (avoid Celery beat plumbing).
    refreshed = Node.query.get(parent.id)
    child_count = Node.query.filter_by(parent_id=parent.id).count()
    assert child_count == 1
    _wipe_content_and_versions(refreshed)
    _db.session.commit()
    refreshed = Node.query.get(parent.id)
    assert refreshed.content is None
    assert NodeVersion.query.filter_by(node_id=parent.id).count() == 0
    # Child still alive.
    assert Node.query.get(child.id).deleted_at is None


# ── 14b. Cleanup task purges leaf rows ──────────────────────────────────

def test_cleanup_purges_leaf_after_grace(app, alice):
    n = _make_node(alice)
    n.deleted_at = datetime.utcnow() - timedelta(days=31)
    _db.session.commit()
    nid = n.id
    from backend.tasks.node_cleanup import _full_purge
    refreshed = Node.query.get(nid)
    _full_purge(refreshed)
    _db.session.commit()
    assert Node.query.get(nid) is None


# ── 13. Tombstone privacy: viewer needs pre-deletion access ─────────────

def test_can_user_view_tombstone_requires_pre_deletion_access(app, alice, bob):
    # Alice creates a private node, soft-deletes it; bob (no access)
    # should not be able to view the tombstone (would leak metadata).
    n = _make_node(alice)
    n.deleted_at = datetime.utcnow()
    _db.session.commit()
    from backend.utils.privacy import (
        can_user_view_tombstone, _can_user_access_ignoring_deleted,
    )
    assert _can_user_access_ignoring_deleted(n, alice.id) is True
    assert _can_user_access_ignoring_deleted(n, bob.id) is False
    assert can_user_view_tombstone(n, alice.id) is True
    assert can_user_view_tombstone(n, bob.id) is False


# ── Public node tombstone is visible to all ─────────────────────────────

def test_public_node_tombstone_visible_to_all(app, alice, bob):
    n = _make_node(alice)
    n.privacy_level = "public"
    n.deleted_at = datetime.utcnow()
    _db.session.commit()
    from backend.utils.privacy import can_user_view_tombstone
    assert can_user_view_tombstone(n, alice.id) is True
    assert can_user_view_tombstone(n, bob.id) is True


# ── Soft-deleted nodes excluded from feed ───────────────────────────────

def test_feed_excludes_soft_deleted(app, alice):
    alive = _make_node(alice, content="alive")
    deleted = _make_node(alice, content="dead")
    deleted.deleted_at = datetime.utcnow()
    _db.session.commit()
    # Re-create the app with feed_bp registered.
    from backend.routes.feed import feed_bp
    app.register_blueprint(feed_bp, url_prefix="/api")
    client = app.test_client()
    _login(client, alice)
    resp = client.get("/api/feed")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json["nodes"]]
    assert alive.id in ids
    assert deleted.id not in ids


# ── 23. Direct URL to soft-deleted node returns 404 ────────────────────

def test_direct_url_to_soft_deleted_node_404s(app, alice):
    n = _make_node(alice)
    n.deleted_at = datetime.utcnow()
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)
    resp = client.get(f"/nodes/{n.id}")
    assert resp.status_code == 404


# ── Tombstone appears in ancestors when traversing through it ──────────

def test_tombstone_in_ancestor_breadcrumb(app, alice, bob):
    # Public node so bob has pre-deletion access via the breadcrumb walk
    # from his nested public reply.
    a_root = _make_node(alice)
    a_root.privacy_level = "public"
    b_reply = _make_node(bob, parent=a_root)
    b_reply.privacy_level = "public"
    a_root.deleted_at = datetime.utcnow()
    _db.session.commit()
    client = app.test_client()
    _login(client, bob)
    resp = client.get(f"/nodes/{b_reply.id}")
    assert resp.status_code == 200
    ancestors = resp.json["ancestors"]
    assert len(ancestors) == 1
    assert ancestors[0]["deleted"] is True
    assert ancestors[0]["id"] == a_root.id
    assert "content" not in ancestors[0]  # tombstone shell only


def test_tombstone_ancestor_hidden_without_pre_access(app, alice, bob):
    # Alice's node is private. Bob has no pre-deletion access. After
    # soft-delete, the ancestor must be omitted entirely from bob's
    # breadcrumb (not surfaced as a tombstone) — username/timestamp
    # would otherwise leak.
    a_root = _make_node(alice)  # private by default
    # Bob can post a reply only if alice's node is public OR shared. For
    # this test we manufacture the tree directly bypassing the API.
    b_reply = _make_node(bob, parent=a_root)
    b_reply.privacy_level = "public"
    a_root.deleted_at = datetime.utcnow()
    _db.session.commit()
    client = app.test_client()
    _login(client, bob)
    resp = client.get(f"/nodes/{b_reply.id}")
    assert resp.status_code == 200
    # Alice's tombstone must NOT appear: it was private pre-deletion, so
    # bob has no pre-deletion access. The ancestors list is empty.
    assert resp.json["ancestors"] == []


# ── §5a Export: tombstones in mixed threads ────────────────────────────

def test_export_includes_tombstones_in_mixed_thread(app, alice):
    """A thread with one alive + one soft-deleted node: export shows
    both, the deleted one as a `[Node deleted by author]` placeholder.
    """
    parent = _make_node(alice, content="parent body")
    child = _make_node(alice, parent=parent, content="child body")
    child.deleted_at = datetime.utcnow()
    _db.session.commit()

    from backend.routes.export_data import build_user_export_content
    content = build_user_export_content(alice)
    assert content is not None
    assert "parent body" in content
    assert "[Node deleted by author]" in content
    # Child's actual content must NOT leak into the export.
    assert "child body" not in content


def test_export_skips_fully_deleted_thread(app, alice):
    """A thread where every node is soft-deleted: export excludes the
    entire tree (no rows).
    """
    parent = _make_node(alice, content="parent body")
    child = _make_node(alice, parent=parent, content="child body")
    parent.deleted_at = datetime.utcnow()
    child.deleted_at = datetime.utcnow()
    _db.session.commit()

    # Add an alive thread separately so build_user_export_content has
    # something to return — otherwise it returns None for "no nodes".
    other = _make_node(alice, content="other thread")

    from backend.routes.export_data import build_user_export_content
    content = build_user_export_content(alice)
    assert content is not None
    assert "other thread" in content
    # Fully-deleted thread should be entirely absent.
    assert "parent body" not in content
    assert "child body" not in content
    # And no tombstone placeholder should leak through either — the
    # whole thread is gone.
    assert "[Node deleted by author]" not in content


# ── 16. Inline quote rendering distinguishes deleted vs inaccessible ────

def test_quote_resolver_deleted_vs_inaccessible(app, alice, bob):
    target = _make_node(alice, content="target body")
    # Public so bob has pre-deletion access.
    target.privacy_level = "public"
    _db.session.commit()
    target_id = target.id

    from backend.utils.quotes import get_quote_data, resolve_quotes

    # Alive: returns content.
    data = get_quote_data([target_id], bob.id)
    assert data[target_id]["content"] == "target body"

    # Soft-deleted with pre-access: returns deleted-True payload, no content.
    target.deleted_at = datetime.utcnow()
    _db.session.commit()
    data = get_quote_data([target_id], bob.id)
    assert data[target_id]["deleted"] is True
    assert data[target_id]["content"] is None

    # Resolver renders distinct strings.
    text = f"prefix {{quote:{target_id}}} suffix"
    rendered, _ = resolve_quotes(text, bob.id, for_llm=True)
    assert "deleted by author" in rendered

    # Privacy-blocked target: bob has no pre-access (private).
    target.privacy_level = "private"
    _db.session.commit()
    data = get_quote_data([target_id], bob.id)
    assert data[target_id] is None
    rendered, _ = resolve_quotes(text, bob.id, for_llm=True)
    assert "inaccessible" in rendered
