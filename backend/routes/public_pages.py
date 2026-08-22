"""Server-rendered public pages (#228 SEO/no-JS pass).

nginx routes these paths to Flask so that crawlers, social scrapers, and
any client without JavaScript get real HTML: the full article text in
semantic markup, per-route metadata, JSON-LD, a sitemap, and Atom/
markdown representations. JS visitors get the identical SPA — the shell
is the built index.html and React replaces the injected content on mount.

PRIVACY INVARIANT — the whole point of reviewing this file carefully:
every piece of content rendered here must satisfy the same predicate the
public JSON API uses: privacy_level == "public" AND deleted_at IS NULL
(via _public_alive from the commons blueprint). Session state is never
read; drafts, private and deleted nodes must be structurally unreachable.
Non-public URLs render the plain app shell with noindex and 404/410 —
never content.

Status codes: 200 published, 404 unknown/private (identical on purpose),
410 for a permalink whose node was revoked or deleted (tombstone rows
still carry public_slug through the 30-day grace window).
"""
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

from flask import Blueprint, Response, current_app, request
from markupsafe import escape

from backend.extensions import db
from backend.models import Node, ShareDraft, User
from backend.routes.commons import (
    MAX_THREAD_NODES,
    _public_alive,
    _publicly_visible,
    _serialize_public_subtree,
)
from backend.utils import public_cache
from backend.utils.public_html import (
    base_url,
    plain_excerpt,
    render_markdown,
    render_page,
    split_title,
)
from backend.utils.timefmt import iso_utc

public_pages_bp = Blueprint("public_pages", __name__)


def _enabled():
    return bool(current_app.config.get("SHARE_V1", False))


def _html(body, status=200):
    return Response(body, status=status, mimetype="text/html")


def _shell_404(title="Not found"):
    return _html(render_page({"title": f"{title} — Loore", "noindex": True}),
                 404)


def _shell_410():
    return _html(render_page({"title": "Gone — Loore", "noindex": True}), 410)


@public_pages_bp.app_errorhandler(404)
def _not_found(e):
    """Unmatched page URLs that nginx routes to Flask (e.g. /@user/x/y,
    /node/999999) get the SPA shell with noindex, not Werkzeug's default
    error page. API-ish paths keep their existing 404 bodies."""
    path = request.path
    if (path.startswith("/api") or path.startswith("/auth")
            or path.startswith("/media")):
        return e
    return _shell_404()


def _cached(render_fn, cacheable_statuses=(200, 410)):
    """Serve request.path from the Redis page cache, else render and
    cache. Bounds KMS decrypt cost under crawler traffic."""
    hit = public_cache.get(request.path)
    if hit:
        status, content_type, body = hit
        return Response(body, status=status, mimetype=content_type)
    resp = render_fn()
    if resp.status_code in cacheable_statuses:
        public_cache.put(request.path, resp.status_code,
                         resp.mimetype, resp.get_data(as_text=True))
    return resp


# ---------------------------------------------------------------------------
# Shared article machinery
# ---------------------------------------------------------------------------

def _public_root_of(node):
    """Walk up to the nearest public living root (same rule as the
    commons thread endpoint)."""
    root = node
    visited = set()
    while root.parent_id and root.id not in visited:
        visited.add(root.id)
        parent = Node.query.get(root.parent_id)
        if not _publicly_visible(parent):
            break
        root = parent
    return root


def _published_at(root):
    share = ShareDraft.query.filter_by(
        public_node_id=root.id, status="published").first()
    return (share.published_at if share and share.published_at
            else root.created_at)


def _display_author(item):
    """Byline for a serialized thread node: '@user' for human nodes,
    'model · via @user' for LLM ones."""
    name = item.get("username")
    if item.get("node_type") == "llm" and item.get("llm_model"):
        model = item["llm_model"]
        return f"{model} · via @{name}" if name else model
    return f"@{name}" if name else "anonymous"


