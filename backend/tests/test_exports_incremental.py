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
               llm_model=None, created_at=None, token_count=None,
               origin=None):
    n = Node(
        origin=origin,
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


# ── 9b. budgeted cursor regression ──────────────────────────────────────
# The chunked profile regen uses `latest_node_created_at` as the resume
# cursor for the next chunk (created_after=cursor). It must therefore
# reflect the newest node the budget actually INCLUDED, not the newest
# node in scope — otherwise the cursor leaps to the present after one
# budgeted chunk and every later chunk is silently skipped.

class TestBudgetCursorDoesNotSkip:
    def test_cursor_stops_at_budget_window_boundary(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        nodes = [
            _make_node(
                alice, content=f"entry {i}",
                ai_usage="chat", token_count=1000,
                created_at=APR_07 + timedelta(days=i + 1),
            )
            for i in range(10)
        ]
        _db.session.commit()

        # Budget fits 3 of the 10 nodes (strict fit: 3000 < 3400,
        # 4000 would overshoot).
        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            max_tokens=3500, chronological_order=True,
            return_metadata=True,
        )
        assert result is not None

        included = [n for n in nodes if n.id in result["node_ids"]]
        assert [n.id for n in included] == [n.id for n in nodes[:3]]
        assert result["node_count"] == 3
        # The cursor: newest INCLUDED node, not newest in scope.
        assert result["latest_node_created_at"] == nodes[2].created_at
        assert result["earliest_node_created_at"] == nodes[0].created_at

        # Resuming from the cursor picks up exactly the next window —
        # nothing skipped, nothing repeated.
        result2 = _build(
            alice, filter_ai_usage=True,
            created_after=result["latest_node_created_at"],
            max_tokens=3500, chronological_order=True,
            return_metadata=True,
        )
        included2 = [n for n in nodes if n.id in result2["node_ids"]]
        assert [n.id for n in included2] == [n.id for n in nodes[3:6]]
        assert result2["latest_node_created_at"] == nodes[5].created_at

    def test_unbudgeted_metadata_still_covers_full_scope(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        nodes = [
            _make_node(
                alice, content=f"entry {i}",
                ai_usage="chat", token_count=1000,
                created_at=APR_07 + timedelta(days=i + 1),
            )
            for i in range(3)
        ]
        _db.session.commit()

        result = _build(
            alice, filter_ai_usage=True, created_after=APR_07,
            return_metadata=True,
        )
        assert result["node_count"] == 3
        assert result["latest_node_created_at"] == nodes[-1].created_at
        assert {n.id for n in nodes} == result["node_ids"]


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
        # node_ids reflects the budget-selected window, so the ejected
        # parent — which was NOT rendered — is excluded. (It used to
        # reflect the full CTE scope, which broke the chunked-regen
        # resume cursor; see TestBudgetCursorDoesNotSkip.)
        assert post_parent.id not in result["node_ids"]


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


# ── 16. _collect_all_nodes_in_tree: deep chain (recursion regression) ────

class TestCollectAllNodesDeepChain:
    def test_deep_chain_does_not_overflow(self, app):
        """A reply chain far deeper than Python's recursion limit must be
        collected without RecursionError — the bug that crashed full
        profile regen for the deepest-thread user before any LLM call."""
        from backend.routes.export_data import _collect_all_nodes_in_tree

        alice = _make_user("alice")
        _db.session.commit()

        depth = 1500  # comfortably past the default recursion limit (1000)
        parent_id = None
        for i in range(depth):
            n = _make_node(
                alice, parent_id=parent_id, content=f"link {i}",
                ai_usage="chat", token_count=1,
                created_at=DEC_15 + timedelta(seconds=i),
            )
            parent_id = n.id
        _db.session.commit()

        root = Node.query.filter_by(
            user_id=alice.id, parent_id=None
        ).first()
        collected = _collect_all_nodes_in_tree(root, filter_ai_usage=True)
        assert len(collected) == depth

    def test_preorder_parent_before_descendants(self, app):
        """Pre-order preserved: a parent precedes all its descendants, and
        a child's whole subtree precedes the next sibling — same ordering
        the recursive implementation produced."""
        from backend.routes.export_data import _collect_all_nodes_in_tree

        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(alice, content="root", ai_usage="chat",
                          token_count=1, created_at=DEC_15)
        a = _make_node(alice, parent_id=root.id, content="A",
                       ai_usage="chat", token_count=1,
                       created_at=DEC_15 + timedelta(hours=1))
        b = _make_node(alice, parent_id=root.id, content="B",
                       ai_usage="chat", token_count=1,
                       created_at=DEC_15 + timedelta(hours=2))
        a1 = _make_node(alice, parent_id=a.id, content="A1",
                        ai_usage="chat", token_count=1,
                        created_at=DEC_15 + timedelta(hours=3))
        _db.session.commit()

        order = [n.id for n in
                 _collect_all_nodes_in_tree(root, filter_ai_usage=True)]
        assert set(order) == {root.id, a.id, b.id, a1.id}
        # root before A before A1; A's subtree (incl. A1) before sibling B
        assert order.index(root.id) < order.index(a.id) < order.index(a1.id)
        assert order.index(a1.id) < order.index(b.id)


# ── 17. format_node_tree: deep chain (renderer recursion regression) ─────

class TestFormatNodeTreeDeepChain:
    def _deep_chain(self, alice, depth):
        parent_id = None
        for i in range(depth):
            n = _make_node(
                alice, parent_id=parent_id, content=f"link {i}",
                ai_usage="chat", token_count=1,
                created_at=DEC_15 + timedelta(seconds=i),
            )
            parent_id = n.id
        _db.session.commit()

    def test_deep_chain_renders_without_overflow(self, app):
        """The renderer had the same one-frame-per-depth recursion the
        collector had before c556e4d, one stage later in the pipeline —
        it crashed unbudgeted exports (and thus from-scratch profile
        regens) on the deepest-thread users."""
        from backend.routes.export_data import format_node_tree

        alice = _make_user("alice")
        _db.session.commit()
        depth = 1500  # comfortably past the default recursion limit (1000)
        self._deep_chain(alice, depth)

        root = Node.query.filter_by(
            user_id=alice.id, parent_id=None
        ).first()
        text = format_node_tree(root, filter_ai_usage=True,
                                user_id=alice.id)
        assert "link 0" in text
        assert f"link {depth - 1}" in text
        assert text.count("link ") == depth

    def test_deep_chain_full_export_paths(self, app):
        """End-to-end: both the unbudgeted legacy export and the
        incremental export survive a deep chain (renderer +
        _subtree_has_alive)."""
        alice = _make_user("alice")
        _db.session.commit()
        depth = 1500
        self._deep_chain(alice, depth)

        legacy = _build(alice, filter_ai_usage=True, return_metadata=True)
        assert legacy["node_count"] == depth

        incremental = _build(
            alice, filter_ai_usage=True, return_metadata=True,
            created_after=DEC_15 - timedelta(days=1),
        )
        assert incremental["node_count"] == depth

    def test_structure_matches_recursive_output(self, app):
        """Branch markers, chronological sibling order, tombstone shells,
        and subtree-before-next-sibling ordering are preserved exactly."""
        from backend.routes.export_data import format_node_tree

        alice = _make_user("alice")
        _db.session.commit()

        root = _make_node(alice, content="ROOT", ai_usage="chat",
                          token_count=1, created_at=DEC_15)
        a = _make_node(alice, parent_id=root.id, content="CHILD-A",
                       ai_usage="chat", token_count=1,
                       created_at=DEC_15 + timedelta(hours=1))
        b = _make_node(alice, parent_id=root.id, content="CHILD-B",
                       ai_usage="chat", token_count=1,
                       created_at=DEC_15 + timedelta(hours=2))
        a1 = _make_node(alice, parent_id=a.id, content="GRANDCHILD-A1",
                        ai_usage="chat", token_count=1,
                        created_at=DEC_15 + timedelta(hours=3))
        b.deleted_at = DEC_15 + timedelta(days=1)
        b1 = _make_node(alice, parent_id=b.id, content="UNDER-TOMBSTONE",
                        ai_usage="chat", token_count=1,
                        created_at=DEC_15 + timedelta(hours=4))
        _db.session.commit()

        text = format_node_tree(root, filter_ai_usage=True,
                                user_id=alice.id)

        # A's subtree before sibling B; B is a tombstone shell whose
        # child still renders; exactly one BRANCH (two siblings).
        i_root = text.index("ROOT")
        i_a = text.index("CHILD-A")
        i_a1 = text.index("GRANDCHILD-A1")
        i_tomb = text.index("[Node deleted by author]")
        i_b1 = text.index("UNDER-TOMBSTONE")
        assert i_root < i_a < i_a1 < i_tomb < i_b1
        assert "CHILD-B" not in text  # deleted content never renders
        assert text.count("---\n**BRANCH**\n---") == 1
        # index paths: A=1.1, A1=1.1.1, B(tombstone)=1.2, B1=1.2.1
        assert "[1.1] " in text and "[1.1.1] " in text
        assert "[1.2] " in text and "[1.2.1] " in text
        assert b1.id is not None  # silence unused warnings


class TestFullExportArtifactContent:
    """Full export surfaces user-artifact refs on any node (not just system
    nodes) AND the artifact content in the preamble (#158 export fix)."""

    def test_artifact_refs_and_content_in_full_export(self, app):
        from backend.models import UserArtifact, NodeContextArtifact
        alice = _make_user("alice")
        _db.session.commit()

        art = UserArtifact(
            user_id=alice.id, kind="reading-list",
            title="Reading List", generated_by="user",
        )
        art.set_content("- Godel Escher Bach")
        _db.session.add(art)
        _db.session.flush()

        # A non-system node pinning the artifact (mirrors an interim
        # retrieval node that read it).
        node = _make_node(
            alice, content="thread body", ai_usage="chat", token_count=100,
        )
        _db.session.add(NodeContextArtifact(
            node_id=node.id, artifact_type="user_artifact",
            artifact_id=art.id,
        ))
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True)

        # Ref emitted on a non-system node (every node is checked).
        assert f"[Artifact 'reading-list' v1 (ref #{art.id})]" in content
        # Full content present in the preamble.
        assert "## Artifacts Referenced" in content
        assert "- Godel Escher Bach" in content

    def test_pinless_node_has_no_artifact_refs(self, app):
        bob = _make_user("bob")
        _db.session.commit()
        _make_node(bob, content="just text", ai_usage="chat", token_count=50)
        _db.session.commit()

        content = _build(bob, filter_ai_usage=True)
        assert "just text" in content
        assert "[Artifact " not in content
        assert "## Artifacts Referenced" not in content


# ── origin: imported nodes are marked, Loore-native ones are not ────────

class TestOrigin:
    def test_header_marks_imports_only_and_metadata_counts_origins(self, app):
        alice = _make_user("alice")
        _db.session.commit()

        _make_node(alice, content="loore entry", token_count=100,
                   created_at=DEC_15)
        # The tweet gets a reply so it renders as a full thread — a
        # childless tweet root would render compactly (no header line).
        tweet = _make_node(alice, content="a tweet", token_count=300,
                           created_at=APR_18, origin="twitter")
        _make_node(alice, parent_id=tweet.id, content="a reply",
                   token_count=50, created_at=APR_19)
        _db.session.commit()

        result = _build(alice, filter_ai_usage=True, return_metadata=True)
        content = result["content"]

        assert "User (alice) via twitter - " in content
        # Loore is the default: never rendered.
        assert "via loore" not in content
        assert content.count(" via ") == 1
        assert result["origin_stats"] == {
            "loore": {"nodes": 2, "tokens": 150},
            "twitter": {"nodes": 1, "tokens": 300},
        }

    def test_source_mix_preamble(self):
        from backend.tasks.exports import (
            source_mix_preamble, chunk_content_for_prompt)
        loore_only = {"content": "x", "origin_stats": {
            "loore": {"nodes": 3, "tokens": 900}}}
        assert source_mix_preamble(loore_only) == ""
        assert chunk_content_for_prompt(loore_only) == "x"
        assert source_mix_preamble({"content": "x"}) == ""

        mixed = {"content": "x", "origin_stats": {
            "loore": {"nodes": 1, "tokens": 100},
            "twitter": {"nodes": 2000, "tokens": 1900}}}
        pre = source_mix_preamble(mixed)
        assert pre.startswith("[Source mix: 95% public tweets")
        assert "2,000 entries" in pre
        assert "5% written in Loore, 1 entries" in pre
        assert chunk_content_for_prompt(mixed) == pre + "x"

    def test_source_mix_is_cumulative_across_updates(self):
        from backend.tasks.exports import (
            source_mix_preamble, merge_origin_stats)
        tweets_base = {"twitter": {"nodes": 38130, "tokens": 900000}}
        loore_chunk = {"content": "x", "origin_stats": {
            "loore": {"nodes": 12, "tokens": 4000}}}
        # A Loore-only chunk on a tweets-built profile MUST still say so.
        pre = source_mix_preamble(loore_chunk, prev_stats=tweets_base)
        assert "existing profile built from: 100% public tweets" in pre
        assert "38,130 entries" in pre
        assert "New data below: 100% written in Loore, 12 entries" in pre
        # Pure-Loore history stays silent.
        assert source_mix_preamble(
            loore_chunk, prev_stats={"loore": {"nodes": 5, "tokens": 9}}) == ""
        assert source_mix_preamble(loore_chunk, prev_stats=None) == ""
        merged = merge_origin_stats(tweets_base, loore_chunk["origin_stats"])
        assert merged == {
            "twitter": {"nodes": 38130, "tokens": 900000},
            "loore": {"nodes": 12, "tokens": 4000}}
        assert tweets_base == {"twitter": {"nodes": 38130, "tokens": 900000}}


def test_export_render_does_not_lazy_load_children_per_node(app):
    """Regression for the 2026-08-27 prod CPU incident: rendering N flat
    threads issued N `SELECT node WHERE parent_id = ?` queries (one lazy
    children load per node, each a seq scan without an index). Children
    are now prefetched level by level; the query count must stay flat in N."""
    from sqlalchemy import event
    from datetime import datetime, timedelta
    from backend.routes.export_data import build_user_export_content
    u = _make_user("flat")
    base = datetime(2026, 8, 1)
    for i in range(60):
        _make_node(u, content=f"tweet {i}", created_at=base + timedelta(minutes=i),
                   token_count=5)
    reply_root = _make_node(u, content="root", created_at=base + timedelta(days=1), token_count=5)
    child = _make_node(u, parent_id=reply_root.id, content="reply", created_at=base + timedelta(days=1, minutes=1), token_count=5)
    _make_node(u, parent_id=child.id, content="reply 2", created_at=base + timedelta(days=1, minutes=2), token_count=5)
    _db.session.commit()
    _db.session.expire_all()

    n_child_queries = [0]

    def after(conn, cursor, statement, params, context, executemany):
        if "FROM node" in statement and "parent_id" in statement and "IN (" not in statement:
            n_child_queries[0] += 1
    event.listen(_db.engine, "after_cursor_execute", after)
    try:
        out = build_user_export_content(
            u, max_tokens=100000, filter_ai_usage=True, chronological_order=True,
            return_metadata=True, include_strategy="engaged_threads")
    finally:
        event.remove(_db.engine, "after_cursor_execute", after)
    assert "tweet 59" in out["content"] and "reply 2" in out["content"]
    assert n_child_queries[0] == 0, n_child_queries[0]


# ── Compact rendering for flat tweet corpora (#276) ──────────────────────

class TestCompactTweetRendering:
    """Runs of childless root nodes with origin="twitter" render as a
    compact `[YYYY-MM-DD HH:MM] text` list instead of one full per-thread
    block per tweet (#276). Threaded/organic content keeps the full
    rendering."""

    def test_flat_tweet_corpus_renders_compact(self, app):
        alice = _make_user("alice")
        _db.session.commit()
        _make_node(alice, content="first tweet", token_count=3,
                   created_at=APR_18, origin="twitter")
        _make_node(alice, content="second tweet", token_count=3,
                   created_at=APR_19, origin="twitter")
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True,
                         include_strategy="engaged_threads")
        assert ("# Tweets by alice (imported from Twitter/X) — 2 tweets"
                in content)
        assert "[2026-04-18 14:30] first tweet" in content
        assert "[2026-04-19 11:00] second tweet" in content
        # No per-thread scaffolding for the flat run.
        assert "# Thread" not in content
        assert "via twitter" not in content
        # Default (unbudgeted engaged) order is newest-first.
        assert content.index("second tweet") < content.index("first tweet")

    def test_tweet_with_reply_keeps_full_rendering(self, app):
        alice = _make_user("alice")
        _db.session.commit()
        root = _make_node(alice, content="engaged tweet", token_count=3,
                          created_at=APR_18, origin="twitter")
        _make_node(alice, parent_id=root.id, content="a loore reply",
                   token_count=3, created_at=APR_19)
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True,
                         include_strategy="engaged_threads")
        assert "# Thread 1" in content
        assert "User (alice) via twitter - " in content
        assert "engaged tweet" in content and "a loore reply" in content
        assert "# Tweets by" not in content

    def test_mixed_corpus_interleaves_runs_and_threads(self, app):
        alice = _make_user("alice")
        _db.session.commit()
        _make_node(alice, content="old tweet", token_count=3,
                   created_at=APR_18, origin="twitter")
        _make_node(alice, content="organic thought", token_count=3,
                   created_at=APR_19)
        _make_node(alice, content="new tweet", token_count=3,
                   created_at=APR_20, origin="twitter")
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True,
                         include_strategy="engaged_threads")
        # Newest-first: run [new tweet], Thread 1 (organic), run [old tweet].
        assert content.count("# Tweets by alice") == 2
        assert content.count("# Thread") == 1
        assert "# Thread 1" in content
        assert (content.index("new tweet") < content.index("organic thought")
                < content.index("old tweet"))

    def test_loore_leaf_root_not_compacted(self, app):
        alice = _make_user("alice")
        _db.session.commit()
        _make_node(alice, content="a lone loore note", token_count=3,
                   created_at=APR_18)
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True,
                         include_strategy="engaged_threads")
        assert "# Thread 1" in content
        assert "# Tweets by" not in content

    def test_budgeted_chronological_window_metadata(self, app):
        """The chunked profile loop's cursor semantics survive compact
        rendering: budget window selects oldest-first, metadata reflects
        the selected rows only."""
        alice = _make_user("alice")
        _db.session.commit()
        for i, ts in enumerate([APR_18, APR_19, APR_20, APR_22]):
            _make_node(alice, content=f"tweet {i}", token_count=100,
                       created_at=ts, origin="twitter")
        _db.session.commit()

        result = _build(alice, max_tokens=350, filter_ai_usage=True,
                        chronological_order=True, return_metadata=True,
                        include_strategy="engaged_threads")
        content = result["content"]
        # Strict-fit window: 350 - 100 header reserve = 250 → two
        # 100-token tweets fit, the third would overshoot.
        assert "[2026-04-18 14:30] tweet 0" in content
        assert "[2026-04-19 11:00] tweet 1" in content
        assert "tweet 2" not in content and "tweet 3" not in content
        assert result["latest_node_created_at"] == APR_19
        assert result["origin_stats"] == {
            "twitter": {"nodes": 2, "tokens": 200},
        }
        # Oldest-first inside the run.
        assert content.index("tweet 0") < content.index("tweet 1")

    def test_legacy_path_renders_flat_tweets_compact(self, app):
        """The legacy authored_threads path (user dump /export/threads,
        estimate route) collapses flat tweet runs the same way (#276)."""
        alice = _make_user("alice")
        _db.session.commit()
        _make_node(alice, content="old tweet", token_count=3,
                   created_at=APR_18, origin="twitter")
        root = _make_node(alice, content="organic root", token_count=3,
                          created_at=APR_19)
        _make_node(alice, parent_id=root.id, content="a reply", token_count=3,
                   created_at=APR_20)
        _make_node(alice, content="new tweet", token_count=3,
                   created_at=APR_22, origin="twitter")
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True)  # legacy path
        # Legacy renders oldest-first: run [old tweet], Thread 1, run [new].
        assert content.count("# Tweets by alice") == 2
        assert content.count("# Thread") == 1
        assert "[2026-04-18 14:30] old tweet" in content
        assert "[2026-04-22 12:00] new tweet" in content
        assert "organic root" in content and "a reply" in content
        assert (content.index("old tweet") < content.index("organic root")
                < content.index("new tweet"))

    def test_legacy_tweet_with_reply_keeps_full_rendering(self, app):
        alice = _make_user("alice")
        _db.session.commit()
        tweet = _make_node(alice, content="engaged tweet", token_count=3,
                           created_at=APR_18, origin="twitter")
        _make_node(alice, parent_id=tweet.id, content="loore reply",
                   token_count=3, created_at=APR_19)
        _db.session.commit()

        content = _build(alice, filter_ai_usage=True)
        assert "# Thread 1" in content
        assert "User (alice) via twitter - " in content
        assert "# Tweets by" not in content


