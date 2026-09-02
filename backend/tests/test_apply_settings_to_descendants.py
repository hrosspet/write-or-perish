"""PUT /nodes/<id> with apply_to_descendants: the privacy / AI-usage change
made on a node is also applied to every alive reply below it that the
user may edit — their own nodes and LLM nodes they are the human owner
of. Other users' replies are untouched but walked through. Only the
settings that actually changed on the edited node propagate.

Patterned after test_tts_invalidation.py: sqlite in-memory, minimal Flask
app, ENCRYPTION_DISABLED.
"""
import os
import sys
from datetime import datetime
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
    # Same sys.modules dance as test_tts_invalidation.py, plus restoring
    # the package attributes the fresh imports re-point (see
    # test_detached_prompt_stays_agentic.py for why).
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.utils.privacy"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [
        k for k in list(sys.modules)
        if k.startswith("backend.routes") or k == "backend.utils.privacy"
    ]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    current = [k for k in list(sys.modules) if _affected(k)]
    for k in current:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    for k in set(current) | set(saved):
        pkg_name, _, attr = k.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is None or not attr:
            continue
        if k in saved:
            setattr(pkg, attr, saved[k])
        elif hasattr(pkg, attr):
            delattr(pkg, attr)


@pytest.fixture
def users(app):
    alice = User(username="alice", twitter_id="alice-twitter-id")
    bob = User(username="bob", twitter_id="bob-twitter-id")
    llm = User(username="claude-test", twitter_id="llm-test")
    _db.session.add_all([alice, bob, llm])
    _db.session.commit()
    return alice, bob, llm


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _node(user, parent=None, *, human_owner=None, node_type="user",
          privacy="private", ai_usage="chat", content="text"):
    n = Node(user_id=user.id,
             human_owner_id=(human_owner or user).id,
             node_type=node_type,
             llm_model="claude-test" if node_type == "llm" else None,
             privacy_level=privacy, ai_usage=ai_usage, token_count=1,
             parent_id=parent.id if parent else None)
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


@pytest.fixture
def tree(users):
    """root(alice)
         ├─ a1(alice)
         │    └─ llm_a(llm user, human owner alice)
         ├─ b1(bob)
         │    ├─ llm_b(llm user, human owner bob)
         │    └─ a2(alice, under bob's reply)
         └─ gone(alice, soft-deleted)"""
    alice, bob, llm = users
    root = _node(alice, content="root")
    a1 = _node(alice, root, content="a1")
    llm_a = _node(llm, a1, human_owner=alice, node_type="llm", content="llm_a")
    b1 = _node(bob, root, content="b1")
    llm_b = _node(llm, b1, human_owner=bob, node_type="llm", content="llm_b")
    a2 = _node(alice, b1, content="a2")
    gone = _node(alice, root, content="gone")
    gone.deleted_at = datetime.utcnow()
    _db.session.commit()
    return dict(root=root, a1=a1, llm_a=llm_a, b1=b1, llm_b=llm_b, a2=a2,
                gone=gone)


def _put(app, user, node, **fields):
    client = app.test_client()
    _login(client, user)
    body = {"content": node.get_content(), "privacy_level": node.privacy_level,
            "ai_usage": node.ai_usage}
    body.update(fields)
    return client.put(f"/nodes/{node.id}", json=body)


def _fresh(nodes):
    return {k: Node.query.get(n.id) for k, n in nodes.items()}


def test_cascade_reaches_only_the_users_replies(app, users, tree):
    alice, _, _ = users
    resp = _put(app, alice, tree["root"], ai_usage="none",
                apply_to_descendants=True)
    assert resp.status_code == 200
    assert resp.json["descendants_updated"] == 3          # a1, llm_a, a2
    t = _fresh(tree)
    assert t["root"].ai_usage == "none"
    assert t["a1"].ai_usage == "none"
    assert t["llm_a"].ai_usage == "none"                  # LLM node she owns
    assert t["a2"].ai_usage == "none"                     # hers, under bob's
    assert t["b1"].ai_usage == "chat"                     # bob's
    assert t["llm_b"].ai_usage == "chat"                  # bob's LLM node
    assert t["gone"].ai_usage == "chat"                   # tombstone


def test_only_the_changed_setting_propagates(app, users, tree):
    alice, _, _ = users
    tree["a1"].ai_usage = "train"
    _db.session.commit()
    resp = _put(app, alice, tree["root"], privacy_level="public",
                apply_to_descendants=True)
    assert resp.status_code == 200
    assert resp.json["descendants_updated"] == 3
    t = _fresh(tree)
    assert t["a1"].privacy_level == "public"
    assert t["a1"].ai_usage == "train"                    # untouched
    assert t["b1"].privacy_level == "private"


def test_going_private_unpins_replies(app, users, tree):
    alice, _, _ = users
    for k in ("root", "a1"):
        tree[k].privacy_level = "public"
    tree["a1"].pinned_at = datetime.utcnow()
    tree["a1"].pinned_by = alice.id
    _db.session.commit()
    resp = _put(app, alice, tree["root"], privacy_level="private",
                apply_to_descendants=True)
    assert resp.status_code == 200
    t = _fresh(tree)
    assert t["a1"].privacy_level == "private"
    assert t["a1"].pinned_at is None
    assert t["a1"].pinned_by is None


def test_no_flag_leaves_replies_alone(app, users, tree):
    alice, _, _ = users
    resp = _put(app, alice, tree["root"], ai_usage="none")
    assert resp.status_code == 200
    assert resp.json["descendants_updated"] == 0
    t = _fresh(tree)
    assert t["root"].ai_usage == "none"
    assert t["a1"].ai_usage == "chat"


def test_flag_without_a_change_is_a_noop(app, users, tree):
    alice, _, _ = users
    resp = _put(app, alice, tree["root"], apply_to_descendants=True)
    assert resp.status_code == 200
    assert resp.json["descendants_updated"] == 0
    assert _fresh(tree)["a1"].ai_usage == "chat"


def test_cascade_from_a_reply_only_covers_its_subtree(app, users, tree):
    alice, _, _ = users
    resp = _put(app, alice, tree["a1"], ai_usage="none",
                apply_to_descendants=True)
    assert resp.status_code == 200
    assert resp.json["descendants_updated"] == 1          # llm_a
    t = _fresh(tree)
    assert t["llm_a"].ai_usage == "none"
    assert t["a2"].ai_usage == "chat"
    assert t["root"].ai_usage == "chat"