def _render_reply(item):
    author = escape(_display_author(item))
    when = item.get("created_at") or ""
    date = when[:10]
    body = render_markdown(item.get("content") or "")
    children = "".join(_render_reply(c) for c in item.get("children", []))
    if children:
        children = f'<div class="ssr-children">{children}</div>'
    return (f"<article><header>{author}"
            f' · <time datetime="{escape(when)}">{escape(date)}</time>'
            f"</header>{body}{children}</article>")


def _article_document(root, author_user):
    """(html_body, meta) for a public root node and its public thread."""
    content = root.get_content() or ""
    title, body_md = split_title(content)
    thread = _serialize_public_subtree(root, [MAX_THREAD_NODES])
    replies = thread.get("children", []) if thread else []

    username = author_user.username
    origin = base_url()
    profile_url = f"{origin}/@{username}"
    canonical = (f"{origin}/@{username}/{root.public_slug}"
                 if root.public_slug else f"{origin}/node/{root.id}")
    published = _published_at(root)
    modified = root.updated_at or published

    parts = ["<article>"]
    parts.append(f"<h1>{escape(title)}</h1>")
    parts.append(
        '<div class="ssr-meta">By <a href="{}">@{}</a> · '
        '<time datetime="{}">{}</time></div>'.format(
            escape(profile_url), escape(username),
            escape(iso_utc(published) or ""),
            escape((iso_utc(published) or "")[:10])))
    parts.append(render_markdown(body_md))
    if replies:
        parts.append('<section aria-label="Replies"><h2>Replies</h2>')
        parts.extend(_render_reply(r) for r in replies)
        parts.append("</section>")
    parts.append("</article>")

    author_person = {
        "@type": "Person",
        "name": username,
        "url": profile_url,
    }
    twitter_id = author_user.twitter_id or ""
    if twitter_id.isdigit():
        # The numeric X id survives handle renames; /i/user/<id> resolves
        # to the current profile.
        author_person["sameAs"] = [f"https://x.com/i/user/{twitter_id}"]

    description = plain_excerpt(body_md or content)
    og_image = (f"{canonical}/og.png" if root.public_slug
                else f"{origin}/og-image.png")
    meta = {
        "title": f"{title} — Loore",
        "description": description,
        "canonical": canonical,
        "og_type": "article",
        "image": og_image,
        "image_alt": f"{title} — by @{username} on Loore",
        "twitter_card": "summary",
        "twitter_image": f"{origin}/loore-logo.png",
        "published_time": iso_utc(published),
        "modified_time": iso_utc(modified),
        "author_url": profile_url,
        "alternates": [
            ("alternate", "application/atom+xml",
             f"{origin}/@{username}/feed.xml", f"@{username} on Loore"),
        ],
        "jsonld": {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": title[:110],
            "description": description,
            "author": author_person,
            "datePublished": iso_utc(published),
            "dateModified": iso_utc(modified),
            "publisher": {
                "@type": "Organization",
                "name": "Loore",
                "url": origin,
                "logo": {
                    "@type": "ImageObject",
                    "url": f"{origin}/loore-logo.png",
                },
            },
            "mainEntityOfPage": canonical,
            "image": og_image,
        },
    }
    if root.public_slug:
        meta["alternates"].append(
            ("alternate", "text/markdown",
             f"{canonical}.md", f"{title} (markdown)"))
    return "".join(parts), meta


def _public_roots_for(user):
    """The user's living public root nodes, pinned first then newest —
    the same set the public JSON API serves."""
    nodes = _public_alive(Node.query.filter(
        Node.parent_id.is_(None),
        (Node.human_owner_id == user.id) | (Node.user_id == user.id),
    )).all()
    nodes.sort(key=lambda n: (
        n.pinned_at is None,
        -(n.pinned_at.timestamp() if n.pinned_at else 0),
        -(n.created_at.timestamp() if n.created_at else 0),
    ))
    return nodes


# ---------------------------------------------------------------------------
# Routes: articles and profiles
# ---------------------------------------------------------------------------

