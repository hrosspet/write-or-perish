"""backend/scripts/soft_delete_empty_imported.py (#317): removes the
empty imported nodes that predate the importer guard, and refuses any
row that is not an isolated leaf."""
import importlib.util
import os
import sys
from datetime import datetime
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User, Node, NodeContextArtifact  # noqa: E402


def _load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "soft_delete_empty_imported.py")
    spec = importlib.util.spec_from_file_location("_sd_empty", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _user(name):
    u = User(username=name, approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    return u


_seq = iter(range(1, 10_000))


def _node(u, content, **kw):
    kw.setdefault("source_key", f"twitter:{next(_seq)}")
    kw.setdefault("privacy_level", "private")
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             ai_usage="none", origin="twitter", **kw)
    n.set_content(content)
    db.session.add(n)
    db.session.flush()
    return n


def test_deletes_only_isolated_empty_imported_leaves(app):
    mod = _load_script()
    u = _user("alice")
    lone = _node(u, "")
    parent_of = _node(u, "")
    _node(u, "a reply", parent_id=parent_of.id)
    linked_to = _node(u, "")
    _node(u, "points here", linked_node_id=linked_to.id)
    with_artifact = _node(u, "")
    db.session.add(NodeContextArtifact(node_id=with_artifact.id,
                                       artifact_type="prompt", artifact_id=1))
    pinned = _node(u, "", pinned_at=datetime.utcnow())
    child = _node(u, "", parent_id=lone.id)
    has_text = _node(u, "real words")
    native = _node(u, "", source_key=None)
    public = _node(u, "", privacy_level="public")
    db.session.commit()

    rows = mod.empty_imported_query().order_by(Node.id.asc()).all()
    # Native (no source_key) and public rows are out of scope entirely.
    assert native.id not in [n.id for n in rows]
    assert public.id not in [n.id for n in rows]
    assert has_text.id not in [n.id for n in rows]

    deletable, held = mod.partition(rows)
    assert [n.id for n in deletable] == []  # lone has a child now
    assert held[parent_of.id] == "has children"
    assert held[linked_to.id] == "referenced as linked_node"
    assert held[with_artifact.id] == "carries a context artifact"
    assert held[pinned.id] == "pinned"
    assert held[child.id] == "has a parent"
    assert held[lone.id] == "has children"


def test_apply_soft_deletes_and_is_idempotent(app):
    mod = _load_script()
    u = _user("bob")
    a = _node(u, "")
    b = _node(u, "")
    keep = _node(u, "real words")
    db.session.commit()

    rows = mod.empty_imported_query().order_by(Node.id.asc()).all()
    deletable, held = mod.partition(rows)
    assert sorted(n.id for n in deletable) == sorted([a.id, b.id])
    assert held == {}

    for node in deletable:
        assert mod.soft_delete_node(node.id, node.human_owner_id,
                                    with_descendants=False).ids
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(Node, a.id).deleted_at is not None
    assert db.session.get(Node, b.id).deleted_at is not None
    assert db.session.get(Node, keep.id).deleted_at is None
    # Already-deleted rows drop out of the query, so a rerun is a no-op.
    assert mod.empty_imported_query().count() == 0
