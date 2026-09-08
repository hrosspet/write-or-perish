"""Tests for the Log thread name: PUT /nodes/<root>/thread-name (a Thread
row keyed by the root node, name encrypted) and the `thread_name` field on
Log cards.

Patterned after test_node_deletion.py: sqlite in-memory, minimal Flask
app, ENCRYPTION_DISABLED.
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
from backend.models import User, Node, Thread  # noqa: E402
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
    from backend.routes.feed import feed_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(feed_bp, url_prefix="/api")

    return app


@pytest.fixture
def app():
    # Same isolation pattern as test_node_deletion.py — only flask_login,
    # backend.routes.*, and backend.models are swapped, and restored
    # exactly afterwards.
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


def _make_node(user, parent=None, content="hello"):
    node = Node(
        user_id=user.id,
        human_owner_id=user.id,
        parent_id=parent.id if parent else None,
        node_type="user",
        privacy_level="private",
        ai_usage="none",
        token_count=1,
    )
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _rename(client, node_id, name):
    return client.put(f"/nodes/{node_id}/thread-name", json={"thread_name": name})


def _stored_name(root_id):
    """Decrypted name from the thread table, or None when there is no row."""
    _db.session.expire_all()
    row = _db.session.get(Thread, root_id)
    return row.get_name() if row is not None else None


def _feed_card(client, root_id):
    resp = client.get("/api/feed")
    assert resp.status_code == 200
    return next(c for c in resp.json["nodes"] if c["thread_root_id"] == root_id)


# ── Setting and clearing ─────────────────────────────────────────────────

def test_owner_names_thread_and_log_card_carries_it(app, alice):
    root = _make_node(alice, content="# 2026-09-05 12:12:01 Voice note\n\nbody")
    client = app.test_client()
    _login(client, alice)

    resp = _rename(client, root.id, "Teplárna plan")
    assert resp.status_code == 200
    assert resp.json["thread_name"] == "Teplárna plan"

    assert _stored_name(root.id) == "Teplárna plan"
    assert _feed_card(client, root.id)["thread_name"] == "Teplárna plan"


def test_card_without_name_has_null(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    assert _feed_card(client, root.id)["thread_name"] is None


def test_empty_string_clears_name(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    assert _rename(client, root.id, "Named").status_code == 200

    resp = _rename(client, root.id, "")
    assert resp.status_code == 200
    assert resp.json["thread_name"] is None
    assert _stored_name(root.id) is None
    assert _feed_card(client, root.id)["thread_name"] is None


def test_whitespace_is_trimmed_and_blank_clears(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)

    assert _rename(client, root.id, "  padded  ").json["thread_name"] == "padded"
    assert _rename(client, root.id, "   ").json["thread_name"] is None
    assert _stored_name(root.id) is None


def test_missing_body_field_clears(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    _rename(client, root.id, "Named")

    resp = client.put(f"/nodes/{root.id}/thread-name", json={})
    assert resp.status_code == 200
    assert resp.json["thread_name"] is None


def test_rename_does_not_touch_updated_at(app, alice):
    """A label change is not new writing: the root's updated_at drives the
    Log's newest-node jump and freshness gates, so it must stay put."""
    root = _make_node(alice)
    before = _db.session.get(Node, root.id).updated_at
    client = app.test_client()
    _login(client, alice)

    assert _rename(client, root.id, "Named").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(Node, root.id).updated_at == before


# ── Validation ───────────────────────────────────────────────────────────

def test_name_over_limit_rejected(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)

    resp = _rename(client, root.id, "x" * 121)
    assert resp.status_code == 400
    assert resp.json["max_length"] == 120
    assert _stored_name(root.id) is None

    assert _rename(client, root.id, "x" * 120).status_code == 200


def test_non_string_rejected(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    assert _rename(client, root.id, 42).status_code == 400
    assert _rename(client, root.id, ["a"]).status_code == 400


def test_reply_cannot_be_named(app, alice):
    root = _make_node(alice)
    reply = _make_node(alice, parent=root, content="reply")
    client = app.test_client()
    _login(client, alice)

    resp = _rename(client, reply.id, "Nope")
    assert resp.status_code == 400
    assert _stored_name(reply.id) is None


# ── Authorization ────────────────────────────────────────────────────────

def test_other_user_gets_403(app, alice, bob):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, bob)

    resp = _rename(client, root.id, "Mine now")
    assert resp.status_code == 403
    assert _stored_name(root.id) is None


def test_anonymous_rejected(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    resp = _rename(client, root.id, "Anon")
    assert resp.status_code in (401, 302)


def test_unknown_node_404(app, alice):
    client = app.test_client()
    _login(client, alice)
    assert _rename(client, 999999, "Ghost").status_code == 404


# ── Storage ──────────────────────────────────────────────────────────────

def test_renaming_twice_keeps_one_row(app, alice):
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    _rename(client, root.id, "First")
    _rename(client, root.id, "Second")
    assert Thread.query.filter_by(root_node_id=root.id).count() == 1
    assert _stored_name(root.id) == "Second"


def test_name_goes_through_content_encryption(app, alice, monkeypatch):
    """The name is user-authored text: it must be stored via the same
    envelope helpers as content and decrypted on the way out. Encryption
    is disabled in tests, so route the helpers through a marker instead."""
    monkeypatch.setattr(_real_backend_models, "encrypt_content",
                        lambda p: "ENC:" + p)
    monkeypatch.setattr(_real_backend_models, "decrypt_content",
                        lambda c: c[4:] if c.startswith("ENC:") else c)
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)

    assert _rename(client, root.id, "Secret label").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(Thread, root.id).name == "ENC:Secret label"
    assert _feed_card(client, root.id)["thread_name"] == "Secret label"


def test_deleting_root_removes_thread_row(app, alice):
    """ORM delete of the root (the hard-purge path) takes the name with
    it — no orphan rows, no FK error."""
    root = _make_node(alice)
    client = app.test_client()
    _login(client, alice)
    _rename(client, root.id, "Named")

    _db.session.delete(_db.session.get(Node, root.id))
    _db.session.commit()
    assert _db.session.get(Thread, root.id) is None
