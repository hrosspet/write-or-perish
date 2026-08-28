"""Minimal X (Twitter) API v2 client for the admin "Fetch via X" pre-fill.

App-only (OAuth2 client-credentials) auth from the OAuth1 consumer keys the
login flow already has (``TWITTER_API_KEY`` / ``TWITTER_API_SECRET``).
Pay-per-use pricing (docs.x.com/x-api/getting-started/pricing, Aug 2026):
$0.005 per post read, $0.010 per user read. The user-timeline endpoint
serves at most the ~3,200 most recent posts per account, so that is the
hard ceiling on what a pre-fill can pull regardless of budget.

``iter_user_tweets`` yields rows in the native data-export shape
(``{"tweet": {...}}``) so ``twitter_archive.compact_row`` and the whole
import path apply unchanged — same trick as community_archive.py.
stdlib only.
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.twitter.com"
TIMEOUT = 60
PAGE_SIZE = 100          # max_results ceiling on /2/users/:id/tweets
TIMELINE_CAP = 3200      # documented per-user limit of the timeline endpoint
from backend.utils.cost import (  # noqa: E402
    X_POST_READ_COST_MICRODOLLARS, X_USER_READ_COST_MICRODOLLARS)

COST_PER_POST_READ = X_POST_READ_COST_MICRODOLLARS / 1_000_000
COST_PER_USER_READ = X_USER_READ_COST_MICRODOLLARS / 1_000_000

_bearer_cache = {}


class XApiError(Exception):
    """User-facing problem (bad credentials, unknown handle, rate limit)."""


def estimate_cost(posts, user_reads=1):
    return round(cost_microdollars(posts, user_reads) / 1_000_000, 2)


def cost_microdollars(posts, user_reads=1):
    return posts * X_POST_READ_COST_MICRODOLLARS + user_reads * X_USER_READ_COST_MICRODOLLARS


def fetchable(tweet_count, requested=None):
    """How many posts a timeline pull can actually return."""
    n = min(int(tweet_count or 0), TIMELINE_CAP)
    if requested is not None:
        n = min(n, max(int(requested), 0))
    return n


def _bearer(key, secret):
    if not key or not secret:
        raise XApiError("X API credentials are not configured (TWITTER_API_KEY / TWITTER_API_SECRET).")
    if key in _bearer_cache:
        return _bearer_cache[key]
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    req = urllib.request.Request(
        f"{API_BASE}/oauth2/token", data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            token = json.load(resp)["access_token"]
    except urllib.error.HTTPError as e:
        raise XApiError(f"X API auth failed ({e.code}).") from e
    _bearer_cache[key] = token
    return token


def _get(path, params, creds):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{API_BASE}{path}?{query}",
        headers={"Authorization": f"Bearer {_bearer(*creds)}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 429:
            raise XApiError("X API rate limit hit — try again later.") from e
        raise XApiError(f"X API {e.code}: {body}") from e


def lookup_user(handle, creds):
    """{id, username, name, tweet_count, protected} or None if no such user.
    One billable user read."""
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        return None
    data = _get(f"/2/users/by/username/{urllib.parse.quote(handle)}",
                {"user.fields": "public_metrics,protected"}, creds)
    d = data.get("data")
    if not d:
        return None
    return {"id": d["id"], "username": d["username"], "name": d.get("name"),
            "tweet_count": d.get("public_metrics", {}).get("tweet_count", 0),
            "protected": bool(d.get("protected"))}


def to_export_entry(t, users_by_id):
    """Map a v2 tweet object to the data/tweets.js shape."""
    text = (t.get("note_tweet") or {}).get("text") or t.get("text") or ""
    dt = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    metrics = t.get("public_metrics") or {}
    reply_to_tweet = next((r["id"] for r in t.get("referenced_tweets") or []
                           if r.get("type") == "replied_to"), None)
    reply_to_user = t.get("in_reply_to_user_id")
    return {"tweet": {
        "id": t["id"], "id_str": t["id"],
        "full_text": text,
        "created_at": dt.strftime("%a %b %d %H:%M:%S +0000 %Y"),
        "favorite_count": str(metrics.get("like_count", 0)),
        "retweet_count": str(metrics.get("retweet_count", 0)),
        "in_reply_to_status_id_str": reply_to_tweet,
        "in_reply_to_user_id_str": reply_to_user,
        "in_reply_to_screen_name": users_by_id.get(reply_to_user),
        "lang": t.get("lang", "und"),
        "truncated": False,
        "source": "x-api",
        "entities": {},
    }}


def iter_user_tweets(user_id, creds, max_tweets=TIMELINE_CAP, on_page=None, on_raw=None):
    """Newest-first pages of @user's timeline, up to ``max_tweets`` posts
    INCLUDING retweets. Yields export-shaped rows (the importer's
    compact_row() drops the retweets).

    We deliberately do NOT pass ``exclude=retweets``: X applies the
    exclusion to each page *after* slicing it and then omits
    ``next_token``, so a heavy retweeter's walk stops after one page
    (observed 2026-08-28: 17k-tweet account → 13 posts, no cursor).
    Retweets are therefore billed like any other post read.

    ``on_raw(tweet, users_by_id)`` sees each raw v2 object first — the
    pre-fill uses it to keep a copy of what we paid for."""
    remaining = fetchable(TIMELINE_CAP, max_tweets)
    token, seen = None, 0
    while remaining > 0:
        params = {
            "max_results": max(5, min(PAGE_SIZE, remaining)),  # API floor is 5
            "tweet.fields": "created_at,public_metrics,in_reply_to_user_id,referenced_tweets,note_tweet,lang",
            "expansions": "in_reply_to_user_id",
            "user.fields": "username",
        }
        if token:
            params["pagination_token"] = token
        data = _get(f"/2/users/{user_id}/tweets", params, creds)
        users_by_id = {u["id"]: u.get("username")
                       for u in (data.get("includes") or {}).get("users") or []}
        page = data.get("data") or []
        for t in page:
            if remaining <= 0:
                break
            remaining -= 1
            seen += 1
            if on_raw:
                on_raw(t, users_by_id)
            yield to_export_entry(t, users_by_id)
        if on_page:
            on_page(seen)
        token = (data.get("meta") or {}).get("next_token")
        if not page or not token:
            break
