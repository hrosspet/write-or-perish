"""Tests for quote-as-response building blocks (#208).

Covers: {quote_ext:ID} resolution (owner-only, LLM + human formats), the
resolve-quotes endpoint's external payload, v3 bookmark-export enrichment
folding in the JSON import, and the early-stop page loop in the bookmark
sync (credit saving). No network — all local.
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
from backend.models import User, ExternalItem  # noqa: E402
from backend.utils.quotes import (  # noqa: E402
    find_ext_quote_ids, has_ext_quotes, resolve_ext_quotes,
    get_ext_quote_data,
)

# Glue-import the sync helpers (identity celery, like test_external_content)
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
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.external import external_bp
    app.register_blueprint(external_bp, url_prefix="/api/external")

    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        other = User(username="rival")
        _db.session.add_all([user, other])
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.filter_by(username="tester").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _mk_item(user_id, content, author="visa", source="twitter_bookmark",
             ext_id=None):
    item = ExternalItem(
        user_id=user_id, source=source,
        external_id=ext_id or f"e{abs(hash(content)) % 10**9}",
        author_handle=author,
        url="https://twitter.com/visa/status/1",
    )
    item.set_content(content)
    _db.session.add(item)
    _db.session.commit()
    return item


# ── {quote_ext:ID} parsing + resolution ──────────────────────────────────

def test_find_and_has_ext_quotes():
    assert find_ext_quote_ids("a {quote_ext:12} b {quote_ext:9}") == [12, 9]
    assert find_ext_quote_ids("plain {quote:5} only") == []
    assert has_ext_quotes("{quote_ext:1}")
    assert not has_ext_quotes("{quote:1}")
    assert not has_ext_quotes(None)


def test_resolve_ext_quotes_llm_and_human_formats(app):
    uid = User.query.filter_by(username="tester").first().id
    item = _mk_item(uid, "the saved tweet text")

    llm_text, ids = resolve_ext_quotes(
        f"look: {{quote_ext:{item.id}}}", uid, for_llm=True)
    assert ids == [item.id]
    assert "<quoted_reference" in llm_text
    assert "the saved tweet text" in llm_text
    assert '@visa' in llm_text

    human_text, _ = resolve_ext_quotes(
        f"look: {{quote_ext:{item.id}}}", uid, for_llm=False)
    assert "Saved reference from @visa" in human_text
    assert "the saved tweet text" in human_text


def test_resolve_ext_quotes_owner_only(app):
    rival_id = User.query.filter_by(username="rival").first().id
    tester_id = User.query.filter_by(username="tester").first().id
    item = _mk_item(rival_id, "rival's private bookmark")

    text, ids = resolve_ext_quotes(
        f"{{quote_ext:{item.id}}}", tester_id, for_llm=True)
    assert ids == []
    assert "inaccessible" in text
    assert "rival's private bookmark" not in text

    data = get_ext_quote_data([item.id], tester_id)
    assert data[item.id] is None


def test_resolve_ext_quotes_missing_item(app):
    uid = User.query.filter_by(username="tester").first().id
    text, ids = resolve_ext_quotes("{quote_ext:99999}", uid)
    assert ids == []
    assert "inaccessible" in text


# ── JSON import: v3 exporter enrichment folding ──────────────────────────

def test_import_folds_quoted_card_media_links(app, client):
    payload = [{
        "id": "111",
        "text": "must read this https://t.co/abc123",
        "author": "alice",
        "created_at": "2026-06-01T10:00:00.000Z",
        "url": "https://twitter.com/alice/status/111",
        "links": ["https://example.com/essay"],
        "media": [{"type": "photo",
                   "url": "https://pbs.twimg.com/media/x.jpg",
                   "alt": "a diagram of feedback loops"}],
        "card": {"title": "The Essay", "description": "On feedback",
                 "domain": "example.com"},
        "quoted": {"author": "bob", "text": "original insight here"},
    }]
    resp = client.post("/api/external/bookmarks/import", json=payload)
    assert resp.status_code == 200
    assert resp.get_json()["created"] == 1

    item = ExternalItem.query.filter_by(external_id="111").first()
    content = item.get_content()
    # Trailing media t.co stub stripped; enrichment folded in.
    assert "t.co/abc123" not in content
    assert "[Quoting @bob: original insight here]" in content
    assert "[Link: The Essay — On feedback (example.com)]" in content
    assert "[photo: a diagram of feedback loops]" in content
    assert "https://example.com/essay" in content


def test_import_media_only_bookmark_survives(app, client):
    # No text beyond the media stub — quoted/media carry the meaning.
    payload = [{
        "id": "222",
        "text": "https://t.co/onlymedia",
        "author": "carol",
        "media": [{"type": "photo", "url": "https://pbs/x.jpg",
                   "alt": None}],
    }]
    resp = client.post("/api/external/bookmarks/import", json=payload)
    assert resp.get_json()["created"] == 1
    item = ExternalItem.query.filter_by(external_id="222").first()
    assert "[photo]" in item.get_content()


def test_import_plain_v2_shape_still_works(app, client):
    payload = [{"id": "333", "text": "plain old bookmark",
                "author": "dan"}]
    resp = client.post("/api/external/bookmarks/import", json=payload)
    assert resp.get_json()["created"] == 1
    item = ExternalItem.query.filter_by(external_id="333").first()
    assert item.get_content() == "plain old bookmark"


# ── Early-stop page sync (credit saving) ─────────────────────────────────

def test_page_loop_stops_after_stale_page(app):
    """The sync consumes pages only until one yields nothing new — the
    remaining generator pages (paid API requests) are never pulled."""
    uid = User.query.filter_by(username="tester").first().id
    # Pre-import "old" items = the second page.
    old_page = [{"external_id": f"old{i}", "content": f"old {i}",
                 "author_handle": "x", "url": None, "posted_at": None}
                for i in range(3)]
    _upsert_items(uid, "twitter_bookmark", old_page)

    pulled = []

    def pages():
        new_page = [{"external_id": f"new{i}", "content": f"new {i}",
                     "author_handle": "x", "url": None, "posted_at": None}
                    for i in range(3)]
        for name, page in [("new", new_page), ("old", old_page),
                           ("never", old_page)]:
            pulled.append(name)
            yield page

    # Mirror of the sync task's loop.
    created = skipped = 0
    for page in pages():
        c, s = _upsert_items(uid, "twitter_bookmark", page)
        created += c
        skipped += s
        if c == 0:
            break

    assert created == 3
    assert pulled == ["new", "old"]  # third page never fetched
