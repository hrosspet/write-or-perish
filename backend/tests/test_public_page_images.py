"""Images on the server-rendered public pages load only from Loore's own
media (/media/...), the rule the web app applies; any other image is
shown as its alt text.

Fixtures from test_public_pages (PUBLIC_BASE_URL https://loore.org).
"""
import re

import pytest

from backend.tests.test_public_pages import (  # noqa: F401 (fixtures)
    ARTICLE, _mk_node, _publish, app,
)

OTHER_TARGETS = [
    "/auth/logout",
    "/auth/magic-link/verify?token=abc",
    "/api/export/threads",
    "/media/../auth/logout",
    "/media/./a.png",
    "/media/%2e%2e/auth/logout",
    "//elsewhere.example/media/a.png",
    "https://elsewhere.example/a.png",
    "http://loore.org.elsewhere.example/media/a.png",
    "data:image/png;base64,AAAA",
    "media/a.png",
    "javascript:void",
]


@pytest.mark.parametrize("src", OTHER_TARGETS)
def test_image_outside_own_media_shows_its_alt_text(app, src):
    from backend.utils.public_html import render_markdown
    html = render_markdown(f"before ![the *alt* text]({src}) after")
    assert "<img" not in html
    assert "before the <em>alt</em> text after" not in html
    assert "before the alt text after" in html


@pytest.mark.parametrize("src", [
    "/media/nodes/1/2/photo.png",
    "/media/nodes/1/2/photo.png?v=2",
    "https://loore.org/media/nodes/1/2/photo.png",
])
def test_own_media_image_loads(app, src):
    from backend.utils.public_html import render_markdown
    html = render_markdown(f"![a photo]({src})")
    assert f'<img src="{src}" alt="a photo" />' in html


def test_reply_images_on_public_pages_request_nothing_else(app):
    root = _publish("author", ARTICLE, "on-lore")
    _mk_node("visitor", "nice piece ![](/auth/logout) "
             "![](/auth/magic-link/verify?token=abc) "
             "![](/api/export/threads) ![ok](/media/nodes/1/2/a.png)",
             parent=root)
    client = app.test_client()
    for url in ("/@author/on-lore", f"/node/{root.id}"):
        html = client.get(url).get_data(as_text=True)
        assert "nice piece" in html
        srcs = re.findall(r'<img src="([^"]+)"', html)
        assert srcs == ["/media/nodes/1/2/a.png"]


def test_feed_images_follow_the_same_rule(app):
    _publish("author", "# Pictures\n\n![x](/auth/logout) "
             "![y](/media/nodes/1/2/b.png)", "pictures")
    xml = app.test_client().get("/@author/feed.xml").get_data(as_text=True)
    assert "/auth/logout" not in xml
    assert "/media/nodes/1/2/b.png" in xml
