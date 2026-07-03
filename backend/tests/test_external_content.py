"""Tests for the external-content substrate (#155 component 2 / Download).

Covers: normalization (CA rows, X bookmark objects, import-file shapes),
per-user dedupe on upsert, the items/import routes, env-gating of the X
connect flow, and external items in semantic search. All network calls
mocked — no CA/X traffic.
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
from backend.models import (  # noqa: E402
    User, ExternalItem, ExternalItemEmbedding,
)
from backend.utils.external_content import (  # noqa: E402
    normalize_ca_tweet, normalize_x_bookmark,
)

# Glue-import the sync task helpers
_GLUE = ("backend.celery_app", "backend.tasks.external_sync")
_saved = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.external_sync", None)
from backend.tasks.external_sync import _upsert_items  # noqa: E402
for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


@pytest.fixture
def app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["OPENAI_API_KEY_CHAT"] = "fake-key"
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.external import external_bp
    from backend.routes.search import search_bp
    app.register_blueprint(external_bp, url_prefix="/api/external")
    app.register_blueprint(search_bp, url_prefix="/api")

    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


# ── Normalization ────────────────────────────────────────────────────────

def test_normalize_ca_tweet_field_tolerance():
    a = normalize_ca_tweet(
        {"tweet_id": 123, "full_text": "hello", "created_at":
         "2024-08-01T12:00:00+00:00"}, "alice")
    assert a["external_id"] == "123"
    assert a["content"] == "hello"
    assert a["url"].endswith("/alice/status/123")
    assert a["posted_at"].year == 2024

    b = normalize_ca_tweet({"id": "9", "text": "alt names"}, "bob")
    assert b["external_id"] == "9"

    assert normalize_ca_tweet({"tweet_id": 1}, "x") is None  # no text
    assert normalize_ca_tweet({"full_text": "t"}, "x") is None  # no id


def test_normalize_x_bookmark():
    item = normalize_x_bookmark(
        {"id": "55", "text": "saved tweet", "author_id": "7",
         "created_at": "2025-01-01T00:00:00.000Z"},
        {"7": {"username": "carol"}})
    assert item["author_handle"] == "carol"
    assert item["url"].endswith("/carol/status/55")
    # Unknown author still produces a working status URL
    anon = normalize_x_bookmark({"id": "56", "text": "x"}, {})
    assert "/i/status/56" in anon["url"]


# ── Upsert / dedupe ──────────────────────────────────────────────────────

def test_upsert_dedupes_per_user_and_source(app):
    with app.app_context():
        uid = User.query.first().id
        items = [
            {"external_id": "1", "content": "a", "author_handle": "x",
             "url": None, "posted_at": None},
            {"external_id": "2", "content": "b", "author_handle": "x",
             "url": None, "posted_at": None},
        ]
        created, skipped = _upsert_items(uid, "community_archive", items)
        assert (created, skipped) == (2, 0)
        created, skipped = _upsert_items(uid, "community_archive", items)
        assert (created, skipped) == (0, 2)
        # Same external_id under a different source is a separate item
        created, _ = _upsert_items(uid, "twitter_bookmark", items[:1])
        assert created == 1
        assert ExternalItem.query.count() == 3


# ── Routes ───────────────────────────────────────────────────────────────

def test_items_route_lists_and_counts(app, client):
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "community_archive", [
            {"external_id": "1", "content": "tweet one " * 60,
             "author_handle": "x", "url": "https://t.co/1",
             "posted_at": None},
        ])
    body = client.get("/api/external/items").get_json()
    assert body["total"] == 1
    assert body["counts"] == {"community_archive": 1}
    assert body["items"][0]["preview"].endswith("…")


def test_bookmarks_json_import_shapes(app, client):
    payload = [
        {"id": "10", "text": "direct shape", "author": "a"},
        {"url": "https://twitter.com/b/status/11?s=20",
         "full_text": "url-derived id", "screen_name": "b"},
        {"text": "no id — skipped"},
        "not even a dict",
    ]
    body = client.post("/api/external/bookmarks/import",
                       json={"bookmarks": payload}).get_json()
    assert body["created"] == 2
    assert body["unrecognized"] == 2
    with app.app_context():
        ids = {i.external_id for i in ExternalItem.query.all()}
        assert ids == {"10", "11"}


def test_twitter_connect_env_gated(app, client):
    assert client.get(
        "/api/external/twitter/connect").status_code == 503
    status = client.get("/api/external/twitter/status").get_json()
    assert status == {"configured": False, "connected": False,
                      "revoked": False,
                      "handle": None, "last_synced_at": None}


def test_ca_fetch_requires_username(app, client):
    assert client.post("/api/external/community-archive/fetch",
                       json={}).status_code == 400


# ── Semantic search inclusion ────────────────────────────────────────────

def test_semantic_search_includes_external(app, client, monkeypatch):
    from backend.utils.embeddings import pack_vector, content_hash
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "twitter_bookmark", [
            {"external_id": "42", "content": "a bookmarked gem about gardens",
             "author_handle": "gardener", "url": "https://t.co/42",
             "posted_at": None},
        ])
        item = ExternalItem.query.first()
        _db.session.add(ExternalItemEmbedding(
            item_id=item.id, user_id=uid,
            model="text-embedding-3-small",
            content_hash=content_hash(item.get_content()),
            vector=pack_vector([1.0, 0.0])))
        _db.session.commit()

    import backend.utils.embeddings as emb_module
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, key, **kw: [[1.0, 0.05] for _ in texts])

    body = client.get("/api/search/semantic?q=gardening").get_json()
    assert body["total"] == 1
    result = body["results"][0]
    assert result["kind"] == "external"
    assert result["source"] == "twitter_bookmark"
    assert result["author_handle"] == "gardener"

    # Opt-out flag excludes references
    body2 = client.get(
        "/api/search/semantic?q=gardening&include_external=0").get_json()
    assert body2["results"] == []
