"""A person's public listings come a page at a time: the Atom feed holds
the newest FEED_ENTRIES pieces, /@<user> and GET /api/share/public/<user>
PUBLIC_LISTING_PAGE_SIZE per page (?page=N), and the page cache stores
no entry above MAX_ENTRY_BYTES. An account with few pieces sees what it
saw before.

Fixtures from test_public_pages (in-memory sqlite, no page cache).
"""
from datetime import datetime, timedelta

from backend.models import Node
from backend.tests.test_public_pages import (  # noqa: F401 (fixtures)
    _db, _mk_node, _publish, _user, app,
)

T0 = datetime(2026, 9, 1, 12, 0, 0)


def _pieces(n, username="author"):
    """n public pieces, piece-0 the oldest; returns them oldest first."""
    nodes = []
    for i in range(n):
        node = _mk_node(username, f"# Piece {i}\n\nBody of piece {i}.",
                        slug=f"piece-{i}")
        node.created_at = T0 + timedelta(minutes=i)
        nodes.append(node)
    _db.session.commit()
    return nodes


def _counting_reads(monkeypatch):
    reads = []
    real = Node.get_content

    def counting(self):
        reads.append(self.id)
        return real(self)
    monkeypatch.setattr(Node, "get_content", counting)
    return reads


def test_feed_holds_the_newest_entries(app, monkeypatch):
    from backend.routes.public_pages import FEED_ENTRIES
    _pieces(FEED_ENTRIES + 5)
    reads = _counting_reads(monkeypatch)
    xml = app.test_client().get("/@author/feed.xml").get_data(as_text=True)
    assert xml.count("<entry>") == FEED_ENTRIES
    assert f"<title>Piece {FEED_ENTRIES + 4}</title>" in xml
    assert "<title>Piece 4</title>" not in xml
    assert len(reads) == FEED_ENTRIES


def test_feed_of_a_few_pieces_is_unchanged(app):
    from backend.utils.timefmt import iso_utc
    nodes = _pieces(3)
    # A root without a permalink is no entry, but its change dates the feed.
    unslugged = _mk_node("author", "# Loose\n\nNo permalink.")
    unslugged.updated_at = T0 + timedelta(days=30)
    _db.session.commit()
    xml = app.test_client().get("/@author/feed.xml").get_data(as_text=True)
    assert xml.count("<entry>") == 3
    assert xml.index("Piece 2") < xml.index("Piece 1") < xml.index("Piece 0")
    newest = max(n.updated_at or n.created_at for n in nodes + [unslugged])
    assert f"<updated>{iso_utc(newest)}</updated><author>" in xml


def test_profile_pages_through_older_entries(app, monkeypatch):
    from backend.routes.commons import PUBLIC_LISTING_PAGE_SIZE as size
    _pieces(size + 3)
    pinned = Node.query.filter_by(public_slug="piece-0").one()
    pinned.pinned_at = T0 + timedelta(days=1)
    _db.session.commit()
    c = app.test_client()
    reads = _counting_reads(monkeypatch)
    first = c.get("/@author").get_data(as_text=True)
    assert len(reads) == size
    assert first.count("<article>") == size
    # Pinned first, then newest.
    assert first.index("Piece 0<") < first.index(f"Piece {size + 2}<")
    assert '<a href="https://loore.org/@author?page=2">Older entries</a>' \
        in first
    second = c.get("/@author?page=2")
    assert second.status_code == 200
    html = second.get_data(as_text=True)
    assert html.count("<article>") == 3
    assert "Piece 1<" in html and "Piece 0<" not in html
    assert "Older entries" not in html
    assert '<link rel="canonical" href="https://loore.org/@author?page=2"/>' \
        in html
    # The app shows page 1 whatever ?page says: later pages are followed,
    # not indexed. The first page stays indexable.
    assert '<meta name="robots" content="noindex, follow"/>' in html
    assert 'name="robots"' not in first
    assert c.get("/@author?page=3").status_code == 404


def test_profile_of_a_few_pieces_has_no_paging(app):
    _pieces(2)
    html = app.test_client().get("/@author").get_data(as_text=True)
    assert html.count("<article>") == 2
    assert "Older entries" not in html
    assert '<link rel="canonical" href="https://loore.org/@author"/>' in html


def test_share_listing_pages_and_reads_only_its_page(app, monkeypatch):
    from backend.routes.commons import PUBLIC_LISTING_PAGE_SIZE as size
    _pieces(size + 3)
    c = app.test_client()
    reads = _counting_reads(monkeypatch)
    body = c.get("/api/share/public/author").get_json()
    assert len(reads) == size
    assert len(body["shares"]) == size
    assert body["has_more"] is True and body["page"] == 1
    assert body["shares"][0]["content"].startswith(f"# Piece {size + 2}")
    more = c.get("/api/share/public/author?page=2").get_json()
    assert [s["content"].split("\n")[0] for s in more["shares"]] == [
        "# Piece 2", "# Piece 1", "# Piece 0"]
    assert more["has_more"] is False
    ids = {s["id"] for s in body["shares"]} | {s["id"] for s in more["shares"]}
    assert len(ids) == size + 3


def test_share_listing_of_a_few_pieces(app):
    _pieces(2)
    body = app.test_client().get("/api/share/public/author").get_json()
    assert len(body["shares"]) == 2
    assert body["has_more"] is False


def test_malformed_page_reads_as_the_first(app):
    _pieces(2)
    c = app.test_client()
    for q in ("?page=0", "?page=-3", "?page=x"):
        assert c.get(f"/@author{q}").status_code == 200
        assert len(c.get(f"/api/share/public/author{q}")
                   .get_json()["shares"]) == 2


def test_case_variant_feed_redirects_without_reading_pieces(app, monkeypatch):
    _pieces(3)
    reads = _counting_reads(monkeypatch)
    r = app.test_client().get("/@AUTHOR/feed.xml")
    assert r.status_code == 301
    assert reads == []


def test_page_cache_stores_no_oversized_entry(app, monkeypatch):
    from backend.utils import public_cache

    class FakeRedis:
        def __init__(self):
            self.store = {}

        def setex(self, key, ttl, value):
            self.store[key] = value

    fake = FakeRedis()
    monkeypatch.setattr(public_cache, "_redis", lambda: fake)
    monkeypatch.setattr(public_cache, "MAX_ENTRY_BYTES", 1000)
    public_cache.put("/small", 200, "text/html", "x" * 100)
    public_cache.put("/large", 200, "text/html", "x" * 2000)
    assert "public_html:/small" in fake.store
    assert "public_html:/large" not in fake.store
