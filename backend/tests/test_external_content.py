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
        # Opted into external content (Account toggle): the clipper's
        # token minting is gated on it.
        user = User(username="tester", external_content_enabled=True)
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


# ── Web clips + personal API tokens (#232) ───────────────────────────────

from backend.models import ApiToken  # noqa: E402
from backend.utils.web_clip import (  # noqa: E402
    canonical_url, classify_clip, tweet_id_from_url,
)


def test_canonical_url_strips_noise_and_keeps_meaning():
    a = canonical_url("HTTPS://Example.com:443/Post/?utm_source=x&b=1#frag")
    assert a == "https://example.com/Post?b=1"
    assert canonical_url("https://example.com") == "https://example.com/"
    assert canonical_url("https://e.com/a?fbclid=1") == "https://e.com/a"
    # Real query params survive, in order
    assert canonical_url("https://e.com/a?z=1&a=2") == "https://e.com/a?z=1&a=2"


def test_classify_clip_routes_tweets_to_bookmarks():
    assert tweet_id_from_url("https://x.com/alice/status/123?s=20") == "123"
    assert tweet_id_from_url("https://twitter.com/i/web/status/9") == "9"
    assert tweet_id_from_url("https://example.com/a/status/1") is None
    source, ext_id, canon = classify_clip("https://x.com/alice/status/123")
    assert (source, ext_id) == ("twitter_bookmark", "123")
    assert canon == "https://x.com/i/status/123"
    source, ext_id, _ = classify_clip("https://example.com/post")
    assert source == "web_clip" and len(ext_id) == 64


def test_clip_session_creates_then_dedupes(app, client):
    body = client.post("/api/external/clip", json={
        "url": "https://example.com/post/?utm_source=tw",
        "title": "A post", "content": "# A post\n\nBody text.",
        "author": "@writer",
    })
    assert body.status_code == 201
    data = body.get_json()
    assert data["created"] is True and data["source"] == "web_clip"
    # Same page spelled differently is the same item
    again = client.post("/api/external/clip", json={
        "url": "https://example.com/post#top", "content": "whatever",
    })
    assert again.status_code == 200
    assert again.get_json() == {
        "created": False, "id": data["id"], "source": "web_clip",
        "title": "A post"}
    with app.app_context():
        item = ExternalItem.query.get(data["id"])
        assert item.url == "https://example.com/post"
        assert item.author_handle == "writer"
        assert item.title == "A post"
    listed = client.get("/api/external/items").get_json()
    assert listed["counts"] == {"web_clip": 1}
    assert listed["items"][0]["title"] == "A post"


def test_clip_tweet_url_lands_as_bookmark(app, client):
    body = client.post("/api/external/clip", json={
        "url": "https://x.com/alice/status/555?s=20",
        "content": "the tweet", "author": "alice",
        "posted_at": "2025-01-02T03:04:05.000Z",
    }).get_json()
    assert body["source"] == "twitter_bookmark"
    with app.app_context():
        item = ExternalItem.query.get(body["id"])
        assert item.external_id == "555"
        assert item.posted_at.year == 2025
        # The nightly sync would now skip it: same (user, source, ext_id)
        uid = User.query.first().id
        created, skipped = _upsert_items(uid, "twitter_bookmark", [
            {"external_id": "555", "content": "x", "author_handle": "alice",
             "url": None, "posted_at": None}])
        assert (created, skipped) == (0, 1)


def test_clip_validates_and_truncates(app, client):
    from backend.utils.node_split import NODE_CHAR_CAP
    assert client.post("/api/external/clip", json={
        "url": "ftp://nope", "content": "x"}).status_code == 400
    assert client.post("/api/external/clip", json={
        "url": "https://e.com", "content": "   "}).status_code == 400
    body = client.post("/api/external/clip", json={
        "url": "https://e.com/long", "content": "a" * (NODE_CHAR_CAP + 5),
    }).get_json()
    assert body["truncated"] is True
    with app.app_context():
        assert len(ExternalItem.query.get(body["id"]).get_content()) == NODE_CHAR_CAP