# ── Windowed DEK prefetch (2026-09-02) ───────────────────────────────────

def test_iter_with_dek_prefetch_keeps_order_and_windows_by_subtree(app, monkeypatch):
    from backend.routes import export_data
    from backend.routes.export_data import iter_with_dek_prefetch, prefetch_children
    u = _make_user("w")
    roots = []
    for i in range(5):
        r = _make_node(u, content=f"root {i}", token_count=1)
        for j in range(2):
            _make_node(u, parent_id=r.id, content=f"child {i}.{j}", token_count=1)
        roots.append(r)
    _db.session.commit()
    batches = []
    monkeypatch.setattr(export_data, "prefetch_deks", lambda texts: batches.append(sorted(texts)) or 0)
    prefetch_children(roots)

    out = list(iter_with_dek_prefetch(roots, window=4))
    assert out == roots  # order untouched
    # 3 nodes per root, window closes at >= 4: two roots per window, then one.
    assert [len(b) for b in batches] == [6, 6, 3]
    assert batches[0] == sorted(["root 0", "child 0.0", "child 0.1", "root 1", "child 1.0", "child 1.1"])
    assert batches[2] == sorted(["root 4", "child 4.0", "child 4.1"])


def test_export_render_prefetches_deks_for_every_rendered_node(app, monkeypatch):
    """The export renderer used to decrypt one node at a time (~80 ms of
    KMS latency each on a cold worker). Every rendered node's ciphertext
    must reach the batched prefetch before rendering starts."""
    from datetime import datetime, timedelta
    from backend.routes import export_data
    u = _make_user("pf")
    base = datetime(2026, 8, 1)
    for i in range(20):
        _make_node(u, content=f"tweet {i}", created_at=base + timedelta(minutes=i), token_count=5)
    root = _make_node(u, content="root", created_at=base + timedelta(days=1), token_count=5)
    child = _make_node(u, parent_id=root.id, content="reply", created_at=base + timedelta(days=1, minutes=1), token_count=5)
    _make_node(u, parent_id=child.id, content="reply 2", created_at=base + timedelta(days=1, minutes=2), token_count=5)
    _db.session.commit()
    _db.session.expire_all()
    seen = []
    monkeypatch.setattr(export_data, "prefetch_deks", lambda texts: seen.extend(texts) or 0)

    out = export_data.build_user_export_content(
        u, max_tokens=100000, filter_ai_usage=True, chronological_order=True,
        return_metadata=True, include_strategy="engaged_threads")
    assert "tweet 19" in out["content"] and "reply 2" in out["content"]
    assert set(seen) == {f"tweet {i}" for i in range(20)} | {"root", "reply", "reply 2"}