@public_pages_bp.route("/@<username>/feed.xml")
def author_feed(username):
    if not _enabled():
        return Response("Not found", status=404, mimetype="text/plain")
    return _cached(lambda: _render_author_feed(username))


def _render_author_feed(username):
    user = User.query.filter_by(username=username).first()
    if user is not None and not user.public_sharing_enabled:
        user = None
    roots = _public_roots_for(user) if user else []
    if not roots:
        return Response("Not found", status=404, mimetype="text/plain")
    origin = base_url()
    feed_url = f"{origin}/@{username}/feed.xml"
    updated = max((r.updated_at or r.created_at) for r in roots)
    entries = []
    for root in roots:
        if not root.public_slug:
            continue
        content = root.get_content() or ""
        title, body_md = split_title(content)
        link = f"{origin}/@{username}/{root.public_slug}"
        entries.append(
            "<entry>"
            f"<title>{xml_escape(title)}</title>"
            f'<link rel="alternate" type="text/html" href="{xml_escape(link)}"/>'
            f"<id>{xml_escape(link)}</id>"
            f"<published>{xml_escape(iso_utc(_published_at(root)) or '')}</published>"
            f"<updated>{xml_escape(iso_utc(root.updated_at or root.created_at) or '')}</updated>"
            f'<content type="html">{xml_escape(render_markdown(body_md))}</content>'
            "</entry>")
    feed = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>@{xml_escape(username)} on Loore</title>"
        f'<link rel="self" href="{xml_escape(feed_url)}"/>'
        f'<link rel="alternate" type="text/html" href="{xml_escape(origin)}/@{xml_escape(username)}"/>'
        f"<id>{xml_escape(feed_url)}</id>"
        f"<updated>{xml_escape(iso_utc(updated) or '')}</updated>"
        f"<author><name>{xml_escape(username)}</name></author>"
        + "".join(entries) + "</feed>")
    return Response(feed, mimetype="application/atom+xml")


@public_pages_bp.route("/@<username>/<slug>/og.png")
def article_og_image(username, slug):
    """Per-article social card. Same visibility predicate as the page;
    rendering needs only the root's title, so cost is one decrypt per
    worker (DEK-cached) — no Redis layer, short HTTP cache instead."""
    if not _enabled():
        return Response("Not found", status=404, mimetype="text/plain")
    user, node, _ = _resolve_permalink(username, slug)
    if node is None:
        return Response("Not found", status=404, mimetype="text/plain")
    from backend.utils.og_image import render_article_card
    title, _body = split_title(node.get_content() or "")
    png = render_article_card(title, username, _published_at(node))
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@public_pages_bp.route("/@<username>/og.png")
def profile_og_image(username):
    if not _enabled():
        return Response("Not found", status=404, mimetype="text/plain")
    user = User.query.filter_by(username=username).first()
    if (user is None or not user.public_sharing_enabled
            or not _public_roots_for(user)):
        return Response("Not found", status=404, mimetype="text/plain")
    from backend.utils.og_image import render_profile_card
    png = render_profile_card(username, user.description)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@public_pages_bp.route("/@<username>/<slug>")
def article(username, slug):
    if not _enabled():
        return _shell_404()
    if slug.endswith(".md"):
        return _cached(lambda: _render_article_md(username, slug[:-3]))
    return _cached(lambda: _render_article(username, slug))


def _resolve_permalink(username, slug):
    """(user, node, tombstoned). Only living public nodes by an opted-in
    author resolve (the toggle check lives in _public_alive); a soft-
    deleted row with this slug reports tombstoned for the 410."""
    user = User.query.filter_by(username=username).first()
    if user is None or not user.public_sharing_enabled:
        return None, None, False
    node = _public_alive(Node.query.filter(
        Node.human_owner_id == user.id,
        Node.public_slug == slug,
    )).first()
    if node is not None:
        return user, node, False
    tombstoned = db.session.query(Node.id).filter(
        Node.human_owner_id == user.id,
        Node.public_slug == slug,
        Node.deleted_at.isnot(None),
    ).first() is not None
    return user, None, tombstoned


