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

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, ExternalItem, ExternalItemEmbedding, TTSChunk,
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
    from backend.routes.sse import sse_bp
    app.register_blueprint(external_bp, url_prefix="/api/external")
    app.register_blueprint(search_bp, url_prefix="/api")
    app.register_blueprint(sse_bp, url_prefix="/api/sse")

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
                      "revoked": False, "handle": None,
                      "last_synced_at": None, "last_sync_created": None}


def test_twitter_reconnect_clears_disconnected_notice(app, client,
                                                       monkeypatch):
    """Reconnecting answers the "X disconnected" notice — it must not keep
    surfacing in the updates window after the user already fixed it."""
    from backend.models import ExternalAccount, UserNotification
    from backend.routes import external as ext
    app.config["X_CLIENT_ID"] = "client-id"
    app.config["X_REDIRECT_URI"] = "http://localhost/cb"
    uid = User.query.first().id
    account = ExternalAccount(user_id=uid, provider="twitter",
                              revoked_at=datetime.utcnow())
    account.set_tokens("old", "old")
    _db.session.add(account)
    _db.session.add(UserNotification(
        user_id=uid, type="x_disconnected", title="X disconnected",
        link="/import#x-bookmarks"))
    _db.session.commit()

    def fake_post(url, **kw):
        r = MagicMock()
        r.json.return_value = {"access_token": "new", "refresh_token": "r",
                               "expires_in": 7200}
        return r

    def fake_get(url, **kw):
        r = MagicMock()
        r.json.return_value = {"data": {"id": "42", "username": "tester"}}
        return r
    monkeypatch.setattr(ext.requests, "post", fake_post)
    monkeypatch.setattr(ext.requests, "get", fake_get)
    with client.session_transaction() as sess:
        sess["x_oauth"] = {"verifier": "v", "state": "s"}

    resp = client.get("/api/external/twitter/callback?state=s&code=c")
    assert resp.status_code == 302 and "x_connect=ok" in resp.location
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None
    notice = UserNotification.query.filter_by(
        user_id=uid, type="x_disconnected").one()
    assert notice.status == "read" and notice.read_at is not None


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
        "created": False, "updated": False, "truncated": False,
        "id": data["id"], "source": "web_clip", "title": "A post"}
    with app.app_context():
        item = ExternalItem.query.get(data["id"])
        assert item.url == "https://example.com/post"
        assert item.author_handle == "writer"
        assert item.title == "A post"
    listed = client.get("/api/external/items").get_json()
    assert listed["counts"] == {"web_clip": 1}
    assert listed["items"][0]["title"] == "A post"


def test_clip_upgrades_stored_text_when_longer(app, client):
    short = "What happens if Claude thinks you are Amanda?\n\n(See https://t.co/x"
    long = short + "\n\nI couldn't jailbreak with it, but: would this get me " \
        "different responses than anyone else?\n\nMain thread below!"
    with app.app_context():
        uid = User.query.first().id
        # The nightly X sync stored the API's truncated text, no author.
        item = ExternalItem(user_id=uid, source="twitter_bookmark",
                            external_id="777", url="https://x.com/i/status/777")
        item.set_content(short)
        _db.session.add(item)
        _db.session.flush()
        _db.session.add(ExternalItemEmbedding(
            item_id=item.id, user_id=uid, model="m", content_hash="h",
            vector=b"\x00"))
        _db.session.commit()
        item_id = item.id
    # A shorter or equal re-clip is the no-op it always was.
    again = client.post("/api/external/clip", json={
        "url": "https://x.com/fjzzq2002/status/777", "content": "  " + short})
    assert again.status_code == 200
    body = again.get_json()
    assert body["created"] is False and body["updated"] is False
    with app.app_context():
        assert ExternalItem.query.get(item_id).get_content() == short
        assert ExternalItemEmbedding.query.filter_by(item_id=item_id).count() == 1
    # The extension reads the full rendered post: longer text replaces
    # the stored copy, fills missing metadata, and drops the embedding
    # so the sweep re-embeds.
    up = client.post("/api/external/clip", json={
        "url": "https://x.com/fjzzq2002/status/777", "content": long,
        "author": "@fjzzq2002", "posted_at": "2026-08-06T22:30:24.000Z",
    })
    assert up.status_code == 200
    body = up.get_json()
    assert body["created"] is False and body["updated"] is True
    assert body["id"] == item_id
    with app.app_context():
        item = ExternalItem.query.get(item_id)
        assert item.get_content() == long
        assert item.author_handle == "fjzzq2002"
        assert item.posted_at.year == 2026
        assert ExternalItemEmbedding.query.filter_by(item_id=item_id).count() == 0
    listed = client.get("/api/external/items").get_json()
    assert listed["counts"] == {"twitter_bookmark": 1}


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


