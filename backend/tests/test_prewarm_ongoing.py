"""Tests for the ongoing-thread pre-warm helper (#187, system-only warm).

Covers _find_thread_system_node: walking up a thread from the reply target
to the system node (the ancestor carrying the 'prompt' artifact), which the
>5-min ongoing pre-warm uses to locate the node to warm. The full-prefix
warm is tracked in #224.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node, NodeContextArtifact  # noqa: E402

# streaming_transcription pulls in the celery glue; stub it to import the
# pure helper (same pattern as test_semantic_search).
_saved = sys.modules.get("backend.celery_app")
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.streaming_transcription", None)
from backend.tasks.streaming_transcription import (  # noqa: E402
    _find_thread_system_node,
)
if _saved is None:
    sys.modules.pop("backend.celery_app", None)
else:
    sys.modules["backend.celery_app"] = _saved


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="tester"))
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()


def _node(uid, parent_id=None, with_prompt=False, deleted=False):
    n = Node(user_id=uid, parent_id=parent_id, node_type="user")
    n.set_content("x")
    if deleted:
        from datetime import datetime
        n.deleted_at = datetime(2026, 1, 1)
    _db.session.add(n)
    _db.session.flush()
    if with_prompt:
        _db.session.add(NodeContextArtifact(
            node_id=n.id, artifact_type="prompt", artifact_id=1))
    _db.session.commit()
    return n


def test_finds_system_node_from_deep_reply_target(app):
    uid = User.query.first().id
    # system -> user -> llm -> user(reply target)
    system = _node(uid, with_prompt=True)
    u1 = _node(uid, parent_id=system.id)
    a1 = _node(uid, parent_id=u1.id)
    reply_target = _node(uid, parent_id=a1.id)

    assert _find_thread_system_node(reply_target.id).id == system.id


def test_returns_none_when_no_prompt_ancestor(app):
    uid = User.query.first().id
    root = _node(uid)                       # no prompt artifact anywhere
    child = _node(uid, parent_id=root.id)
    assert _find_thread_system_node(child.id) is None


def test_skips_soft_deleted_system_node(app):
    uid = User.query.first().id
    # A soft-deleted system node must not be warmed against.
    deleted_system = _node(uid, with_prompt=True, deleted=True)
    child = _node(uid, parent_id=deleted_system.id)
    assert _find_thread_system_node(child.id) is None
