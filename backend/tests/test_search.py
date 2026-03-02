"""Tests for the search endpoint.

Follows the same pattern as test_feed_dashboard_privacy.py:
ENCRYPTION_DISABLED=true, sqlite in-memory, minimal Flask app.
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

# Mock optional heavy deps
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest
from flask import Flask

# ── Force-import real modules ────────────────────────────────────────────
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

    from backend.routes.search import search_bp
    app.register_blueprint(search_bp, url_prefix="/api")

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
def data(app):
    """Create users and nodes for search tests.

    Structure:
    - alice: human user
    - bob: human user
    - llm_bot: system user representing an LLM model
    - dec_node (alice, user) → llm_reply (llm_bot, llm) → llm_reply2 (llm_bot, llm)
    - jan_node (alice, user)
    - bob_node (bob, user) → bob_llm (llm_bot, llm)
    """
    alice = User(username="alice", approved=True)
    bob = User(username="bob", approved=True)
    llm_bot = User(username="claude-3-opus", approved=True)
    _db.session.add_all([alice, bob, llm_bot])
    _db.session.flush()

    # Alice's nodes: one in December, one in January
    dec_node = Node(
        user_id=alice.id, human_owner_id=alice.id,
        content="My December reflection on winter solstice",
        privacy_level="private", node_type="user",
        created_at=datetime(2025, 12, 15),
    )
    jan_node = Node(
        user_id=alice.id, human_owner_id=alice.id,
        content="January new year resolutions and plans",
        privacy_level="private", node_type="user",
        created_at=datetime(2026, 1, 10),
    )
    _db.session.add_all([dec_node, jan_node])
    _db.session.flush()

    # LLM reply to Alice's dec_node (owned by bot, parented to Alice's node)
    llm_reply = Node(
        user_id=llm_bot.id, human_owner_id=alice.id,
        parent_id=dec_node.id,
        content="LLM response about winter traditions",
        privacy_level="private", node_type="llm",
        llm_model="claude-3-opus",
        created_at=datetime(2025, 12, 20),
    )
    _db.session.add(llm_reply)
    _db.session.flush()

    # Second-level LLM reply (Human → LLM → LLM chain)
    llm_reply2 = Node(
        user_id=llm_bot.id, human_owner_id=alice.id,
        parent_id=llm_reply.id,
        content="Deeper LLM winter follow-up",
        privacy_level="private", node_type="llm",
        llm_model="claude-3-opus",
        created_at=datetime(2025, 12, 21),
    )

    # Bob's node (should never appear in Alice's search)
    bob_node = Node(
        user_id=bob.id, human_owner_id=bob.id,
        content="Bob's December winter thoughts",
        privacy_level="public", node_type="user",
        created_at=datetime(2025, 12, 18),
    )
    _db.session.add_all([llm_reply2, bob_node])
    _db.session.flush()

    # Bob's LLM reply (should not appear in Alice's search)
    bob_llm = Node(
        user_id=llm_bot.id, human_owner_id=bob.id,
        parent_id=bob_node.id,
        content="LLM response about Bob's winter",
        privacy_level="private", node_type="llm",
        llm_model="claude-3-opus",
        created_at=datetime(2025, 12, 19),
    )
    _db.session.add(bob_llm)
    _db.session.commit()

    return dict(
        alice_id=alice.id, bob_id=bob.id, llm_bot_id=llm_bot.id,
        dec_node_id=dec_node.id, jan_node_id=jan_node.id,
        llm_reply_id=llm_reply.id, llm_reply2_id=llm_reply2.id,
        bob_node_id=bob_node.id, bob_llm_id=bob_llm.id,
    )


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


class TestSearch:

    def test_no_params_returns_400(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search")
        assert resp.status_code == 400

    def test_keyword_search_finds_match(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=solstice")
        assert resp.status_code == 200
        assert resp.json["total"] == 1
        assert resp.json["results"][0]["id"] == data["dec_node_id"]
        assert "<mark>" in resp.json["results"][0]["snippet"]

    def test_keyword_search_case_insensitive(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=DECEMBER")
        assert resp.status_code == 200
        assert resp.json["total"] == 1
        assert resp.json["results"][0]["id"] == data["dec_node_id"]

    def test_only_returns_accessible_nodes(self, app, data):
        """Alice sees her own nodes + LLM replies in her threads, not Bob's."""
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=winter")
        ids = [r["id"] for r in resp.json["results"]]
        assert data["bob_node_id"] not in ids
        assert data["bob_llm_id"] not in ids
        # Alice has: dec_node, llm_reply, llm_reply2 (all contain "winter")
        assert data["dec_node_id"] in ids
        assert data["llm_reply_id"] in ids
        assert data["llm_reply2_id"] in ids

    def test_date_range_filter(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?from=2025-12-01&to=2025-12-31")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json["results"]]
        assert data["dec_node_id"] in ids
        assert data["llm_reply_id"] in ids
        assert data["llm_reply2_id"] in ids
        assert data["jan_node_id"] not in ids

    def test_keyword_plus_date_range(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=winter&from=2025-12-01&to=2025-12-16")
        assert resp.status_code == 200
        # Only dec_node (Dec 15) matches; llm_node is Dec 20
        assert resp.json["total"] == 1
        assert resp.json["results"][0]["id"] == data["dec_node_id"]

    def test_node_type_filter(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=winter&node_type=llm")
        assert resp.status_code == 200
        assert resp.json["total"] == 2  # llm_reply + llm_reply2
        for r in resp.json["results"]:
            assert r["node_type"] == "llm"

    def test_pagination(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        # 3 "winter" matches: dec_node, llm_reply, llm_reply2
        resp = client.get("/api/search?q=winter&per_page=1&page=1")
        assert resp.status_code == 200
        assert len(resp.json["results"]) == 1
        assert resp.json["total"] == 3
        assert resp.json["has_more"] is True

        resp2 = client.get("/api/search?q=winter&per_page=1&page=3")
        assert len(resp2.json["results"]) == 1
        assert resp2.json["has_more"] is False

    def test_response_shape(self, app, data):
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=solstice")
        body = resp.json
        assert "results" in body
        assert "page" in body
        assert "per_page" in body
        assert "total" in body
        assert "has_more" in body
        assert body["search_type"] == "keyword"

        r = body["results"][0]
        for key in ["id", "preview", "snippet", "node_type",
                     "created_at", "username", "child_count",
                     "parent_id", "score"]:
            assert key in r

    def test_date_only_search(self, app, data):
        """Date range without keyword should return all nodes in range."""
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?from=2026-01-01&to=2026-01-31")
        assert resp.status_code == 200
        assert resp.json["total"] == 1
        assert resp.json["results"][0]["id"] == data["jan_node_id"]
        # No keyword, so snippet should be None
        assert resp.json["results"][0]["snippet"] is None

    def test_llm_reply_in_own_thread_appears(self, app, data):
        """LLM nodes in user's thread should be searchable."""
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=traditions")
        assert resp.status_code == 200
        assert resp.json["total"] == 1
        assert resp.json["results"][0]["id"] == data["llm_reply_id"]

    def test_deep_llm_chain_appears(self, app, data):
        """Human → LLM → LLM chain: second-level LLM is searchable."""
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=follow-up")
        assert resp.status_code == 200
        assert resp.json["total"] == 1
        assert resp.json["results"][0]["id"] == data["llm_reply2_id"]

    def test_other_users_llm_replies_hidden(self, app, data):
        """Bob's LLM replies should not appear in Alice's search."""
        client = app.test_client()
        _login(client, data["alice_id"])
        resp = client.get("/api/search?q=Bob's winter")
        ids = [r["id"] for r in resp.json["results"]]
        assert data["bob_llm_id"] not in ids
        assert data["bob_node_id"] not in ids