def test_item_detail_delete_and_saved_order(app, client):
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "web_clip", [
            {"external_id": "a" * 64, "content": "# Old\n\nfirst saved",
             "title": "Old", "author_handle": "example.com",
             "url": "https://example.com/old",
             "posted_at": datetime(2026, 5, 1)},
            {"external_id": "b" * 64, "content": "# New\n\nsaved later",
             "title": "New", "author_handle": "example.com",
             "url": "https://example.com/new",
             "posted_at": datetime(2026, 1, 1)},
        ])
        old_id, new_id = [i.id for i in ExternalItem.query.order_by(
            ExternalItem.id).all()]
        _db.session.add(ExternalItemEmbedding(
            item_id=new_id, user_id=uid, model="m", content_hash="h",
            vector=b""))
        _db.session.commit()

    # Default order is by posted_at (Old posted later → first); the
    # references list asks for saved order (New saved later → first).
    default = client.get("/api/external/items").get_json()["items"]
    assert [i["id"] for i in default] == [old_id, new_id]
    saved = client.get("/api/external/items?sort=saved").get_json()["items"]
    assert [i["id"] for i in saved] == [new_id, old_id]

    detail = client.get(f"/api/external/items/{new_id}").get_json()
    assert detail["title"] == "New"
    assert detail["content"] == "# New\n\nsaved later"

    assert client.delete(f"/api/external/items/{new_id}").status_code == 200
    assert client.get(f"/api/external/items/{new_id}").status_code == 404
    assert client.delete(f"/api/external/items/{new_id}").status_code == 404
    with app.app_context():
        assert ExternalItemEmbedding.query.filter_by(item_id=new_id).count() == 0
        assert ExternalItem.query.count() == 1


def test_item_routes_are_owner_only(app, client):
    with app.app_context():
        other = User(username="other")
        _db.session.add(other)
        _db.session.commit()
        _upsert_items(other.id, "web_clip", [
            {"external_id": "c" * 64, "content": "theirs", "title": None,
             "author_handle": None, "url": None, "posted_at": None}])
        their_id = ExternalItem.query.filter_by(user_id=other.id).one().id
    assert client.get(f"/api/external/items/{their_id}").status_code == 404
    assert client.delete(f"/api/external/items/{their_id}").status_code == 404
    with app.app_context():
        assert ExternalItem.query.get(their_id) is not None


def test_mark_read_is_explicit_idempotent_and_reversible(app, client):
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "web_clip", [
            {"external_id": "r" * 64, "content": "to read", "title": "T",
             "author_handle": None, "url": None, "posted_at": None}])
        item_id = ExternalItem.query.one().id

    # Opening the page does not mark it read.
    assert client.get(f"/api/external/items/{item_id}").get_json()["read_at"] is None

    first = client.post(f"/api/external/items/{item_id}/read").get_json()
    assert first["read_at"] is not None
    # Marking again keeps the original timestamp.
    again = client.post(f"/api/external/items/{item_id}/read").get_json()
    assert again["read_at"] == first["read_at"]
    listed = client.get("/api/external/items").get_json()["items"][0]
    assert listed["read_at"] == first["read_at"]
    assert listed["surfaced_count"] == 0 and listed["last_surfaced_at"] is None

    assert client.delete(f"/api/external/items/{item_id}/read").get_json()["read_at"] is None
    assert client.post("/api/external/items/999/read").status_code == 404


