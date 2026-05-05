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


# ── 3b. Walk past other-user nodes into nested user-owned descendants ──

def test_delete_descendants_walks_past_other_user_into_my_replies(app, alice, bob):
    """Chain Me → Other → Me: the deepest "Me" must also be soft-deleted.

    Earlier the walker `continue`d on other-user nodes, which both
    skipped the deleted_at assignment AND skipped the descendant
    enumeration — so the deepest "Me" silently stayed alive. The user
    promised "delete this node and all my replies" expects the entire
    user-owned subtree, not just up to the first foreign reply.
    """
    a_root = _make_node(alice)
    b_reply = _make_node(bob, parent=a_root)
    a_nested = _make_node(alice, parent=b_reply)
    client = app.test_client()
    _login(client, alice)
    resp = client.delete(
        f"/nodes/{a_root.id}?delete_descendants=true",
    )
    assert resp.status_code == 200
    # a_root + a_nested = 2 owned nodes; b_reply preserved.
    assert resp.json["scheduled"] == 2
    assert Node.query.get(a_root.id).deleted_at is not None
    assert Node.query.get(b_reply.id).deleted_at is None  # preserved
    assert Node.query.get(a_nested.id).deleted_at is not None  # also deleted


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


# ── 4. Cross-user descendant cascade across cleanup runs ────────────────

def test_cleanup_cascade_processes_tree_leaf_first(app, alice):
    """Leaf-first cascade: a parent's row stays as long as its (deleted)
    children's rows still exist. Once a child purges, the parent
    becomes eligible the next time the cleanup logic looks at it.

    Whether this happens within one run or across multiple runs
    depends on iteration order under the cleanup task's `yield_per`
    stream — both are correct behavior. The test verifies the
    invariant: a parent NEVER purges before all its children's rows
    are gone.
    """
    parent = _make_node(alice, content="parent")
    child = _make_node(alice, parent=parent, content="child")
    parent.deleted_at = datetime.utcnow() - timedelta(days=31)
    child.deleted_at = datetime.utcnow() - timedelta(days=31)
    _db.session.commit()
    pid, cid = parent.id, child.id

    from backend.tasks.node_cleanup import (
        _full_purge, _wipe_content_and_versions,
    )

    # Adversarial ordering: parent comes BEFORE child. Parent must NOT
    # purge in this iteration because the child's row still exists.
    p = Node.query.get(pid)
    cc = Node.query.filter_by(parent_id=p.id).count()
    assert cc == 1  # invariant precondition
    if cc == 0:
        _full_purge(p)
    elif p.content is not None:
        _wipe_content_and_versions(p)
    _db.session.commit()
    assert Node.query.get(pid) is not None  # parent kept
    assert Node.query.get(pid).content is None  # content wiped

    # Now process the child — purge.
    c = Node.query.get(cid)
    if Node.query.filter_by(parent_id=c.id).count() == 0:
        _full_purge(c)
    _db.session.commit()
    assert Node.query.get(cid) is None

    # Next pass touches the parent again. It now has zero children →
    # eligible for full purge.
    p = Node.query.get(pid)
    if Node.query.filter_by(parent_id=p.id).count() == 0:
        _full_purge(p)
    _db.session.commit()
    assert Node.query.get(pid) is None  # row gone after cascade


# ── 5b. LLM placeholder factory blocked on soft-deleted parent ─────────

def test_llm_placeholder_factory_blocked_on_soft_deleted_parent(app, alice):
    """The LLM placeholder factory at backend/utils/llm_nodes.py:67+
    uses with_for_update() on the parent and raises ParentDeletedError
    if the parent has deleted_at set. This is the second create-side
    code path that needs the Race A guard.
    """
    parent = _make_node(alice)
    parent.deleted_at = datetime.utcnow()
    _db.session.commit()

    from backend.utils.llm_nodes import create_llm_placeholder
    from backend.utils.node_deletion import ParentDeletedError

    raised = False
    try:
        create_llm_placeholder(
            parent.id, "claude-opus-4.6", alice.id, enqueue=False,
        )
    except ParentDeletedError:
        raised = True
    assert raised
    # No placeholder LLM child was created.
    assert Node.query.filter_by(parent_id=parent.id).count() == 0


# ── 17. Drafts referencing soft-deleted nodes ──────────────────────────

def test_drafts_filter_with_soft_deleted_targets(app, alice):
    """Plan §17:
    - Draft.node_id deleted: omit (return 404 from get_draft)
    - Draft.parent_id deleted: keep, null parent_id, surface warning
    """
    from backend.models import Draft
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/drafts")
    client = app.test_client()
    _login(client, alice)

    # Setup: alive node, soft-deleted node, a draft against each as
    # parent_id, plus a draft against the deleted node as node_id.
    alive = _make_node(alice)
    dead = _make_node(alice)
    dead.deleted_at = datetime.utcnow()
    _db.session.commit()

    parent_draft = Draft(user_id=alice.id, parent_id=dead.id)
    parent_draft.set_content("draft body")
    _db.session.add(parent_draft)

    edit_draft = Draft(user_id=alice.id, node_id=dead.id)
    edit_draft.set_content("edit body")
    _db.session.add(edit_draft)
    _db.session.commit()

    # node_id branch: 404 (treat as gone).
    r = client.get(f"/drafts/?node_id={dead.id}")
    assert r.status_code == 404

    # parent_id branch: surface the saved content with parent_id null
    # and a warning flag.
    r = client.get(f"/drafts/?parent_id={dead.id}")
    assert r.status_code == 200
    assert r.json["parent_id"] is None
    assert r.json.get("parent_deleted") is True
    assert r.json["content"] == "draft body"