def _mint(client):
    res = client.post("/api/external/tokens", json={"name": "test clipper"})
    assert res.status_code == 201
    return res.get_json()


def _no_session(app, method, path, **kw):
    """A request from a client that has no session cookie.

    The ``app`` fixture holds one app context open for the whole test and
    flask-login caches the loaded user on ``g`` per app context, so a
    plain ``app.test_client()`` call after any logged-in request would
    silently reuse the fixture user. A nested app context gives each call
    a fresh ``g`` — which is what production has per request anyway.
    """
    with app.app_context():
        return getattr(app.test_client(), method)(path, **kw)


def test_tokens_lifecycle_and_bearer_auth(app, client):
    with app.app_context():
        user = User.query.first()
        user.approved = True
        _db.session.commit()
    minted = _mint(client)
    plaintext = minted["token"]
    assert plaintext.startswith("loore_") and minted["prefix"] == plaintext[6:14]
    # Only the hash is stored; the listing never returns the plaintext
    with app.app_context():
        row = ApiToken.query.get(minted["id"])
        assert row.token_hash != plaintext and plaintext not in row.token_hash
    listing = client.get("/api/external/tokens").get_json()["tokens"]
    assert [t["id"] for t in listing] == [minted["id"]]
    assert "token" not in listing[0]

    # Bearer auth with no session at all
    headers = {"Authorization": f"Bearer {plaintext}"}
    status = _no_session(app, "get", "/api/external/clip/status",
                         headers=headers)
    assert status.status_code == 200
    assert status.get_json()["token_name"] == "test clipper"
    res = _no_session(app, "post", "/api/external/clip", headers=headers,
                      json={"url": "https://e.com/via-token", "content": "hi"})
    assert res.status_code == 201
    with app.app_context():
        assert ApiToken.query.get(minted["id"]).last_used_at is not None

    # Wrong / absent bearer
    assert _no_session(app, "post", "/api/external/clip", json={
        "url": "https://e.com/x", "content": "hi"}).status_code == 401
    bad = _no_session(app, "post", "/api/external/clip",
                      headers={"Authorization": "Bearer loore_nope"},
                      json={"url": "https://e.com/x", "content": "hi"})
    assert bad.status_code == 401 and bad.get_json()["error"] == "invalid_token"

    # A bad bearer is refused even with a live session (no silent fallback)
    with_session = client.post(
        "/api/external/clip", headers={"Authorization": "Bearer loore_nope"},
        json={"url": "https://e.com/y", "content": "hi"})
    assert with_session.status_code == 401

    # Revocation: token dies, listing forgets it, second revoke is idempotent
    assert client.delete(f"/api/external/tokens/{minted['id']}").status_code == 200
    assert client.get("/api/external/tokens").get_json()["tokens"] == []
    assert _no_session(app, "get", "/api/external/clip/status",
                       headers=headers).status_code == 401
    assert client.delete(f"/api/external/tokens/{minted['id']}").status_code == 200
    assert client.delete("/api/external/tokens/999").status_code == 404


def test_token_of_unapproved_user_is_refused(app, client):
    minted = _mint(client)  # fixture user is not approved
    res = _no_session(app, "get", "/api/external/clip/status",
                      headers={"Authorization": f"Bearer {minted['token']}"})
    assert res.status_code == 401


def test_token_routes_need_a_session(app):
    assert _no_session(app, "get", "/api/external/tokens").status_code == 401
    assert _no_session(app, "post", "/api/external/tokens",
                       json={}).status_code == 401


def test_token_minting_needs_external_content_opt_in(app, client):
    # Each request runs in its own nested app context so flask-login
    # reloads the user (see _no_session) and sees the flag change.
    def _set(enabled):
        with app.app_context():
            user = User.query.first()
            user.external_content_enabled = enabled
            _db.session.commit()

    def _mint_status():
        with app.app_context():
            return client.post("/api/external/tokens", json={}).status_code

    _set(False)
    assert _mint_status() == 403
    _set(True)
    assert _mint_status() == 201
