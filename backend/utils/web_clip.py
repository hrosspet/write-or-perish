"""Web clips (#232): pure helpers shared by the clip route and tests.

A clip arrives from the Chrome clipper as {url, title, content, ...}.
The server decides what it is from the URL alone:

- an X/Twitter status URL becomes a ``twitter_bookmark`` keyed by tweet
  id, so a tweet clipped from a tab and the same tweet arriving via the
  nightly bookmark sync collapse into one row;
- anything else becomes a ``web_clip`` keyed by the sha256 of the
  canonical URL (64 hex chars, exactly the width of ``external_id``).
"""
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SOURCE_WEB_CLIP = "web_clip"
SOURCE_TWITTER_BOOKMARK = "twitter_bookmark"

_TWITTER_HOSTS = {
    "x.com", "www.x.com", "mobile.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com",
}
_STATUS_RE = re.compile(r"^/(?:i/web/|i/|[^/]+/)status(?:es)?/(\d+)")
# Tracking parameters that never change what page a URL denotes.
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
                    "igshid", "ref_src", "ref_url", "_hsenc", "_hsmi"}


def tweet_id_from_url(url):
    """Tweet id if ``url`` is an X/Twitter status page, else None."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    if parts.netloc.lower() not in _TWITTER_HOSTS:
        return None
    m = _STATUS_RE.match(parts.path)
    return m.group(1) if m else None


def canonical_url(url):
    """Normalize a URL so trivially different spellings dedupe: lowercase
    scheme/host, no fragment, no tracking params, no default port, no
    trailing slash (except the root path). Query order is preserved."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    port = parts.port
    if port and not ((scheme == "http" and port == 80)
                     or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)]
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def web_clip_external_id(url):
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def classify_clip(url):
    """Return (source, external_id, canonical_url) for a clipped URL."""
    tweet_id = tweet_id_from_url(url)
    if tweet_id:
        return (SOURCE_TWITTER_BOOKMARK, tweet_id,
                f"https://x.com/i/status/{tweet_id}")
    canon = canonical_url(url)
    return SOURCE_WEB_CLIP, web_clip_external_id(url), canon