# ── 19. AI-rooted thread soft-deleted via human_owner_id ───────────────

def test_human_owner_can_delete_llm_rooted_thread(app, alice):
    """A system-prompt root may be owned by an LLM pseudo-user but have
    human_owner_id set to a real user. That user can soft-delete it
    via can_user_edit_node, even though they aren't the literal owner.
    """
    # Manufacture an "LLM" pseudo-user as the owner; alice is human_owner.
    llm_user = User(username="claude-test", twitter_id="llm-test")
    _db.session.add(llm_user)
    _db.session.commit()

    root = Node(
        user_id=llm_user.id, human_owner_id=alice.id,
        node_type="llm", llm_model="claude-test",
        privacy_level="private", ai_usage="none", token_count=1,
    )
    root.set_content("system prompt body")
    _db.session.add(root)
    _db.session.commit()

    client = app.test_client()
    _login(client, alice)
    resp = client.delete(f"/nodes/{root.id}")
    assert resp.status_code == 200
    assert Node.query.get(root.id).deleted_at is not None


# ── 20, 21. Feed display-swap rules with soft-deletion ─────────────────

def test_feed_skips_deleted_first_child_of_system_prompt_root(app, alice):
    """§4a Case 1: when the thread root is a system prompt and the
    first child is soft-deleted, the Log card preview falls through to
    the next live child rather than rendering as [Node deleted].

    Implemented in feed.py via filter(deleted_at IS NULL) on the
    first_child query.
    """
    from backend.models import (
        UserPrompt, NodeContextArtifact,
    )
    from backend.routes.feed import feed_bp
    app.register_blueprint(feed_bp, url_prefix="/api")

    # Build a system-prompt root + 2 children; soft-delete the first.
    prompt = UserPrompt(
        user_id=alice.id, prompt_key="default", title="t",
    )
    prompt.set_content("ignore me")
    _db.session.add(prompt)
    _db.session.commit()
    root = _make_node(alice)
    artifact = NodeContextArtifact(
        node_id=root.id, artifact_type="prompt", artifact_id=prompt.id,
    )
    _db.session.add(artifact)
    first = _make_node(alice, parent=root, content="first child")
    second = _make_node(alice, parent=root, content="second child body")
    first.deleted_at = datetime.utcnow()
    _db.session.commit()

    client = app.test_client()
    _login(client, alice)
    r = client.get("/api/feed")
    assert r.status_code == 200
    cards = [c for c in r.json["nodes"] if c["thread_root_id"] == root.id]
    assert len(cards) == 1
    # Falls through to second (alive) child, not the deleted first.
    assert cards[0]["id"] == second.id


def test_feed_surfaces_deleted_root_with_alive_descendants(app, alice, bob):
    """§4a Case 2: a soft-deleted thread root whose subtree still has
    an alive accessible descendant must still surface in Log so the
    descendants are reachable.
    """
    from backend.routes.feed import feed_bp
    app.register_blueprint(feed_bp, url_prefix="/api")

    root = _make_node(alice, content="root body")
    # bob's reply is alive and visible to alice via human_owner /
    # owner check. Force public so accessible_nodes_filter passes.
    root.privacy_level = "public"
    bob_reply = _make_node(bob, parent=root, content="bob reply")
    bob_reply.privacy_level = "public"
    root.deleted_at = datetime.utcnow()
    _db.session.commit()

    client = app.test_client()
    _login(client, alice)
    r = client.get("/api/feed")
    assert r.status_code == 200
    cards = [c for c in r.json["nodes"] if c["thread_root_id"] == root.id]
    # Root is deleted but its subtree has an alive reply → the thread
    # surfaces via §4a Case 2.
    assert len(cards) == 1
    # thread_root_id stays the actual (deleted) root so the kebab
    # targets it for further deletion; the display preview swap to a
    # live descendant happens in production (Postgres) but `newest_map`
    # uses DISTINCT ON which SQLite silently ignores. We only assert
    # the routing invariant here; the swap is exercised manually on
    # staging.
    assert cards[0]["thread_root_id"] == root.id


# ── 22. recent-context token counter excludes soft-deleted ─────────────

def test_recent_context_token_counter_excludes_deleted(app, alice):
    """Soft-deleted nodes' token_count must not contribute to the
    recent-context summarization threshold — otherwise a user who
    soft-deleted a 10k-token wall of text would keep tripping the
    "regenerate summary" trigger on static data.
    """
    alive = _make_node(alice, content="alive")
    alive.token_count = 100
    alive.ai_usage = "chat"
    deleted = _make_node(alice, content="dead")
    deleted.token_count = 100_000  # would dominate without the filter
    deleted.ai_usage = "chat"
    deleted.deleted_at = datetime.utcnow()
    _db.session.commit()

    from backend.tasks.recent_context import _count_total_eligible_tokens
    total = _count_total_eligible_tokens(alice.id)
    # Only the alive node contributes; the soft-deleted one is excluded
    # despite its huge token_count.
    assert total == 100


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
