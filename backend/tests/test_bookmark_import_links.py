"""The X bookmark import keeps an entry's url only when it is an http(s)
link to X (twitter.com / x.com) that fits the column; otherwise the item
gets the tweet's link built from its id. A quoted reference offers its
url to open only when it is an http(s) link.

Fixtures from test_reference_log (in-memory sqlite, alice signed in).
"""
import sys
from unittest.mock import MagicMock

import pytest
from flask import g

from backend.extensions import db as _db
from backend.models import ExternalItem, Node
from backend.tests.test_reference_log import (  # noqa: F401 (fixtures)
    _user, app, client,
)


def _login_as(test_client, user):
    g.pop("_login_user", None)
    with test_client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _import(client, entries):
    # The route imports backend.tasks.external_sync at call time; that
    # module imports the celery app, which the tests replace.
    saved = sys.modules.get("backend.celery_app")
    sys.modules["backend.celery_app"] = MagicMock()
    sys.modules.pop("backend.tasks.external_sync", None)
    try:
        return client.post("/api/external/bookmarks/import", json=entries)
    finally:
        sys.modules.pop("backend.tasks.external_sync", None)
        if saved is None:
            sys.modules.pop("backend.celery_app", None)
        else:
            sys.modules["backend.celery_app"] = saved


@pytest.mark.parametrize("url", [
    "javascript:alert(document.domain)",
    "https://twitter.com.elsewhere.example/i/status/1",
    "https://x.com@elsewhere.example/i/status/1",
    "ftp://x.com/i/status/1",
    "https://x.com/" + "a" * 600,
    12345,
    ["https://x.com/i/status/1"],
])
def test_other_urls_are_replaced_by_the_tweets_link(app, client, url):
    resp = _import(client, [{"id": "1", "text": "x", "author": "jack",
                             "url": url}])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert ExternalItem.query.one().url == "https://twitter.com/i/status/1"


@pytest.mark.parametrize("url", [
    "https://x.com/jack/status/1",
    "https://twitter.com/jack/status/1?s=20",
    "http://mobile.twitter.com/jack/status/1",
    "https://www.x.com/jack/status/1",
])
def test_links_to_x_are_kept(app, client, url):
    resp = _import(client, [{"id": "1", "text": "x", "author": "jack",
                             "url": url}])
    assert resp.status_code == 200
    assert ExternalItem.query.one().url == url


def test_id_from_the_url_still_works(app, client):
    resp = _import(client, [{"text": "x", "author": "jack",
                             "url": "https://x.com/jack/status/77?s=1"}])
    assert resp.status_code == 200
    item = ExternalItem.query.one()
    assert item.external_id == "77"
    assert item.url == "https://x.com/jack/status/77?s=1"


def test_a_stored_non_web_url_is_not_offered_to_other_viewers(app, client):
    alice = _user("alice")
    item = ExternalItem(user_id=alice.id, source="twitter_bookmark",
                        external_id="1", author_handle="jack",
                        url="javascript:alert(1)")
    item.set_content("x")
    web = ExternalItem(user_id=alice.id, source="web_clip",
                       external_id="2", url="https://example.org/a")
    web.set_content("y")
    _db.session.add_all([item, web])
    _db.session.commit()
    node = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
                privacy_level="public")
    node.set_content("look {quote_ext:%d} and {quote_ext:%d}"
                     % (item.id, web.id))
    _db.session.add(node)
    _db.session.commit()

    _login_as(client, _user("bob"))
    resp = client.get(f"/api/nodes/{node.id}/resolve-quotes")
    assert resp.status_code == 200
    quotes = resp.get_json()["external_quotes"]
    assert quotes[str(item.id)]["url"] is None
    assert quotes[str(item.id)]["author_handle"] == "jack"
    assert quotes[str(web.id)]["url"] == "https://example.org/a"
