"""Integration tests for privacy filtering on feed, public dashboard, and node detail.

Tests that private nodes are excluded from list endpoints (feed, public dashboard)
and that the node detail endpoint returns 403 for unauthorized access.

These tests build a minimal Flask app to avoid conflicts with module-level
flask_login mocks in other test files (e.g. test_auth.py).
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

# Mock optional heavy deps that may not be installed locally
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest
from flask import Flask

# ── Force-import real modules ────────────────────────────────────────────
# Earlier test files (test_auth.py, etc.) replace flask_login and
# backend.models with MagicMock at module level.  We must evict those mocks
# NOW (at import time) so our own imports get the real implementations, and
# then save references to restore them later during fixture setup.
_SENTINEL = object()
for _mod in [k for k in list(sys.modules)
             if k == "flask_login" or k.startswith("backend.")]:
    _m = sys.modules[_mod]
    if isinstance(_m, MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402


def _make_app():
    """Build a minimal Flask app with only the blueprints under test."""
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

    from backend.routes.feed import feed_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.nodes import nodes_bp

    app.register_blueprint(feed_bp, url_prefix="/api")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")

    return app


@pytest.fixture
def app():
    # Snapshot keys we're about to touch so we can restore them exactly.
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    # Install real flask_login and backend.models; purge route modules
    # so they get re-imported with real decorators/models.
    # We intentionally leave backend.utils.* untouched — the route code
    # passes explicit user_id args, so mocked current_user in the utils
    # module doesn't matter.
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

    # Restore sys.modules exactly: remove anything new, reinstate originals
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def data(app):
    """Create two users with public and private nodes each.

    Returns plain IDs to avoid SQLAlchemy DetachedInstanceError.
    """
    alice = User(username="alice", approved=True)
    bob = User(username="bob", approved=True)
    _db.session.add_all([alice, bob])
    _db.session.flush()

    alice_public = Node(
        user_id=alice.id, content="Alice public post",
        privacy_level="public", node_type="user",
    )
    alice_private = Node(
        user_id=alice.id, content="Alice private post",
        privacy_level="private", node_type="user",
    )
    bob_public = Node(
        user_id=bob.id, content="Bob public post",
        privacy_level="public", node_type="user",
    )
    bob_private = Node(
        user_id=bob.id, content="Bob private post",
        privacy_level="private", node_type="user",
    )

    _db.session.add_all([alice_public, alice_private, bob_public, bob_private])
    _db.session.commit()

    return dict(
        alice_id=alice.id, bob_id=bob.id,
        alice_public_id=alice_public.id, alice_private_id=alice_private.id,
        bob_public_id=bob_public.id, bob_private_id=bob_private.id,
    )


def _login(client, user_id):
    """Set the Flask-Login session cookie so subsequent requests are authenticated."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# ── Feed ─────────────────────────────────────────────────────────────────

class TestFeedPrivacy:
    """GET /api/feed should only return public nodes + the caller's own nodes."""

    def test_feed_hides_other_users_private_nodes(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])

        resp = client.get("/api/feed")
        assert resp.status_code == 200
        previews = [n["preview"] for n in resp.json["nodes"]]

        assert "Alice public post" in previews
        assert "Alice private post" in previews   # own node
        assert "Bob public post" in previews
        assert "Bob private post" not in previews  # someone else's private

    def test_feed_shows_own_private_nodes(self, app, data):
        client = app.test_client()
        _login(client, data["bob_id"])

        resp = client.get("/api/feed")
        previews = [n["preview"] for n in resp.json["nodes"]]

        assert "Bob private post" in previews      # own node
        assert "Alice private post" not in previews

    def test_feed_total_count_respects_privacy(self, app, data):
        """The pagination total must not include inaccessible nodes."""
        client = app.test_client()
        _login(client, data["alice_id"])

        resp = client.get("/api/feed")
        # Alice sees: her 2 nodes + Bob's 1 public node = 3
        assert resp.json["total"] == 3


# ── Public Dashboard ─────────────────────────────────────────────────────

class TestPublicDashboardPrivacy:
    """GET /api/dashboard/<username> should only show public nodes to other users."""

    def test_hides_private_nodes_from_other_users(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])

        resp = client.get("/api/dashboard/bob")
        assert resp.status_code == 200
        previews = [n["preview"] for n in resp.json["nodes"]]

        assert "Bob public post" in previews
        assert "Bob private post" not in previews

    def test_owner_sees_all_own_nodes(self, app, data):
        client = app.test_client()
        _login(client, data["bob_id"])

        resp = client.get("/api/dashboard/bob")
        previews = [n["preview"] for n in resp.json["nodes"]]

        assert "Bob public post" in previews
        assert "Bob private post" in previews

    def test_total_count_excludes_private_for_others(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])

        resp = client.get("/api/dashboard/bob")
        assert resp.json["total_nodes"] == 1  # only Bob's public node

    def test_total_count_includes_all_for_owner(self, app, data):
        client = app.test_client()
        _login(client, data["bob_id"])

        resp = client.get("/api/dashboard/bob")
        assert resp.json["total_nodes"] == 2  # both of Bob's nodes


# ── Node Detail ──────────────────────────────────────────────────────────

class TestNodeDetailPrivacy:
    """GET /api/nodes/<id> should return 403 for private nodes of other users."""

    def test_returns_403_for_other_users_private_node(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])

        resp = client.get(f"/api/nodes/{data['bob_private_id']}")
        assert resp.status_code == 403
        assert "Not authorized" in resp.json["error"]

    def test_owner_can_access_own_private_node(self, app, data):
        client = app.test_client()
        _login(client, data["bob_id"])

        resp = client.get(f"/api/nodes/{data['bob_private_id']}")
        assert resp.status_code == 200
        assert "Bob private post" in resp.json["content"]

    def test_anyone_can_access_public_node(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])

        resp = client.get(f"/api/nodes/{data['bob_public_id']}")
        assert resp.status_code == 200
        assert "Bob public post" in resp.json["content"]
