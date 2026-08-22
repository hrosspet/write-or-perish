"""Server-rendered HTML for the public pages (#228 SEO/no-JS pass).

The public side of Loore must be readable by clients that execute no
JavaScript: search crawlers, AI training crawlers, social scrapers,
archival tools, curl. nginx routes the public paths (/@…, /node/…,
/sitemap.xml, the marketing pages) to Flask, and this module turns the
built SPA shell (frontend/build/index.html) into a per-route document:

- the default <title>/description/OG/Twitter block is swapped for
  route-specific metadata (plus canonical, article:*, JSON-LD), and
- for article/profile pages the full semantic content is injected into
  <div id="root">, where React's createRoot().render simply replaces it
  when JS does run — JS users get the exact same app as before.

Privacy invariant: callers may only pass content that is already
public by the rules of the public API (privacy_level == "public",
deleted_at IS NULL). Nothing in this module reads the session.

Markdown is rendered with raw HTML escaped (mistune escape=True), which
matches react-markdown-without-rehype-raw on the frontend and closes the
XSS vector of user-authored HTML landing on the loore.org origin.
"""
import json
import os
import re

import mistune
from flask import current_app
from markupsafe import escape

# ---------------------------------------------------------------------------
# Markdown → semantic HTML
# ---------------------------------------------------------------------------

_markdown = mistune.create_markdown(
    escape=True,
    plugins=["table", "strikethrough", "url", "task_lists"],
)


def render_markdown(text):
    """User markdown → HTML with raw HTML escaped, h1s demoted to h2.

    The page reserves its single <h1> for the article title, so headings
    inside bodies shift down one notch (only h1→h2; deeper levels keep
    their authored hierarchy)."""
    html = _markdown(text or "")
    return re.sub(r"<(/?)h1>", r"<\1h2>", html)


def split_title(content):
    """(title, body_markdown) from a piece's content.

    The first line is the piece's de-facto title — it is what slugify()
    builds the permalink from — so it becomes the <h1>/<title> and is
    dropped from the rendered body to avoid duplication."""
    text = (content or "").strip()
    if not text:
        return "Untitled", ""
    first_line, _, rest = text.partition("\n")
    title = re.sub(r"^[#>\s]+", "", first_line)
    title = re.sub(r"[*_`]+", "", title).strip() or "Untitled"
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0] + "…"
    return title, rest.strip()


_WS = re.compile(r"\s+")


