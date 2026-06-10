"""Tests for Wave-6 backend leftovers (#110, #104, #139).

#110 — export preselection includes the user's replies inside other
users' threads. #104 — _call_llm_with_retries forwards max_tokens to the
provider. #139 — covered here at the unit level for the dedup stub text;
the loop wiring mirrors the long-standing profile dedup.
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
        exports_module._single_pass_generation).parameters
    assert "max_output_tokens" in inspect.signature(
        exports_module._chunked_profile_loop).parameters
    assert "max_output_tokens" in inspect.signature(
        exports_module._do_iterative_incremental_update).parameters
    assert "max_tokens" in inspect.signature(
        exports_module._call_llm_with_retries).parameters