class TestPlannerWindowMetadata:
    """The chunk planner's contract with the incremental window
    (docs/design/chunk-planner.md): a window never splits a timestamp,
    reports its own stored units, and `count_remaining_units` sums the
    same rows the windows draw from."""

    def test_window_takes_every_row_sharing_the_boundary_timestamp(self, app):
        from backend.routes.export_data import build_user_export_content
        alice = _make_user("alice")
        _db.session.commit()
        ts = APR_07 + timedelta(days=1)
        a = _make_node(alice, content="a", token_count=1000, created_at=ts)
        b = _make_node(alice, content="b", token_count=1000, created_at=ts)
        c = _make_node(alice, content="c", token_count=1000,
                       created_at=ts + timedelta(seconds=1))
        _db.session.commit()

        # Budget for one row: the strict fit alone would stop after `a`,
        # and a resume at created_at > ts would never read `b`.
        result = build_user_export_content(
            alice, filter_ai_usage=True, created_after=APR_07,
            max_tokens=1100, chronological_order=True, return_metadata=True)
        assert result["node_ids"] == {a.id, b.id}
        assert result["unit_count"] == 2000
        assert result["latest_node_created_at"] == ts

        result2 = build_user_export_content(
            alice, filter_ai_usage=True,
            created_after=result["latest_node_created_at"],
            max_tokens=1100, chronological_order=True, return_metadata=True)
        assert result2["node_ids"] == {c.id}
        assert result2["unit_count"] == 1000

    def test_count_remaining_units_matches_the_windows(self, app):
        from backend.routes.export_data import (
            build_user_export_content, count_remaining_units)
        alice, bob = _make_user("alice"), _make_user("bob")
        _db.session.commit()
        nodes = [
            _make_node(alice, content=f"n{i}", token_count=1000,
                       created_at=APR_07 + timedelta(days=i + 1))
            for i in range(3)
        ]
        _make_node(alice, content="opted out", token_count=999,
                   ai_usage="none", created_at=APR_18)          # not AI-readable
        _make_node(bob, content="someone else", token_count=999,
                   created_at=APR_18)                           # not in scope
        _db.session.commit()

        assert count_remaining_units(alice.id, APR_07) == 3000
        assert count_remaining_units(alice.id, None) == 3000
        assert count_remaining_units(alice.id, nodes[0].created_at) == 2000

        # Walking the windows to exhaustion covers exactly that sum.
        covered, cutoff = 0, APR_07
        while True:
            w = build_user_export_content(
                alice, filter_ai_usage=True, created_after=cutoff,
                max_tokens=1100, chronological_order=True,
                return_metadata=True)
            if not w:
                break
            covered += w["unit_count"]
            cutoff = w["latest_node_created_at"]
        assert covered == 3000
        assert count_remaining_units(alice.id, cutoff) == 0
