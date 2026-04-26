"""Tests for the incremental export path in build_user_export_content.

Layer 2: when `created_after` is passed, the export uses an anchor-based
selection that includes:
- target's own/addressed nodes that are post-cutoff,
- accessible foreign post-cutoff ancestors (climb-up),
- accessible post-cutoff descendants (climb-down),

and renders entry points (in-scope nodes whose parent is not in scope)
with a short preamble when the entry point sits beneath an out-of-scope
parent.
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

# Mock heavy deps that aren't needed for export logic
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node            # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


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
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


# ── helpers ─────────────────────────────────────────────────────────────

def _make_user(username, **kwargs):
    u = User(username=username, approved=True, plan="alpha", **kwargs)
    _db.session.add(u)
    _db.session.flush()
    return u


def _make_node(user, parent_id=None, content="hello", node_type="user",
               privacy_level="private", ai_usage="chat", human_owner=None,
               llm_model=None, created_at=None, token_count=None):
    n = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent_id,
        node_type=node_type,
        llm_model=llm_model,
        privacy_level=privacy_level,
        ai_usage=ai_usage,
    )
    n.set_content(content)
    if token_count is not None:
        n.token_count = token_count
    if created_at is not None:
        n.created_at = created_at
    _db.session.add(n)
    _db.session.flush()
    return n


# Convenient datetime constants for fixtures
DEC_15 = datetime(2025, 12, 15, 10, 0, 0)
APR_07 = datetime(2026, 4, 7, 0, 0, 0)   # cutoff
APR_18 = datetime(2026, 4, 18, 14, 30, 0)
APR_19 = datetime(2026, 4, 19, 11, 0, 0)
APR_20 = datetime(2026, 4, 20, 9, 0, 0)
APR_22 = datetime(2026, 4, 22, 12, 0, 0)


def _build(user, **kwargs):
    """Import build_user_export_content lazily to honor the per-test
    module-mocking dance done by the fixture."""
    from backend.routes.export_data import build_user_export_content
    return build_user_export_content(user, **kwargs)


# ── 1. pre-cutoff top-level + post-cutoff reply ─────────────────────────

class TestPreCutoffTopLevelWithPostCutoffReply:
    def test_post_cutoff_reply_appears_with_preamble(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(
            alice, content="dec discussion start",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        reply = _make_node(
            alice, parent_id=root.id, content="april reply content",
            ai_usage="chat", token_count=200, created_at=APR_18,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        assert "april reply content" in content
        assert "dec discussion start" not in content
        # Pin preamble text exactly (per plan):
        assert "Continuation of thread started 2025-12-15" in content
        # latest_node_created_at reflects the new reply
        assert result["latest_node_created_at"] == reply.created_at
        assert reply.id in result["node_ids"]
        assert root.id not in result["node_ids"]


# ── 2. pre-cutoff top-level, no post-cutoff descendants ─────────────────

class TestPreCutoffTopLevelOnly:
    def test_thread_not_in_output(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        _make_node(
            alice, content="old thread content",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        _db.session.commit()

        result = _build(alice, filter_ai_usage=True, created_after=APR_07)
        assert result is None  # no post-cutoff anchors → empty export


# ── 3. post-cutoff top-level, only post-cutoff nodes ────────────────────

class TestPostCutoffTopLevelOnly:
    def test_full_thread_renders_no_preamble(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(
            alice, content="brand new thread",
            ai_usage="chat", token_count=100, created_at=APR_18,
        )
        child = _make_node(
            alice, parent_id=root.id, content="reply",
            ai_usage="chat", token_count=50, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        assert "brand new thread" in content
        assert "reply" in content
        assert "Continuation of thread" not in content
        assert {root.id, child.id}.issubset(result["node_ids"])


# ── 4. mixed: pre-cutoff thread + post-cutoff thread ────────────────────

class TestMixed:
    def test_both_appear_correctly(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        old_root = _make_node(
            alice, content="old root",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        old_reply = _make_node(
            alice, parent_id=old_root.id, content="old-thread april reply",
            ai_usage="chat", token_count=80, created_at=APR_18,
        )
        new_root = _make_node(
            alice, content="brand new april thread",
            ai_usage="chat", token_count=120, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        content = result["content"]

        # Both post-cutoff entries appear
        assert "old-thread april reply" in content
        assert "brand new april thread" in content
        # Pre-cutoff root content suppressed
        assert "old root" not in content
        # Exactly one preamble (for the old-thread continuation)
        assert content.count("Continuation of thread started") == 1
        # node_ids has both post-cutoff nodes; not the pre-cutoff root
        assert {old_reply.id, new_root.id}.issubset(result["node_ids"])
        assert old_root.id not in result["node_ids"]


# ── 5. multiple entry points in same thread ─────────────────────────────

class TestMultipleEntryPoints:
    def test_two_branches_two_preambles(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(
            alice, content="dec root",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        # Two post-cutoff branches directly under the pre-cutoff root.
        branch1 = _make_node(
            alice, parent_id=root.id, content="branch one new reply",
            ai_usage="chat", token_count=50, created_at=APR_18,
        )
        branch2 = _make_node(
            alice, parent_id=root.id, content="branch two new reply",
            ai_usage="chat", token_count=50, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        content = result["content"]

        assert "branch one new reply" in content
        assert "branch two new reply" in content
        # One preamble per entry point (cosmetic; flagged in plan as
        # follow-up to dedupe by shared root, but functionally correct).
        assert content.count("Continuation of thread started") == 2
        assert {branch1.id, branch2.id}.issubset(result["node_ids"])


# ── 6. deep post-cutoff chain in old thread ─────────────────────────────

class TestDeepPostCutoffChain:
    def test_deep_chain_renders_below_entry(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(
            alice, content="ancient root",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        n1 = _make_node(
            alice, parent_id=root.id, content="layer one",
            ai_usage="chat", token_count=50, created_at=APR_18,
        )
        n2 = _make_node(
            alice, parent_id=n1.id, content="layer two",
            ai_usage="chat", token_count=50,
            created_at=APR_18 + timedelta(hours=1),
        )
        n3 = _make_node(
            alice, parent_id=n2.id, content="layer three",
            ai_usage="chat", token_count=50,
            created_at=APR_18 + timedelta(hours=2),
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        content = result["content"]

        assert "ancient root" not in content
        for txt in ("layer one", "layer two", "layer three"):
            assert txt in content
        # Only ONE preamble — n1 is the entry point; n2/n3 are descendants.
        assert content.count("Continuation of thread started") == 1
        assert {n1.id, n2.id, n3.id}.issubset(result["node_ids"])
        assert root.id not in result["node_ids"]


# ── 7. foreign post-cutoff ancestor pulled in by climb-up ───────────────

class TestForeignAncestor:
    def test_foreign_public_ancestor_included(self, app):
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        # Bob owns a foreign thread. Pre-cutoff root + post-cutoff public
        # reply by Bob. Alice replies post-cutoff beneath Bob's reply.
        bob_root = _make_node(
            bob, content="bob's old thread root",
            privacy_level="public", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_reply = _make_node(
            bob, parent_id=bob_root.id,
            content="bob's april public reply",
            privacy_level="public", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        alice_reply = _make_node(
            alice, parent_id=bob_reply.id, content="alice's april reply",
            privacy_level="public", ai_usage="chat",
            token_count=60, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        # Foreign post-cutoff ancestor (climbed up via filters) appears
        assert "bob's april public reply" in content
        # Alice's reply also appears (descendant of climbed-up ancestor)
        assert "alice's april reply" in content
        # Bob's pre-cutoff root content does NOT appear
        assert "bob's old thread root" not in content
        # Preamble appears (Bob's reply's parent is pre-cutoff)
        assert "Continuation of thread started" in content
        # Both post-cutoff nodes in node_ids
        assert {bob_reply.id, alice_reply.id}.issubset(result["node_ids"])


# ── 8. foreign sibling exclusion ────────────────────────────────────────

class TestForeignSiblingExclusion:
    def test_foreign_sibling_not_included(self, app):
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        # Pre-cutoff thread root (Alice's). Bob's post-cutoff public
        # reply directly under root. Alice's separate post-cutoff reply
        # also directly under root. Bob's reply is a sibling of Alice's
        # reply, NOT an ancestor or descendant. Should be excluded.
        root = _make_node(
            alice, content="alice's pre-cutoff root",
            privacy_level="public", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_sibling = _make_node(
            bob, parent_id=root.id,
            content="bob's unrelated public reply",
            privacy_level="public", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        alice_reply = _make_node(
            alice, parent_id=root.id, content="alice's own april reply",
            privacy_level="public", ai_usage="chat",
            token_count=60, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        content = result["content"]

        assert "alice's own april reply" in content
        # Foreign sibling is NOT included
        assert "bob's unrelated public reply" not in content
        assert bob_sibling.id not in result["node_ids"]
        assert alice_reply.id in result["node_ids"]


# ── 9. max_tokens budgeted path ─────────────────────────────────────────

class TestBudgetedPath:
    def test_post_cutoff_reply_with_max_tokens(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        _make_node(
            alice, content="dec root",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        # Build root we can reference
        from backend.models import Node as _N
        root = _N.query.filter_by(user_id=alice.id, parent_id=None).first()

        reply = _make_node(
            alice, parent_id=root.id, content="post-cutoff reply",
            ai_usage="chat", token_count=200, created_at=APR_18,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            max_tokens=5000, return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        assert "post-cutoff reply" in content
        assert "dec root" not in content
        assert "Continuation of thread started" in content
        assert reply.id in result["node_ids"]


# ── 10. quote from pre-cutoff node ──────────────────────────────────────
# Simplified: the resolver's embed-pre-cutoff behavior is exercised by
# the end-to-end content; the precise embed mechanism is covered in
# test_quotes.py. Here we just assert the negative: a quoted-by-id
# pre-cutoff node does not become an entry point with a misleading
# preamble.

class TestQuotedPreCutoffNotEntryPoint:
    def test_quoted_node_not_an_entry_point(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        old = _make_node(
            alice, content="old quoted content",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        # New post-cutoff entry that quotes the old node by ID.
        _make_node(
            alice,
            content=f"new reply that quotes {{quote:{old.id}}} the old one",
            ai_usage="chat", token_count=80, created_at=APR_18,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            max_tokens=5000, return_metadata=True,
        )
        content = result["content"]

        # The new entry appears with a single preamble (it's a new
        # top-level so actually NO preamble — its parent_id is None).
        assert "new reply that quotes" in content
        # Old quoted node is NOT an entry point — there is no SECOND
        # preamble for it.
        # (The content text might appear if the resolver embeds it,
        # which is fine — what matters is that the OLD node never
        # gets its own "Thread N" entry-point header.)
        # Count how many "# Thread N" headers we have: should be 1.
        thread_headers = [
            line for line in content.split("\n")
            if line.startswith("# Thread ")
        ]
        assert len(thread_headers) == 1
        # And the old node is not in node_ids (it's pre-cutoff).
        assert old.id not in result["node_ids"]


# ── 11. budget-ejected post-cutoff parent ───────────────────────────────

class TestBudgetEjectedParent:
    def test_anchor_renders_when_parent_ejected_by_budget(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(
            alice, content="dec root",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        # Two post-cutoff anchors, parent → child. Budget chosen so
        # that the parent's tokens push us over and only the child fits.
        # Using chronological_order=True so oldest (parent) is selected
        # first; in the windowing helper the older one fits first, then
        # the next would overflow. Want behavior: parent ejected, child
        # included alone.
        # Easier: chronological_order=False (newest first) with budget
        # that fits only one node. The newest (child) is selected, the
        # parent is ejected.
        post_parent = _make_node(
            alice, parent_id=root.id, content="post-cutoff parent",
            ai_usage="chat", token_count=900, created_at=APR_18,
        )
        post_child = _make_node(
            alice, parent_id=post_parent.id,
            content="post-cutoff child",
            ai_usage="chat", token_count=200, created_at=APR_19,
        )
        _db.session.commit()

        # Budget: 600 tokens. With chronological_order=False (default),
        # newest first → child selected first (200 ≤ budget), parent
        # would push us over (200 + 900 > 600), so parent is ejected.
        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            max_tokens=600, return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        assert "post-cutoff child" in content
        assert "post-cutoff parent" not in content
        # Child becomes its own entry point with preamble (parent
        # ejected from resolver.included_ids, so child's parent
        # check fails).
        assert "Continuation of thread started" in content
        assert post_child.id in result["node_ids"]
        # CTE rows still see parent — node_ids reflects CTE rows
        assert post_parent.id in result["node_ids"]


# ── 12. private foreign ancestor exclusion ──────────────────────────────

class TestPrivateForeignAncestorExclusion:
    def test_private_ancestor_blocks_climb(self, app):
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        # Bob's PRIVATE post-cutoff thread. Alice replies inside it.
        # Alice's reply is accessible to her (own node). Bob's parent is
        # private — accessible_nodes_filter excludes it from climb-up,
        # so Alice's reply becomes the entry point.
        bob_private_root = _make_node(
            bob, content="bob's private root",
            privacy_level="private", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_private_reply = _make_node(
            bob, parent_id=bob_private_root.id,
            content="bob's private reply",
            privacy_level="private", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        alice_reply = _make_node(
            alice, parent_id=bob_private_reply.id,
            content="alice's reply in private thread",
            privacy_level="private", ai_usage="chat",
            token_count=60, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        content = result["content"]

        assert "alice's reply in private thread" in content
        # Bob's private content NOT included (accessible_nodes_filter)
        assert "bob's private reply" not in content
        assert "bob's private root" not in content
        # Preamble appears for alice's entry point — but the date of
        # Bob's private root must NOT leak (it's metadata about content
        # Alice can't see). Generic preamble only.
        assert "Continuation of thread" in content
        assert "2025-12-15" not in content
        assert alice_reply.id in result["node_ids"]
        assert bob_private_root.id not in result["node_ids"]
        assert bob_private_reply.id not in result["node_ids"]


# ── 13. node-level parity with _count_new_tokens ────────────────────────

class TestParityWithCountNewTokens:
    def test_count_new_tokens_subset_of_export_node_ids(self, app):
        from backend.tasks.recent_context import _count_new_tokens

        alice = _make_user("alice")
        _db.session.commit()

        # Mix: pre-cutoff root, post-cutoff reply (anchor), LLM
        # placeholder addressed to alice.
        root = _make_node(
            alice, content="old root",
            ai_usage="chat", token_count=100, created_at=DEC_15,
        )
        anchor_reply = _make_node(
            alice, parent_id=root.id, content="april reply",
            ai_usage="chat", token_count=80, created_at=APR_18,
        )
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        llm_reply = _make_node(
            llm_user, parent_id=anchor_reply.id,
            content="llm answer", node_type="llm", llm_model="gpt-5",
            human_owner=alice, ai_usage="chat",
            token_count=120, created_at=APR_19,
        )
        _db.session.commit()

        cutoff = APR_07
        counted_total = _count_new_tokens(alice.id, cutoff)
        assert counted_total > 0

        # Build the set of node IDs _count_new_tokens summed.
        from sqlalchemy import or_ as _or
        from backend.utils.privacy import AI_ALLOWED
        counted_rows = _db.session.query(Node.id).filter(
            _or(Node.user_id == alice.id,
                Node.human_owner_id == alice.id),
            Node.created_at > cutoff,
            Node.ai_usage.in_(AI_ALLOWED),
        ).all()
        counted_ids = {r.id for r in counted_rows}

        result = _build(
            alice, filter_ai_usage=True, created_after=cutoff,
            return_metadata=True,
        )
        node_ids = result["node_ids"]

        # Layer 2 invariant: every node _count_new_tokens saw is rendered.
        assert counted_ids.issubset(node_ids), (
            f"counted but not in export: {counted_ids - node_ids}"
        )
        # Sanity: anchor and llm reply in both sets.
        assert {anchor_reply.id, llm_reply.id}.issubset(counted_ids)
        assert {anchor_reply.id, llm_reply.id}.issubset(node_ids)
        # Pre-cutoff root is in neither.
        assert root.id not in counted_ids
        assert root.id not in node_ids


# ── 14. full-archive regression (no created_after) ──────────────────────

class TestFullArchiveRegression:
    def test_legacy_path_unchanged(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        roots = []
        for i in range(3):
            r = _make_node(
                alice, content=f"thread {i} marker text",
                ai_usage="chat", token_count=100,
                created_at=DEC_15 + timedelta(days=i),
            )
            roots.append(r)
        _db.session.commit()

        result = _build(alice, filter_ai_usage=True)  # no created_after
        assert result is not None
        # Three top-level threads → three "# Thread N" headers
        thread_headers = [
            line for line in result.split("\n")
            if line.startswith("# Thread ")
        ]
        assert len(thread_headers) == 3
        # Distinctive snippet from each thread present
        for i in range(3):
            assert f"thread {i} marker text" in result
        # Legacy path doesn't emit the Layer 2 preamble
        assert "Continuation of thread" not in result


# ── 15. engaged_threads strategy (created_after=None, anchor-based) ─────

class TestEngagedThreadsStrategy:
    def test_user_owned_tree_fully_covered_when_only_root_anchor(self, app):
        """Alice owns a root; Bob posts an accessible public reply; Alice
        never engages further. With engaged_threads + no cutoff, climb-down
        from Alice's root anchor must still include Bob's reply (parity
        with authored_threads on user-owned subtrees)."""
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        root = _make_node(
            alice, content="alice owned root marker",
            privacy_level="public", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_reply = _make_node(
            bob, parent_id=root.id,
            content="bob foreign public reply marker",
            privacy_level="public", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True,
            include_strategy="engaged_threads",
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        assert "alice owned root marker" in content
        assert "bob foreign public reply marker" in content
        assert {root.id, bob_reply.id}.issubset(result["node_ids"])

    def test_foreign_public_thread_included_when_user_replied_no_cutoff(self, app):
        """Bob's public thread; Alice replied. Without a cutoff, Alice's
        reply is an anchor; climb-up must reach Bob's accessible public
        root."""
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        bob_root = _make_node(
            bob, content="bob public root marker",
            privacy_level="public", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_reply = _make_node(
            bob, parent_id=bob_root.id,
            content="bob public reply marker",
            privacy_level="public", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        alice_reply = _make_node(
            alice, parent_id=bob_reply.id,
            content="alice reply marker",
            privacy_level="public", ai_usage="chat",
            token_count=60, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True,
            include_strategy="engaged_threads",
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]

        assert "bob public root marker" in content
        assert "bob public reply marker" in content
        assert "alice reply marker" in content
        assert {bob_root.id, bob_reply.id, alice_reply.id}.issubset(
            result["node_ids"]
        )

    def test_private_foreign_ancestor_blocks_climb_no_cutoff(self, app):
        """Bob's private root; Alice replies inside. Climb-up must stop
        at the private parent — content & date must not leak."""
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        bob_private_root = _make_node(
            bob, content="bob private root secret",
            privacy_level="private", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_private_reply = _make_node(
            bob, parent_id=bob_private_root.id,
            content="bob private reply secret",
            privacy_level="private", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        alice_reply = _make_node(
            alice, parent_id=bob_private_reply.id,
            content="alice in private marker",
            privacy_level="private", ai_usage="chat",
            token_count=60, created_at=APR_19,
        )
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True,
            include_strategy="engaged_threads",
            return_metadata=True,
        )
        content = result["content"]

        assert "alice in private marker" in content
        assert "bob private root secret" not in content
        assert "bob private reply secret" not in content
        # Bob's private root date must NOT leak
        assert "2025-12-15" not in content
        assert alice_reply.id in result["node_ids"]
        assert bob_private_root.id not in result["node_ids"]
        assert bob_private_reply.id not in result["node_ids"]

    def test_max_tokens_with_engaged_threads_keeps_newest(self, app):
        """With engaged_threads + a small max_tokens budget and
        chronological_order=False, newest anchors win."""
        alice = _make_user("alice")
        _db.session.commit()

        old_root = _make_node(
            alice, content="OLD anchor marker",
            ai_usage="chat", token_count=200, created_at=DEC_15,
        )
        new_root = _make_node(
            alice, content="NEW anchor marker",
            ai_usage="chat", token_count=200,
            created_at=APR_22,
        )
        _db.session.commit()

        # Budget that fits one root but not both (200 tokens each + 100
        # header_footer reserve; budget = 250 → only newest fits).
        result = _build(
            alice, filter_ai_usage=True,
            include_strategy="engaged_threads",
            max_tokens=350,
            chronological_order=False,
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]
        assert "NEW anchor marker" in content
        assert "OLD anchor marker" not in content
        # node_ids reflects the full CTE row set (pre-budget); selected_ids
        # (post-budget) drives rendering. Content is the truth-source for
        # what the budget kept, so no node_ids assertion here.

    def test_invalid_include_strategy_raises(self, app):
        alice = _make_user("alice")
        _db.session.commit()
        with pytest.raises(ValueError):
            _build(alice, include_strategy="bogus")

    def test_authored_threads_default_byte_identical_to_legacy(
        self, app, monkeypatch
    ):
        """Default (no include_strategy arg) must produce byte-identical
        output to explicit include_strategy='authored_threads'. Freeze
        utcnow so the **Export Date:** field doesn't drift between the
        two calls."""
        from backend.routes import export_data as ed

        frozen = datetime(2026, 4, 26, 12, 0, 0)

        class _FrozenDT(datetime):
            @classmethod
            def utcnow(cls):
                return frozen

        monkeypatch.setattr(ed, "datetime", _FrozenDT)

        alice = _make_user("alice")
        _db.session.commit()

        for i in range(2):
            _make_node(
                alice, content=f"thread {i} marker text",
                ai_usage="chat", token_count=100,
                created_at=DEC_15 + timedelta(days=i),
            )
        _db.session.commit()

        default_result = _build(alice, filter_ai_usage=True)
        explicit_result = _build(
            alice, filter_ai_usage=True,
            include_strategy="authored_threads",
        )

        assert default_result == explicit_result

    def test_created_after_overrides_authored_threads(self, app):
        """When created_after is set, the function takes the anchor-based
        incremental path regardless of include_strategy. Verify by using
        a fixture that exercises the foreign-ancestor climb (which only
        the incremental path does)."""
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        bob_root = _make_node(
            bob, content="bob pre-cutoff public root",
            privacy_level="public", ai_usage="chat",
            token_count=100, created_at=DEC_15,
        )
        bob_post = _make_node(
            bob, parent_id=bob_root.id,
            content="bob april public reply marker",
            privacy_level="public", ai_usage="chat",
            token_count=80, created_at=APR_18,
        )
        alice_post = _make_node(
            alice, parent_id=bob_post.id,
            content="alice april reply marker",
            privacy_level="public", ai_usage="chat",
            token_count=60, created_at=APR_19,
        )
        _db.session.commit()

        # Even with include_strategy='authored_threads', created_after
        # forces the incremental path → climb-up picks up bob_post.
        result = _build(
            alice, filter_ai_usage=True,
            include_strategy="authored_threads",
            created_after=APR_07,
            return_metadata=True,
        )
        assert result is not None
        content = result["content"]
        assert "bob april public reply marker" in content
        assert "alice april reply marker" in content
        assert {bob_post.id, alice_post.id}.issubset(result["node_ids"])