def _render_article(username, slug):
    user, node, tombstoned = _resolve_permalink(username, slug)
    if node is None:
        return _shell_410() if tombstoned else _shell_404()
    body, meta = _article_document(node, user)
    return _html(render_page(meta, body))


def _render_article_md(username, slug):
    user, node, tombstoned = _resolve_permalink(username, slug)
    if node is None:
        status = 410 if tombstoned else 404
        return Response("Not found", status=status, mimetype="text/plain")
    content = (node.get_content() or "").strip()
    footer = (f"\n\n---\n\nPublished by @{username} on Loore: "
              f"{base_url()}/@{username}/{slug}\n")
    return Response(content + footer,
                    mimetype="text/markdown; charset=utf-8")


@public_pages_bp.route("/@<username>")
def profile(username):
    if not _enabled():
        return _shell_404()
    return _cached(lambda: _render_profile(username))


def _render_profile(username):
    user = User.query.filter_by(username=username).first()
    if user is not None and not user.public_sharing_enabled:
        user = None
    roots = _public_roots_for(user) if user else []
    # A user with nothing public is indistinguishable from a user that
    # doesn't exist — same as the JSON API's 404 parity.
    if not roots:
        return _shell_404()
    origin = base_url()
    canonical = f"{origin}/@{username}"
    parts = [f"<h1>@{escape(username)}</h1>"]
    if user.description:
        parts.append(f"<p>{escape(user.description)}</p>")
    parts.append('<section aria-label="Published pieces">')
    for root in roots:
        content = root.get_content() or ""
        title, body_md = split_title(content)
        preview = plain_excerpt(body_md or content, limit=300)
        when = iso_utc(_published_at(root)) or ""
        parts.append("<article>")
        if root.public_slug:
            parts.append(
                f'<h2><a href="{escape(origin)}/@{escape(username)}/'
                f'{escape(root.public_slug)}">{escape(title)}</a></h2>')
        else:
            parts.append(f"<h2>{escape(title)}</h2>")
        parts.append(
            f'<div class="ssr-meta"><time datetime="{escape(when)}">'
            f"{escape(when[:10])}</time></div>")
        parts.append(f"<p>{escape(preview)}</p>")
        parts.append("</article>")
    parts.append("</section>")

    description = (user.description
                   or f"Writing published by @{username} on Loore.")
    meta = {
        "title": f"@{username} — Loore",
        "description": description,
        "canonical": canonical,
        "og_type": "profile",
        "image": f"{canonical}/og.png",
        "image_alt": f"@{username} on Loore",
        "twitter_card": "summary",
        "twitter_image": f"{origin}/loore-logo.png",
        "alternates": [
            ("alternate", "application/atom+xml",
             f"{canonical}/feed.xml", f"@{username} on Loore"),
        ],
        "jsonld": {
            "@context": "https://schema.org",
            "@type": "ProfilePage",
            "mainEntity": {
                "@type": "Person",
                "name": username,
                "url": canonical,
                "description": description,
            },
        },
    }
    return _html(render_page(meta, "".join(parts)))


@public_pages_bp.route("/node/<int:node_id>")
def node_page(node_id):
    """/node/<id> is the id-addressed public thread URL. Public nodes get
    SSR with a canonical pointing at the pretty permalink; everything
    else gets the plain app shell (the SPA handles members' private
    views) with a 404 status and noindex."""
    if not _enabled():
        return _shell_404()
    return _cached(lambda: _render_node_page(node_id))


def _render_node_page(node_id):
    node = Node.query.get(node_id)
    if not _publicly_visible(node):
        return _shell_404()
    root = _public_root_of(node)
    owner_id = root.human_owner_id or root.user_id
    owner = User.query.get(owner_id) if owner_id else None
    if owner is None:
        return _shell_404()
    body, meta = _article_document(root, owner)
    return _html(render_page(meta, body))


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------

