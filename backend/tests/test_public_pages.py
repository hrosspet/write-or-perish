"""Tests for the server-rendered public pages (SEO/no-JS pass).

The privacy audit lives here: drafts, private, deleted, and opted-out
content must be structurally absent from SSR output, the sitemap, and
the feeds — and the public JSON API must honor the author's
public_sharing_enabled toggle immediately.

Mirrors test_forum.py's mocking pattern (celery + LLM task module).
"""
import os
import sys
from datetime import datetime
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
sys.modules.setdefault("ffmpeg", MagicMock())

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node, ShareDraft  # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


def _make_app(share_v1=True):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = share_v1
    # No build dir -> the minimal fallback shell; no broker -> cache off.
    app.config["FRONTEND_BUILD_DIR"] = None
    app.config["PUBLIC_BASE_URL"] = "https://loore.org"

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.share import share_bp
    from backend.routes.commons import commons_bp
    from backend.routes.public_pages import public_pages_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    app.register_blueprint(public_pages_bp)
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
        author = User(username="author", default_ai_usage="chat",
                      public_sharing_enabled=True,
                      description="Writes about lore.")
        visitor = User(username="visitor", default_ai_usage="chat",
                       public_sharing_enabled=True)
        hermit = User(username="hermit", default_ai_usage="chat",
                      public_sharing_enabled=False)
        _db.session.add_all([author, visitor, hermit])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


def _user(username):
    return User.query.filter_by(username=username).first()


def _mk_node(username, content, parent=None, privacy="public",
             node_type="user", slug=None, deleted=False):
    user = _user(username)
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None,
                node_type=node_type, privacy_level=privacy,
                public_slug=slug)
    node.set_content(content)
    if deleted:
        node.deleted_at = datetime.utcnow()
    _db.session.add(node)
    _db.session.commit()
    return node


def _publish(username, content, slug):
    """A published article: public root node + its ShareDraft record."""
    node = _mk_node(username, content, slug=slug)
    share = ShareDraft(user_id=_user(username).id, share_type="insight",
                       status="published", public_node_id=node.id,
                       published_at=datetime(2026, 8, 1, 12, 0, 0))
    share.set_content(content)
    _db.session.add(share)
    _db.session.commit()
    return node


ARTICLE = "# On lore\n\nEverything you say is kept.\n\n## Why\n\nBecause."


# ── Article pages ────────────────────────────────────────────────────────

def test_article_full_text_in_initial_html(app):
    _publish("author", ARTICLE, "on-lore")
    r = app.test_client().get("/@author/on-lore")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Everything you say is kept." in html
    assert "<article>" in html
    assert html.count("<h1>") == 1
    assert "<h1>On lore</h1>" in html
    # body headings demoted below the single h1
    assert "<h2>Why</h2>" in html
    assert "<p>Because.</p>" in html


def test_article_meta_tags(app):
    _publish("author", ARTICLE, "on-lore")
    html = app.test_client().get("/@author/on-lore").get_data(as_text=True)
    assert "<title>On lore — Loore</title>" in html
    assert ('<link rel="canonical" href="https://loore.org/@author/on-lore"/>'
            in html)
    assert '<meta property="og:type" content="article"/>' in html
    # Card title without the tab-title's " — Loore" suffix — the card
    # already shows the domain line.
    assert '<meta property="og:title" content="On lore"/>' in html
    # X gets the compact card with the square logo; the generated
    # 1200×630 card stays in og:image for og:*-reading platforms.
    assert '<meta name="twitter:card" content="summary"/>' in html
    assert ('<meta name="twitter:image" content="https://loore.org/'
            'loore-logo.png"/>' in html)
    assert 'property="article:published_time" content="2026-08-01' in html
    assert 'property="article:modified_time"' in html
    assert '"@type": "Article"' in html
    assert '"headline": "On lore"' in html
    assert '"name": "author"' in html
    assert "noindex" not in html


def test_two_articles_have_distinct_meta(app):
    _publish("author", "# First piece\n\nAlpha body.", "first-piece")
    _publish("author", "# Second piece\n\nBeta body.", "second-piece")
    client = app.test_client()
    one = client.get("/@author/first-piece").get_data(as_text=True)
    two = client.get("/@author/second-piece").get_data(as_text=True)
    assert "<title>First piece — Loore</title>" in one
    assert "<title>Second piece — Loore</title>" in two
    assert "Alpha body." in one and "Alpha body." not in two
    assert "/@author/first-piece" in one
    assert "/@author/second-piece" in two


