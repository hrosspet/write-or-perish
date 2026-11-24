"""One page cache entry per public page: /node/<id> written with leading
zeros redirects to the id's own URL before the cache, and the 410 of a
tombstone is cached under the handle's own spelling only.

Runs the real public_cache code against an in-memory stand-in for Redis.
"""
from datetime import datetime

import pytest

from backend.tests.test_public_pages import (  # noqa: F401 (fixtures)
    ARTICLE, _db, _mk_node, _publish, app,
)


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    from backend.utils import public_cache
    fake = FakeRedis()
    monkeypatch.setattr(public_cache, "_redis", lambda: fake)
    return fake


def _node_keys(fake):
    return sorted(k for k in fake.store if k.startswith("public_html:/node/"))


def test_zero_padded_node_url_redirects_and_adds_no_entry(app, fake_redis):
    root = _publish("author", "# Title\n\nTEXT-OF-THE-PIECE", "title")
    c = app.test_client()
    assert c.get(f"/node/{root.id}").status_code == 200
    for zeros in range(1, 6):
        r = c.get(f"/node/{'0' * zeros}{root.id}?x=1")
        assert r.status_code == 301
        assert r.headers["Location"].endswith(f"/node/{root.id}?x=1")
        assert r.headers["Cache-Control"] == "no-store"
    assert _node_keys(fake_redis) == [f"public_html:/node/{root.id}"]


def test_takedown_reaches_every_spelling(app, fake_redis):
    from backend.utils import public_cache
    root = _publish("author", "# Title\n\nTEXT-OF-THE-PIECE", "title")
    c = app.test_client()
    c.get(f"/node/{root.id}")
    c.get(f"/node/00{root.id}")
    root.privacy_level = "private"
    root.public_slug = None
    _db.session.commit()
    public_cache.invalidate_for_node(root)
    assert c.get(f"/node/{root.id}").status_code == 404
    padded = c.get(f"/node/00{root.id}", follow_redirects=True)
    assert padded.status_code == 404
    assert "TEXT-OF-THE-PIECE" not in padded.get_data(as_text=True)


def test_tombstone_410_is_cached_under_the_handles_own_spelling(
        app, fake_redis):
    node = _publish("author", ARTICLE, "on-lore")
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    c = app.test_client()
    for url in ("/@author/on-lore", "/@Author/on-lore", "/@AUTHOR/on-lore",
                "/@author/on-lore.md", "/@AuThOr/on-lore.md"):
        assert c.get(url).status_code == 410
    article_keys = sorted(k for k in fake_redis.store if "/on-lore" in k)
    assert article_keys == ["public_html:/@author/on-lore",
                            "public_html:/@author/on-lore.md"]