def test_update_item_edits_title_and_text_and_drops_embedding(app, client):
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "web_clip", [
            {"external_id": "e" * 64, "content": "first draft",
             "title": "Old title", "author_handle": None, "url": None,
             "posted_at": None}])
        item = ExternalItem.query.one()
        item_id = item.id
        _db.session.add(ExternalItemEmbedding(
            item_id=item_id, user_id=uid, model="m",
            content_hash="h" * 64, vector=b"\x00" * 8))
        _db.session.commit()

    res = client.put(f"/api/external/items/{item_id}",
                     json={"title": "  New title  ", "content": "rewritten\n"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["title"] == "New title"
    assert body["content"] == "rewritten"
    assert body["edited_at"] is not None
    with app.app_context():
        assert ExternalItemEmbedding.query.filter_by(item_id=item_id).count() == 0
        assert ExternalItem.query.get(item_id).get_content() == "rewritten"

    # An empty title clears it; the text is untouched.
    res = client.put(f"/api/external/items/{item_id}", json={"title": ""})
    assert res.status_code == 200 and res.get_json()["title"] is None
    assert res.get_json()["content"] == "rewritten"

    # Validation: nothing to change, empty text, over the cap, not mine.
    assert client.put(f"/api/external/items/{item_id}", json={}).status_code == 400
    assert client.put(f"/api/external/items/{item_id}",
                      json={"content": "   "}).status_code == 400
    assert client.put(f"/api/external/items/{item_id}",
                      json={"content": "x" * 100001}).status_code == 422
    assert client.put("/api/external/items/999",
                      json={"content": "x"}).status_code == 404


def test_reclip_keeps_a_hand_edited_reference(app, client):
    with app.app_context():
        User.query.first().approved = True
        _db.session.commit()
    url = "https://example.com/edited"
    first = client.post("/api/external/clip",
                        json={"url": url, "content": "short capture"})
    assert first.status_code == 201
    item_id = first.get_json()["id"]

    assert client.put(f"/api/external/items/{item_id}",
                      json={"content": "my own notes"}).status_code == 200

    # A longer re-clip would normally replace the text; not after an edit.
    again = client.post("/api/external/clip",
                        json={"url": url, "content": "a much longer capture of the page"})
    assert again.status_code == 200
    assert again.get_json()["updated"] is False
    assert client.get(f"/api/external/items/{item_id}").get_json()["content"] == "my own notes"


def test_reference_tts_routes_queue_report_and_clear(app, client, monkeypatch):
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "web_clip", [
            {"external_id": "t" * 64, "content": "# Title\n\nSpoken text",
             "title": "Title", "author_handle": None, "url": None,
             "posted_at": None}])
        item_id = ExternalItem.query.one().id

    # Nothing yet.
    assert client.get(f"/api/external/items/{item_id}/audio").status_code == 404
    assert client.get(f"/api/external/items/{item_id}").get_json()["has_tts"] is False

    # Queue: the task is dispatched with the item id and the caller's id.
    # Module objects, not dotted strings: another test's fixture swaps
    # sys.modules entries and a string path then fails to resolve.
    calls = []
    import backend.tasks.tts as tts_mod
    import backend.routes.external as ext_mod
    from backend.celery_app import celery as celery_app
    monkeypatch.setattr(tts_mod.generate_tts_audio_for_item, "delay",
                        lambda *a, **kw: (calls.append((a, kw)) or MagicMock(id="task-1")))
    monkeypatch.setattr(ext_mod, "get_openai_chat_key", lambda cfg: "k")
    res = client.post(f"/api/external/items/{item_id}/tts")
    assert res.status_code == 202, res.get_json()
    assert calls and calls[0][0][0] == item_id and calls[0][1]["requesting_user_id"] == uid
    # In progress: audio says generating, a second POST does not re-queue.
    assert client.get(f"/api/external/items/{item_id}/audio").status_code == 202
    assert client.post(f"/api/external/items/{item_id}/tts").status_code == 202
    assert len(calls) == 1

    # The worker finished: chunk rows + final URL, as the task writes them.
    with app.app_context():
        item = ExternalItem.query.get(item_id)
        item.audio_tts_url = "/media/user/1/item/1/tts.mp3?v=9"
        item.tts_task_status = "completed"
        item.tts_task_progress = 100
        _db.session.add_all([
            TTSChunk(item_id=item_id, chunk_index=0, status="completed",
                     section_index=0, section_title="Title", duration=3.0),
            TTSChunk(item_id=item_id, chunk_index=1, status="completed",
                     section_index=1, section_title="Second", duration=2.0),
        ])
        _db.session.commit()
    monkeypatch.setattr(celery_app, "AsyncResult",
                        lambda tid: MagicMock(state="SUCCESS"))
    status = client.get(f"/api/external/items/{item_id}/tts-status").get_json()
    assert status["status"] == "completed"
    assert status["item"]["audio_tts_url"].endswith("tts.mp3?v=9")
    assert client.get(f"/api/external/items/{item_id}/audio").get_json()["tts_url"]
    assert client.post(f"/api/external/items/{item_id}/tts").status_code == 200
    chapters = client.get(f"/api/external/items/{item_id}/tts-chapters").get_json()["chapters"]
    assert [c["start_time"] for c in chapters] == [0.0, 3.0]
    assert client.get(f"/api/external/items/{item_id}").get_json()["has_tts"] is True

    # An edit that keeps the audio leaves it; one that regenerates clears
    # the URL and the chunk rows.
    client.put(f"/api/external/items/{item_id}", json={"content": "changed once"})
    assert client.get(f"/api/external/items/{item_id}").get_json()["has_tts"] is True
    client.put(f"/api/external/items/{item_id}",
               json={"content": "changed twice", "regenerate_tts": True})
    assert client.get(f"/api/external/items/{item_id}").get_json()["has_tts"] is False
    with app.app_context():
        assert TTSChunk.query.filter_by(item_id=item_id).count() == 0

    # Deleting the reference takes its chunk rows along.
    with app.app_context():
        _db.session.add(TTSChunk(item_id=item_id, chunk_index=0, status="completed"))
        _db.session.commit()
    assert client.delete(f"/api/external/items/{item_id}").status_code == 200
    with app.app_context():
        assert TTSChunk.query.filter_by(item_id=item_id).count() == 0


def test_reference_tts_stream_gates_on_ownership_and_state(app, client):
    with app.app_context():
        uid = User.query.first().id
        _upsert_items(uid, "web_clip", [
            {"external_id": "s" * 64, "content": "text", "title": None,
             "author_handle": None, "url": None, "posted_at": None}])
        item_id = ExternalItem.query.one().id
    # Not in progress and no audio: 400, not a hanging stream.
    assert client.get(f"/api/sse/items/{item_id}/tts-stream").status_code == 400
    with app.app_context():
        item = ExternalItem.query.get(item_id)
        item.audio_tts_url = "/media/x.mp3"
        _db.session.commit()
    assert client.get(f"/api/sse/items/{item_id}/tts-stream").get_json()["status"] == "completed"
    assert client.get("/api/sse/items/999/tts-stream").status_code == 404