def test_article_includes_public_replies_only(app):
    root = _publish("author", ARTICLE, "on-lore")
    _mk_node("visitor", "a public reply", parent=root)
    _mk_node("visitor", "a private note", parent=root, privacy="private")
    html = app.test_client().get("/@author/on-lore").get_data(as_text=True)
    assert "a public reply" in html
    assert "a private note" not in html


def test_article_escapes_user_html(app):
    _publish("author",
             "# XSS check\n\n<script>alert('pwn')</script>", "xss-check")
    html = app.test_client().get("/@author/xss-check").get_data(as_text=True)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_node_url_serves_public_thread(app):
    root = _publish("author", ARTICLE, "on-lore")
    html = app.test_client().get(f"/node/{root.id}").get_data(as_text=True)
    assert "Everything you say is kept." in html
    # canonical points at the pretty permalink
    assert ('<link rel="canonical" href="https://loore.org/@author/on-lore"/>'
            in html)


# ── Status codes and privacy ─────────────────────────────────────────────

def test_private_node_404_and_never_leaks(app):
    node = _mk_node("author", "deeply private thought", privacy="private")
    r = app.test_client().get(f"/node/{node.id}")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "deeply private thought" not in html
    assert "noindex" in html


def test_unknown_article_404(app):
    r = app.test_client().get("/@author/never-written")
    assert r.status_code == 404
    assert "noindex" in r.get_data(as_text=True)


def test_deleted_article_410(app):
    node = _publish("author", ARTICLE, "on-lore")
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    r = app.test_client().get("/@author/on-lore")
    assert r.status_code == 410
    html = r.get_data(as_text=True)
    assert "Everything you say is kept." not in html
    assert "noindex" in html


def test_unmatched_public_path_404_shell(app):
    r = app.test_client().get("/@author/on-lore/extra")
    assert r.status_code == 404
    assert "noindex" in r.get_data(as_text=True)


def test_draft_share_content_never_rendered(app):
    share = ShareDraft(user_id=_user("author").id, share_type="insight",
                       status="draft")
    share.set_content("unpublished draft secret")
    _db.session.add(share)
    _db.session.commit()
    client = app.test_client()
    for path in ("/@author", "/sitemap.xml", "/@author/feed.xml"):
        assert "unpublished draft secret" not in client.get(
            path).get_data(as_text=True)


def test_share_v1_off_disables_everything(app):
    node = _publish("author", ARTICLE, "on-lore")
    app.config["SHARE_V1"] = False
    client = app.test_client()
    assert client.get("/@author/on-lore").status_code == 404
    assert client.get("/@author").status_code == 404
    assert client.get(f"/node/{node.id}").status_code == 404
    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert "on-lore" not in sitemap


# ── The opt-out toggle takes content down immediately ────────────────────

def test_opted_out_author_is_gone_from_ssr(app):
    node = _publish("author", ARTICLE, "on-lore")
    _user("author").public_sharing_enabled = False
    _db.session.commit()
    client = app.test_client()
    assert client.get("/@author/on-lore").status_code == 404
    assert client.get("/@author").status_code == 404
    assert client.get(f"/node/{node.id}").status_code == 404
    assert client.get("/@author/feed.xml").status_code == 404
    for path in (f"/node/{node.id}", "/sitemap.xml"):
        assert "Everything you say is kept." not in client.get(
            path).get_data(as_text=True)


def test_opted_out_author_is_gone_from_json_api(app):
    node = _publish("author", ARTICLE, "on-lore")
    _user("author").public_sharing_enabled = False
    _db.session.commit()
    client = app.test_client()
    assert client.get(f"/api/commons/node/{node.id}").status_code == 404
    assert client.get(
        "/api/commons/permalink/author/on-lore").status_code == 404
    assert client.get("/api/share/public/author").status_code == 404


def test_opted_out_replier_vanishes_from_thread(app):
    root = _publish("author", ARTICLE, "on-lore")
    _mk_node("hermit", "hermit says hello", parent=root)
    html = app.test_client().get("/@author/on-lore").get_data(as_text=True)
    assert "hermit says hello" not in html


# ── Profile, sitemap, feeds, markdown ────────────────────────────────────