_MARKETING_PATHS = ["/", "/landing", "/why-loore", "/vision", "/how-to"]


@public_pages_bp.route("/sitemap.xml")
def sitemap():
    return _cached(_render_sitemap)


def _render_sitemap():
    origin = base_url()
    urls = [(f"{origin}{p}" if p != "/" else origin, None)
            for p in _MARKETING_PATHS]
    if _enabled():
        rows = (db.session.query(
                    User.username, Node.public_slug, Node.updated_at,
                    Node.created_at)
                .join(User, User.id == Node.human_owner_id)
                .filter(Node.parent_id.is_(None),
                        Node.privacy_level == "public",
                        Node.deleted_at.is_(None),
                        Node.public_slug.isnot(None),
                        User.public_sharing_enabled.is_(True))
                .all())
        profiles = {}
        for username, slug, updated_at, created_at in rows:
            lastmod = updated_at or created_at
            urls.append((f"{origin}/@{username}/{slug}", lastmod))
            prev = profiles.get(username)
            if prev is None or (lastmod and lastmod > prev):
                profiles[username] = lastmod
        for username, lastmod in sorted(profiles.items()):
            urls.append((f"{origin}/@{username}", lastmod))
    entries = []
    for loc, lastmod in urls:
        lastmod_tag = ""
        if isinstance(lastmod, datetime):
            lastmod_tag = f"<lastmod>{lastmod.date().isoformat()}</lastmod>"
        entries.append(
            f"<url><loc>{xml_escape(loc)}</loc>{lastmod_tag}</url>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(entries) + "</urlset>")
    return Response(xml, mimetype="application/xml")


# ---------------------------------------------------------------------------
# Marketing pages: per-route metadata, body stays the SPA's
# ---------------------------------------------------------------------------

_MARKETING_META = {
    "/": (
        "Loore — a place to become yourself",
        "Voice-first journaling with an AI that remembers. Uncover your "
        "lore. Unleash your hidden potential.",
    ),
    "/landing": (
        "Loore — uncover your lore",
        "Voice-first journaling with an AI that remembers. Uncover your "
        "lore. Unleash your hidden potential.",
    ),
    "/why-loore": (
        "Why Loore — a place to become yourself",
        "AI is powerful. Loore puts that power in service of something "
        "personal — understanding who you are and authoring who you're "
        "becoming.",
    ),
    "/vision": (
        "The Vision — from private reflection to effortless connection",
        "Loore is a complete ecosystem for self-authorship: a journal that "
        "grows into a living cycle of reflection, insight, sharing, and "
        "meaningful connection.",
    ),
    "/how-to": (
        "How to use Loore — practical tips & workflows",
        "Loore is flexible enough to fit how you think. The basics, plus "
        "workflows people use on Loore every day.",
    ),
    "/login": ("Sign in — Loore", "Sign in to Loore."),
    "/alpha-thank-you": (
        "Thank you — Loore",
        "Thanks for your interest in the Loore alpha.",
    ),
}


def _marketing_page(path):
    title, description = _MARKETING_META[path]
    origin = base_url()
    canonical = origin if path == "/" else f"{origin}{path}"
    return _html(render_page({
        "title": title,
        "description": description,
        "canonical": canonical,
    }))


@public_pages_bp.route("/")
def marketing_root():
    return _marketing_page("/")


@public_pages_bp.route("/landing")
def marketing_landing():
    return _marketing_page("/landing")


@public_pages_bp.route("/why-loore")
def marketing_why():
    return _marketing_page("/why-loore")


@public_pages_bp.route("/vision")
def marketing_vision():
    return _marketing_page("/vision")


@public_pages_bp.route("/how-to")
def marketing_howto():
    return _marketing_page("/how-to")


@public_pages_bp.route("/login")
def marketing_login():
    return _marketing_page("/login")


@public_pages_bp.route("/alpha-thank-you")
def marketing_alpha_thanks():
    return _marketing_page("/alpha-thank-you")