def plain_excerpt(markdown_text, limit=200):
    """Plaintext description from markdown: markers stripped, whitespace
    collapsed, cut at a word boundary."""
    text = markdown_text or ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[#>\s|*-]+", "", text, flags=re.M)
    text = re.sub(r"[*_~]{1,3}", "", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = _WS.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


# ---------------------------------------------------------------------------
# The SPA shell
# ---------------------------------------------------------------------------

_FALLBACK_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="description" content="Uncover your lore."/>
<title>Loore</title>
</head>
<body><div id="root"></div></body>
</html>"""

_shell_cache = {"path": None, "mtime": None, "html": None}


def _load_shell():
    """The built index.html, cached by mtime; a minimal skeleton when no
    build exists (dev containers, unit tests)."""
    build_dir = current_app.config.get("FRONTEND_BUILD_DIR")
    path = os.path.join(build_dir, "index.html") if build_dir else None
    try:
        mtime = os.path.getmtime(path)
    except (OSError, TypeError):
        return _FALLBACK_SHELL
    if _shell_cache["path"] != path or _shell_cache["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            _shell_cache.update(path=path, mtime=mtime, html=f.read())
    return _shell_cache["html"]


# Default tags in the shell that per-route metadata replaces. CRA's build
# minifies index.html (comments are stripped), so surgery targets the tags
# themselves rather than marker comments.
_STRIP_TAGS = re.compile(
    r'<meta\s+(?:property="og:[^"]*"|name="twitter:[^"]*"'
    r'|name="description")[^>]*/?>\s*',
)
_TITLE = re.compile(r"<title>.*?</title>", re.S)
_NOSCRIPT = re.compile(r"<noscript>.*?</noscript>", re.S)
_ROOT_DIV = re.compile(r'(<div id="root">)')

# Minimal reading style for the no-JS view, on the boot palette the shell
# already defines. React replaces #root on mount, so JS users see the app.
_SSR_CSS = (
    '<style>.loore-ssr{max-width:44rem;margin:0 auto;padding:2.5rem 1.25rem;'
    'font-family:Outfit,system-ui,sans-serif;line-height:1.65}'
    '.loore-ssr h1,.loore-ssr h2,.loore-ssr h3'
    '{font-family:"Cormorant Garamond",Georgia,serif;line-height:1.25}'
    '.loore-ssr a{color:#c4956a}'
    '.loore-ssr .ssr-meta{opacity:.7;font-size:.9em;margin-bottom:2rem}'
    '.loore-ssr article+article{margin-top:2rem;border-top:1px solid '
    'rgba(128,128,128,.35);padding-top:1.5rem}'
    '.loore-ssr pre{overflow-x:auto}'
    '.loore-ssr img{max-width:100%}</style>'
)


def _meta_block(meta):
    """The per-route <head> additions. Every value is HTML-escaped here;
    callers pass raw strings."""
    m = []

    def tag(fmt, value):
        if value:
            m.append(fmt.format(v=escape(value)))

    site = "Loore"
    title = meta.get("title") or site
    # Card titles drop the " — Loore" suffix the browser-tab <title>
    # carries: the card already shows the domain right above the title.
    social_title = meta.get("og_title") or title
    m.append(f"<title>{escape(title)}</title>")
    tag('<meta name="description" content="{v}"/>', meta.get("description"))
    if meta.get("noindex"):
        m.append('<meta name="robots" content="noindex"/>')
    tag('<link rel="canonical" href="{v}"/>', meta.get("canonical"))

    tag('<meta property="og:title" content="{v}"/>', social_title)
    tag('<meta property="og:description" content="{v}"/>',
        meta.get("description"))
    tag('<meta property="og:url" content="{v}"/>', meta.get("canonical"))
    m.append('<meta property="og:type" content="{}"/>'.format(
        escape(meta.get("og_type") or "website")))
    m.append(f'<meta property="og:site_name" content="{site}"/>')
    image = meta.get("image") or _base_url() + "/og-image.png"
    tag('<meta property="og:image" content="{v}"/>', image)
    # Every image this app serves as a card is 1200×630.
    m.append('<meta property="og:image:width" content="1200"/>')
    m.append('<meta property="og:image:height" content="630"/>')
    tag('<meta property="og:image:alt" content="{v}"/>',
        meta.get("image_alt"))

    # X gets the compact card with the square logo mark (Peter's call —
    # the large generated card is finicky on X); og:image keeps the big
    # card for platforms that read og:* (Slack, Discord, iMessage, FB).
    m.append('<meta name="twitter:card" content="{}"/>'.format(
        escape(meta.get("twitter_card") or "summary_large_image")))
    tag('<meta name="twitter:title" content="{v}"/>', social_title)
    tag('<meta name="twitter:description" content="{v}"/>',
        meta.get("description"))
    tag('<meta name="twitter:image" content="{v}"/>',
        meta.get("twitter_image") or image)
    tag('<meta name="twitter:creator" content="{v}"/>',
        meta.get("twitter_creator"))

    tag('<meta property="article:published_time" content="{v}"/>',
        meta.get("published_time"))
    tag('<meta property="article:modified_time" content="{v}"/>',
        meta.get("modified_time"))
    tag('<meta property="article:author" content="{v}"/>',
        meta.get("author_url"))

    for rel, type_, href, feed_title in meta.get("alternates", []):
        m.append(
            '<link rel="{}" type="{}" href="{}" title="{}"/>'.format(
                escape(rel), escape(type_), escape(href), escape(feed_title)))

    if meta.get("jsonld"):
        payload = json.dumps(meta["jsonld"], ensure_ascii=False)
        # No literal '<' inside the script block: user text can't smuggle
        # markup (or a premature </script>) into the document.
        payload = payload.replace("<", "\\u003c")
        m.append('<script type="application/ld+json">'
                 f"{payload}</script>")
    return "\n".join(m)


def render_page(meta, body_html=None):
    """The SPA shell with per-route metadata and (optionally) server-
    rendered content inside #root. Returns an HTML string."""
    html = _load_shell()
    html = _STRIP_TAGS.sub("", html)
    html = _TITLE.sub("", html)
    block = _meta_block(meta)
    if body_html:
        block += _SSR_CSS
    html = html.replace("</head>", block + "\n</head>", 1)
    if body_html:
        # No-JS clients read this; React's render() replaces it on mount.
        html = _NOSCRIPT.sub("", html)
        wrapped = f'<div class="loore-ssr">{body_html}</div>'
        html, n = _ROOT_DIV.subn(lambda mo: mo.group(1) + wrapped, html)
        if n == 0:
            html = html.replace(
                "<body>", f"<body><div>{wrapped}</div>", 1)
    return html


def _base_url():
    """Absolute origin for canonical URLs and OG tags."""
    return (current_app.config.get("PUBLIC_BASE_URL")
            or current_app.config.get("FRONTEND_URL")
            or "https://loore.org").rstrip("/")


def base_url():
    return _base_url()