def test_profile_page_lists_articles(app):
    _publish("author", ARTICLE, "on-lore")
    _publish("author", "# Second piece\n\nBeta body.", "second-piece")
    r = app.test_client().get("/@author")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "<h1>@author</h1>" in html
    assert "Writes about lore." in html
    assert "/@author/on-lore" in html
    assert "/@author/second-piece" in html
    assert '<meta property="og:type" content="profile"/>' in html


def test_profile_of_user_without_public_content_404(app):
    r = app.test_client().get("/@visitor")
    assert r.status_code == 404


def test_sitemap_lists_only_published_public(app):
    _publish("author", ARTICLE, "on-lore")
    _mk_node("author", "private one", privacy="private", slug=None)
    _mk_node("author", "deleted one", slug="deleted-one", deleted=True)
    _publish("hermit", "# Hermit piece\n\nHidden.", "hermit-piece")
    r = app.test_client().get("/sitemap.xml")
    assert r.status_code == 200
    xml = r.get_data(as_text=True)
    assert "<loc>https://loore.org/@author/on-lore</loc>" in xml
    assert "<loc>https://loore.org/@author</loc>" in xml
    assert "<loc>https://loore.org/why-loore</loc>" in xml
    assert "<lastmod>" in xml
    assert "deleted-one" not in xml
    assert "hermit" not in xml
    assert "private one" not in xml


def test_author_atom_feed(app):
    _publish("author", ARTICLE, "on-lore")
    r = app.test_client().get("/@author/feed.xml")
    assert r.status_code == 200
    assert "application/atom+xml" in r.mimetype
    xml = r.get_data(as_text=True)
    assert "<title>On lore</title>" in xml
    assert "https://loore.org/@author/on-lore" in xml


def test_markdown_representation(app):
    _publish("author", ARTICLE, "on-lore")
    r = app.test_client().get("/@author/on-lore.md")
    assert r.status_code == 200
    assert "text/markdown" in r.content_type
    body = r.get_data(as_text=True)
    assert "# On lore" in body
    assert "https://loore.org/@author/on-lore" in body


# ── Social cards (og:image) ──────────────────────────────────────────────

def test_article_og_image(app):
    _publish("author", ARTICLE, "on-lore")
    r = app.test_client().get("/@author/on-lore/og.png")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    from io import BytesIO
    from PIL import Image
    img = Image.open(BytesIO(r.data))
    assert img.size == (1200, 630)
    html = app.test_client().get("/@author/on-lore").get_data(as_text=True)
    assert ('og:image" content="https://loore.org/@author/on-lore/og.png"'
            in html)
    assert '<meta property="og:image:width" content="1200"/>' in html
    assert 'og:image:alt' in html


def test_profile_og_image(app):
    _publish("author", ARTICLE, "on-lore")
    r = app.test_client().get("/@author/og.png")
    assert r.status_code == 200
    assert r.mimetype == "image/png"


def test_og_image_respects_privacy(app):
    node = _publish("author", ARTICLE, "on-lore")
    _user("author").public_sharing_enabled = False
    _db.session.commit()
    client = app.test_client()
    assert client.get("/@author/on-lore/og.png").status_code == 404
    assert client.get("/@author/og.png").status_code == 404
    _user("author").public_sharing_enabled = True
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    assert client.get("/@author/on-lore/og.png").status_code == 404


# ── Marketing pages ──────────────────────────────────────────────────────

def test_marketing_pages_have_distinct_meta(app):
    client = app.test_client()
    root = client.get("/").get_data(as_text=True)
    why = client.get("/why-loore").get_data(as_text=True)
    assert "<title>Loore — a place to become yourself</title>" in root
    assert "Why Loore" in why
    assert ('<link rel="canonical" href="https://loore.org/why-loore"/>'
            in why)
    assert "noindex" not in why


def test_no_cookies_required(app):
    """The GOAL check: anonymous, cookieless request returns the full
    article text — and the response sets no session cookie."""
    _publish("author", ARTICLE, "on-lore")
    r = app.test_client().get("/@author/on-lore")
    assert r.status_code == 200
    assert "Everything you say is kept." in r.get_data(as_text=True)
    assert "Set-Cookie" not in r.headers


