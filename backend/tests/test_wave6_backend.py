"""Tests for Wave-6 backend leftovers (#110, #104).

#110 — export preselection includes the user's replies inside other
users' threads. #104 — _call_llm_with_retries forwards max_tokens to the
provider + every generation helper accepts max_output_tokens.

#139 (the {user_export} first-occurrence dedup) lives in the
generate_llm_response message-build loop, so its test rides that harness:
see test_retrieval_loop.py::test_user_export_deduped_to_first_occurrence.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402

# Glue import for the celery-tainted modules
_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.exports")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.exports", None)
import backend.tasks.exports as exports_module  # noqa: E402
for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        a = User(username="alice")
        b = User(username="bob")
        _db.session.add_all([a, b])
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _mk_node(user, parent=None, content="x", human_owner=None):
    node = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent.id if parent else None,
        node_type="user",
        token_count=10,
        privacy_level="private",
        ai_usage="chat",
    )
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


# ── #110: preselection includes replies in foreign threads ───────────────

def test_preselect_includes_replies_in_foreign_threads(app):
    from backend.routes.export_data import _preselect_node_ids
    with app.app_context():
        alice = User.query.filter_by(username="alice").first()
        bob = User.query.filter_by(username="bob").first()

        own_root = _mk_node(alice, content="alice's own thread")
        bob_root = _mk_node(bob, content="bob's thread")
        alice_reply = _mk_node(alice, parent=bob_root,
                               content="alice replying to bob")
        reply_child = _mk_node(bob, parent=alice_reply,
                               human_owner=alice,
                               content="bob under alice's reply")

        ids = set(_preselect_node_ids(alice.id, budget=10_000))
        assert own_root.id in ids
        # The fix (#110): alice's reply inside bob's thread is included,
        # along with the sub-thread beneath it.
        assert alice_reply.id in ids
        assert reply_child.id in ids
        # Bob's own root is not seeded by alice's export.
        assert bob_root.id not in ids


def test_preselect_walks_foreign_chains_and_stops_at_own_nodes(app):
    """The thread walk (2026-09-02, replaced the recursive CTE) lists only
    OTHER authors' nodes; the user's own are matched by user_id. It must
    still reach: foreign chains under an own node, own nodes buried under
    foreign chains (and the foreign sub-threads under those), and nodes
    below a foreign node the viewer can't see. Unrelated threads stay out.
    """
    from datetime import datetime
    from backend.routes.export_data import _preselect_node_ids
    with app.app_context():
        alice = User.query.filter_by(username="alice").first()
        bob = User.query.filter_by(username="bob").first()

        root = _mk_node(alice, content="alice root")
        # foreign → foreign → own → foreign: the walk must not stop at the
        # first foreign level, and must resume under alice's buried reply.
        f1 = _mk_node(bob, parent=root, human_owner=alice, content="llm 1")
        f2 = _mk_node(bob, parent=f1, human_owner=alice, content="llm 2")
        own_deep = _mk_node(alice, parent=f2, content="alice deep")
        f3 = _mk_node(bob, parent=own_deep, human_owner=alice, content="llm 3")
        # A foreign node alice can't see (bob's private reply in her
        # thread) is walked THROUGH: alice's reply under it and the
        # foreign sub-thread under that are still in scope.
        hidden = _mk_node(bob, parent=root, content="bob private aside")
        own_under_hidden = _mk_node(alice, parent=hidden,
                                    content="alice under hidden")
        f_under_hidden = _mk_node(bob, parent=own_under_hidden,
                                  human_owner=alice, content="llm 4")
        # Soft-deleted foreign node: walked through, and itself listed as
        # a tombstone because alice human-owns it (§5a).
        gone = _mk_node(bob, parent=root, human_owner=alice, content="gone")
        gone.deleted_at = datetime(2026, 1, 1)
        _db.session.commit()
        after_gone = _mk_node(bob, parent=gone, human_owner=alice,
                              content="llm after gone")
        # Bob's own thread, untouched by alice: out of scope entirely.
        bob_root = _mk_node(bob, content="bob root")
        bob_child = _mk_node(bob, parent=bob_root, content="bob child")

        ids = set(_preselect_node_ids(alice.id, budget=10_000))
        assert ids == {
            root.id, f1.id, f2.id, own_deep.id, f3.id,
            own_under_hidden.id, f_under_hidden.id,
            gone.id, after_gone.id,
        }
        assert hidden.id not in ids
        assert bob_root.id not in ids and bob_child.id not in ids


# ── #104: max_tokens forwarded to the provider ───────────────────────────

def test_call_llm_with_retries_forwards_max_tokens(app):
    captured = {}

    def fake_get_completion(model_id, messages, api_keys, max_tokens=None,
                            **kwargs):
        captured["max_tokens"] = max_tokens
        return {"content": "ok", "total_tokens": 1,
                "input_tokens": 1, "output_tokens": 0}

    with app.app_context():
        original = exports_module.LLMProvider
        exports_module.LLMProvider = MagicMock(
            get_completion=fake_get_completion)
        try:
            task_self = MagicMock()
            exports_module._call_llm_with_retries(
                task_self, "claude-opus-4.6", "prompt", 1, {},
                max_tokens=1234)
        finally:
            exports_module.LLMProvider = original
        assert captured["max_tokens"] == 1234


def test_generation_helpers_accept_max_output_tokens():
    """Regression for the staging-caught TypeError: every #104 call site
    passes max_output_tokens — the defs must accept it."""
    import inspect
    assert "max_output_tokens" in inspect.signature(
        exports_module._chunked_profile_loop).parameters
    assert "max_output_tokens" in inspect.signature(
        exports_module._do_iterative_incremental_update).parameters
    assert "max_tokens" in inspect.signature(
        exports_module._call_llm_with_retries).parameters
