"""Tests for the user_id / parent_user_id fields on ancestors and children.

NodeDetail's per-bubble kebab needs to know which nodes the current user
owns (Edit/Delete are gated by ownership). The focal node already exposes
`user.id` and `parent_user_id`; these tests pin down that the same info is
present on every ancestor and every recursively-serialized child, with the
LLM-node derivation matching the focal serializer at nodes.py:822.
"""
import os
import sys
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
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


@pytest.fixture
def llm_user(app):
    u = User(username="claude-opus-4.6", twitter_id="claude-opus-4-6")
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _make_node(user, parent=None, content="hi", node_type="user",
               human_owner_id=None, privacy_level="public"):
    n = Node(
        user_id=user.id,
        human_owner_id=human_owner_id if human_owner_id is not None else user.id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        privacy_level=privacy_level,
        ai_usage="chat",
        token_count=1,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.commit()
    return n


def test_ancestors_carry_user_id_and_derived_parent_user_id(app, alice, bob):
    """Walking up: each ancestor's user_id is its author, parent_user_id is
    one level above (or None at the root). This is what the frontend uses
    to gate Edit/Delete on ancestor bubbles."""
    a_root = _make_node(alice)              # parent_user_id = None
    b_mid = _make_node(bob, parent=a_root)  # parent_user_id = alice.id
    a_focal = _make_node(alice, parent=b_mid)

    client = app.test_client()
    _login(client, alice)
    resp = client.get(f"/nodes/{a_focal.id}")
    assert resp.status_code == 200
    ancestors = resp.json["ancestors"]
    # ancestors are ordered root-first
    by_id = {a["id"]: a for a in ancestors}
    assert by_id[a_root.id]["user_id"] == alice.id
    assert by_id[a_root.id]["parent_user_id"] is None
    assert by_id[b_mid.id]["user_id"] == bob.id
    assert by_id[b_mid.id]["parent_user_id"] == alice.id


def test_llm_ancestor_parent_user_id_uses_human_owner(app, alice, llm_user):
    """LLM ancestors derive parent_user_id from human_owner_id, not from
    parent.user_id, so the focal serializer's rule (nodes.py:822) is
    mirrored on the way up the chain."""
    a_root = _make_node(alice)
    llm_mid = _make_node(
        llm_user, parent=a_root, node_type="llm", human_owner_id=alice.id,
    )
    a_focal = _make_node(alice, parent=llm_mid, human_owner_id=alice.id)

    client = app.test_client()
    _login(client, alice)
    resp = client.get(f"/nodes/{a_focal.id}")
    assert resp.status_code == 200
    by_id = {a["id"]: a for a in resp.json["ancestors"]}
    assert by_id[llm_mid.id]["user_id"] == llm_user.id
    # The LLM's "owner" for edit/delete purposes is alice — that's what
    # parent_user_id encodes for LLM nodes.
    assert by_id[llm_mid.id]["parent_user_id"] == alice.id


def test_children_carry_user_id_and_parent_user_id(app, alice, bob):
    """Direct children expose user_id and the focal node's user_id as
    their parent_user_id."""
    focal = _make_node(alice)
    c_alice = _make_node(alice, parent=focal)
    c_bob = _make_node(bob, parent=focal)

    client = app.test_client()
    _login(client, alice)
    resp = client.get(f"/nodes/{focal.id}")
    assert resp.status_code == 200
    children = {c["id"]: c for c in resp.json["children"]}
    assert children[c_alice.id]["user_id"] == alice.id
    assert children[c_alice.id]["parent_user_id"] == alice.id
    assert children[c_bob.id]["user_id"] == bob.id
    assert children[c_bob.id]["parent_user_id"] == alice.id


def test_grandchild_parent_user_id_threads_through_recursion(app, alice, bob):
    """parent_user_id propagates correctly through serialize_node_recursive
    so a grandchild knows its (intermediate) parent's owner without an N+1."""
    focal = _make_node(alice)
    mid_bob = _make_node(bob, parent=focal)
    grand_alice = _make_node(alice, parent=mid_bob)

    client = app.test_client()
    _login(client, alice)
    resp = client.get(f"/nodes/{focal.id}")
    assert resp.status_code == 200
    # children[0] = mid_bob; children[0].children[0] = grand_alice.
    mid_payload = next(c for c in resp.json["children"] if c["id"] == mid_bob.id)
    grand_payload = next(
        g for g in mid_payload["children"] if g["id"] == grand_alice.id
    )
    assert grand_payload["user_id"] == alice.id
    assert grand_payload["parent_user_id"] == bob.id


def test_llm_child_parent_user_id_uses_human_owner(app, alice, llm_user):
    """An LLM child of an alice-owned focal exposes parent_user_id=alice.id
    (since the focal's user_id is alice). When that LLM has a human child
    of its own, the grandchild's parent_user_id reflects the LLM's
    human_owner_id, mirroring the focal's :822 derivation at depth."""
    focal = _make_node(alice)
    llm_child = _make_node(
        llm_user, parent=focal, node_type="llm", human_owner_id=alice.id,
    )
    a_grand = _make_node(alice, parent=llm_child)

    client = app.test_client()
    _login(client, alice)
    resp = client.get(f"/nodes/{focal.id}")
    assert resp.status_code == 200
    llm_payload = next(c for c in resp.json["children"] if c["id"] == llm_child.id)
    assert llm_payload["user_id"] == llm_user.id
    assert llm_payload["parent_user_id"] == alice.id  # focal owner
    grand_payload = next(
        g for g in llm_payload["children"] if g["id"] == a_grand.id
    )
    # The LLM acts as parent → parent_user_id = LLM's human_owner_id.
    assert grand_payload["parent_user_id"] == alice.id