def test_private_node_shell_is_neutral_for_signed_in_member(app):
    """A member opening their own private node in a new tab gets the SSR
    shell too; it must not flash 'Not found' in the tab before the SPA
    sets the real title. Still 404 (never cached), still noindex, still
    no content; anonymous keeps 'Not found'."""
    node = _mk_node("author", "deeply private thought", privacy="private")
    anon = app.test_client().get(f"/node/{node.id}")
    assert anon.status_code == 404
    assert "Not found — Loore" in anon.get_data(as_text=True)

    # The fixture wraps the whole test in one app context, so flask-login
    # caches the anon request's (anonymous) user on g; drop it so the
    # signed-in request re-loads identity from its own session cookie.
    from flask import g
    g.pop("_login_user", None)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_user("author").id)
        sess["_fresh"] = True
    r = client.get(f"/node/{node.id}")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    import re
    m = re.search(r"<title>(.*?)</title>", html)
    assert m and m.group(1) == "Loore", html[:700]
    assert "Not found" not in html
    assert "deeply private thought" not in html
    assert "noindex" in html


# ── Username rename keeps permalinks alive (#253) ────────────────────────

def _rename(old, new):
    from backend.utils.username_history import record_rename
    user = _user(old)
    record_rename(user, old, new)
    user.username = new
    _db.session.commit()


def _former_handles(username):
    from backend.models import UsernameHistory
    return [r.old_username for r in UsernameHistory.query.filter_by(
        user_id=_user(username).id).all()]


ROUTES = [
    ("/@{u}/on-lore", "/@{u}/on-lore"),
    ("/@{u}/on-lore.md", "/@{u}/on-lore.md"),
    ("/@{u}/on-lore/og.png", "/@{u}/on-lore/og.png"),
    ("/@{u}", "/@{u}"),
    ("/@{u}/feed.xml", "/@{u}/feed.xml"),
    ("/@{u}/og.png", "/@{u}/og.png"),
]


def _assert_redirects(client, old, new):
    for path, target in ROUTES:
        r = client.get(path.format(u=old))
        assert r.status_code == 301, (path, r.status_code)
        assert r.headers["Location"].endswith(target.format(u=new)), (
            path, r.headers["Location"])
        # A pinned 301 would loop once the handle is taken back.
        assert r.headers["Cache-Control"] == "no-store", path


def _assert_plain_404(client, handle):
    for path, _ in ROUTES:
        r = client.get(path.format(u=handle))
        assert r.status_code == 404, (path, r.status_code)
        assert "Location" not in r.headers, path


def test_former_handle_301s_to_current_on_every_public_route(app):
    _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    c = app.test_client()
    _assert_redirects(c, "author", "writer")
    # The current handle serves the page; the sitemap only lists it.
    assert c.get("/@writer/on-lore").status_code == 200
    sitemap = c.get("/sitemap.xml").get_data(as_text=True)
    assert "/@writer/on-lore" in sitemap and "/@author/" not in sitemap
    # Nobody-ever-held-it stays a plain 404.
    _assert_plain_404(c, "nobody")


def test_redirect_keeps_the_query_string(app):
    _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    r = app.test_client().get("/@author/on-lore?utm_source=newsletter&q=a%20b")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(
        "/@writer/on-lore?utm_source=newsletter&q=a%20b")


def test_former_handle_of_a_private_account_is_a_plain_404(app):
    """The redirect names the new handle, so it exists only where the
    target is public anyway: an account that went private after renaming
    answers like a handle nobody ever held (404 parity)."""
    _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    _user("writer").public_sharing_enabled = False
    _db.session.commit()
    c = app.test_client()
    _assert_plain_404(c, "author")
    _assert_plain_404(c, "writer")
    assert c.get("/api/commons/permalink/author/on-lore").status_code == 404
    assert c.get("/api/share/public/author").status_code == 404
    # Sharing back on: the redirect is back.
    _user("writer").public_sharing_enabled = True
    _db.session.commit()
    _assert_redirects(c, "author", "writer")


def test_former_handle_only_redirects_to_a_page_that_renders(app):
    node = _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    c = app.test_client()
    # An unknown slug under the old handle confirms nothing.
    for path in ["/@author/nope", "/@author/nope.md", "/@author/nope/og.png"]:
        r = c.get(path)
        assert r.status_code == 404 and "Location" not in r.headers, path
    assert c.get("/api/commons/permalink/author/nope").status_code == 404
    # A tombstone under the old handle is a plain 404 too (410 would say
    # the article existed under the new handle), the current one 410s.
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    assert c.get("/@writer/on-lore").status_code == 410
    r = c.get("/@author/on-lore")
    assert r.status_code == 404 and "Location" not in r.headers
    # Nothing public left: the profile-ish routes 404 without redirecting.
    _assert_plain_404(c, "author")
    assert c.get("/api/share/public/author").status_code == 404


def test_current_handle_in_another_case_redirects_to_its_spelling(app):
    """Handles are unique case-insensitively; every page has one URL, so
    another case of the current handle 301s to the stored spelling — the
    same rule that keeps a case-only rename from breaking old links."""
    _publish("author", ARTICLE, "on-lore")
    c = app.test_client()
    _assert_redirects(c, "Author", "author")
    old = c.get("/api/commons/permalink/AUTHOR/on-lore").get_json()
    assert old["canonical"] == "/@author/on-lore"
    assert c.get("/api/share/public/AUTHOR").get_json()["canonical"] == "/@author"
    # A private account's handle in another case is still a plain 404.
    _mk_node("hermit", "hermit's public root", slug="quiet")
    _assert_plain_404(c, "Hermit")
    # A case-only rename records no history and keeps every old URL.
    _rename("author", "Author")
    assert _former_handles("Author") == []
    _assert_redirects(c, "author", "Author")
    assert c.get("/@Author/on-lore").status_code == 200


def test_permalink_api_carries_canonical_for_former_handle(app):
    node = _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    c = app.test_client()
    old = c.get("/api/commons/permalink/author/on-lore").get_json()
    assert old == {"node_id": node.id, "canonical": "/@writer/on-lore"}
    new = c.get("/api/commons/permalink/writer/on-lore").get_json()
    assert new == {"node_id": node.id}


def test_public_profile_api_resolves_former_handle(app):
    """In-app navigation to /@old never reaches the server-rendered 301,
    so the profile API resolves the handle and hands the SPA the
    canonical URL."""
    node = _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    c = app.test_client()
    old = c.get("/api/share/public/author").get_json()
    assert old["username"] == "writer"
    assert old["canonical"] == "/@writer"
    assert [s["public_node_id"] for s in old["shares"]] == [node.id]
    assert "canonical" not in c.get("/api/share/public/writer").get_json()


def test_former_handle_is_reserved_for_everyone_but_its_owner(app):
    from backend.utils.reserved_usernames import (
        derive_available_username, validate_username)
    from backend.utils.magic_link import generate_unique_username
    _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    assert validate_username("author", exclude_user_id=_user("visitor").id) == (
        "That username is reserved.")
    assert validate_username("Author", exclude_user_id=_user("visitor").id) == (
        "That username is reserved.")
    # The signup paths (magic link, X login, admin whitelist) share the
    # check: a new account gets a derived name instead.
    assert validate_username("author") == "That username is reserved."
    assert derive_available_username("author") == "author2"
    assert generate_unique_username("Author@example.com") == "Author2"
    # Taking your own former handle back is allowed…
    assert validate_username("author", exclude_user_id=_user("writer").id) is None
    # …and ends the redirect: the old direction flips.
    _rename("writer", "author")
    assert _former_handles("author") == ["writer"]
    c = app.test_client()
    assert c.get("/@author/on-lore").status_code == 200
    r = c.get("/@writer/on-lore")
    assert r.status_code == 301 and r.headers["Location"].endswith("/@author/on-lore")
    assert validate_username("writer", exclude_user_id=_user("visitor").id) == (
        "That username is reserved.")
    assert derive_available_username("writer") == "writer2"


def test_rename_history_dedupes_per_handle(app):
    _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    _rename("writer", "author")
    _rename("author", "writer")
    assert _former_handles("writer") == ["author"]


def test_rename_without_public_writing_reserves_nothing(app):
    """Only a handle the open web could reach becomes a redirect: with
    sharing off, or nothing published, a rename leaves no history — so
    nobody can reserve handles by cycling through them, and a private
    account's renames stay private."""
    from backend.utils.reserved_usernames import validate_username
    # Sharing on, nothing published.
    _rename("visitor", "visitor_2")
    assert _former_handles("visitor_2") == []
    assert validate_username("visitor", exclude_user_id=_user("hermit").id) is None
    # Something public, sharing off.
    _mk_node("hermit", "hermit's public root", slug="quiet")
    _rename("hermit", "hermit_2")
    assert _former_handles("hermit_2") == []
    r = app.test_client().get("/@hermit/quiet")
    assert r.status_code == 404 and "Location" not in r.headers
    # Taking a former handle back still clears its row, whatever the state.
    _publish("author", ARTICLE, "on-lore")
    _rename("author", "writer")
    _user("writer").public_sharing_enabled = False
    _db.session.commit()
    _rename("writer", "author")
    assert _former_handles("author") == []
