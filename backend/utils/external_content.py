"""Clients for external content sources (#155 component 2 / Download).

Community Archive: the public open tweet database
(https://github.com/TheExGenesis/community-archive) — a Supabase
PostgREST API readable with a published anon key. No user credentials
involved; only accounts that donated their archives are present.

Twitter/X bookmarks: X API v2 (pay-per-use tier), OAuth 2.0 PKCE user
context with bookmark.read scope. Env-gated until credentials/credits
are configured (X_CLIENT_ID etc.).
"""
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# Public Community Archive instance (anon key is published in their docs
# for read access — not a secret).
CA_BASE_URL = "https://fabxmporizzqflnftavs.supabase.co"
CA_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZhYnhtcG9yaXp6cWZsbmZ0YXZzIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3MjIyNDQ5MTIsImV4cCI6MjAzNzgyMDkxMn0."
    "UIEJiUNkLsW28tBHmG-RQDW-I5JNlJLt62CSk9D_qG8"
)
CA_PAGE_SIZE = 500
CA_TIMEOUT = 20

X_API_BASE = "https://api.twitter.com/2"
X_BOOKMARKS_PAGE_SIZE = 100


def _ca_headers():
    return {
        "apikey": CA_ANON_KEY,
        "Authorization": f"Bearer {CA_ANON_KEY}",
    }


def ca_lookup_account(username):
    """Resolve a Community Archive username to its account_id, or None."""
    resp = requests.get(
        f"{CA_BASE_URL}/rest/v1/account",
        params={"username": f"ilike.{username}", "select": "account_id,username"},
        headers=_ca_headers(), timeout=CA_TIMEOUT,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["account_id"] if rows else None


def _parse_tweet_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")
                                      ).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def normalize_ca_tweet(row, username):
    """Map a Community Archive tweets row to ExternalItem fields.

    Tolerant of column-name drift: tries the documented/likely names for
    id and text, skips rows where neither resolves.
    """
    tweet_id = (row.get("tweet_id") or row.get("id")
                or row.get("status_id"))
    text = (row.get("full_text") or row.get("text")
            or row.get("tweet_text"))
    if not tweet_id or not text or not str(text).strip():
        return None
    return {
        "external_id": str(tweet_id),
        "author_handle": username,
        "content": str(text),
        "url": f"https://twitter.com/{username}/status/{tweet_id}",
        "posted_at": _parse_tweet_dt(
            row.get("created_at") or row.get("created at")),
    }


def ca_fetch_tweets(account_id, username, max_items=2000):
    """Fetch up to *max_items* tweets for a CA account, newest first.

    Yields normalized item dicts; paginates via PostgREST offset.
    """
    fetched = 0
    offset = 0
    while fetched < max_items:
        page_size = min(CA_PAGE_SIZE, max_items - fetched)
        resp = requests.get(
            f"{CA_BASE_URL}/rest/v1/tweets",
            params={
                "account_id": f"eq.{account_id}",
                "select": "*",
                "order": "created_at.desc",
                "limit": str(page_size),
                "offset": str(offset),
            },
            headers=_ca_headers(), timeout=CA_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            item = normalize_ca_tweet(row, username)
            if item is not None:
                yield item
                fetched += 1
                if fetched >= max_items:
                    break
        offset += len(rows)
        if len(rows) < page_size:
            break


def normalize_x_bookmark(tweet, authors_by_id):
    """Map an X API v2 bookmarks tweet object to ExternalItem fields."""
    text = tweet.get("text")
    tweet_id = tweet.get("id")
    if not tweet_id or not text:
        return None
    author = authors_by_id.get(tweet.get("author_id"), {})
    handle = author.get("username")
    return {
        "external_id": str(tweet_id),
        "author_handle": handle,
        "content": text,
        "url": (f"https://twitter.com/{handle}/status/{tweet_id}"
                if handle else f"https://twitter.com/i/status/{tweet_id}"),
        "posted_at": _parse_tweet_dt(tweet.get("created_at")),
    }


def x_fetch_bookmark_pages(access_token, x_user_id, max_items=800):
    """Fetch the user's X bookmarks (newest-bookmarked first) as PAGES of
    normalized items, so the caller can stop paginating — each page is a
    paid API request — once a page yields nothing new.

    X API v2: GET /2/users/:id/bookmarks — OAuth2 user context with
    bookmark.read; max 800 most recent per X's own cap.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "max_results": X_BOOKMARKS_PAGE_SIZE,
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username",
    }
    fetched = 0
    next_token = None
    while fetched < max_items:
        if next_token:
            params["pagination_token"] = next_token
        resp = requests.get(
            f"{X_API_BASE}/users/{x_user_id}/bookmarks",
            params=params, headers=headers, timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        tweets = payload.get("data") or []
        users = (payload.get("includes") or {}).get("users") or []
        authors_by_id = {u["id"]: u for u in users}
        page = []
        for tweet in tweets:
            item = normalize_x_bookmark(tweet, authors_by_id)
            if item is not None:
                page.append(item)
                fetched += 1
                if fetched >= max_items:
                    break
        # Empty pages are yielded too: every yield corresponds to exactly
        # one paid API request, so the caller can meter cost by counting.
        yield page
        next_token = (payload.get("meta") or {}).get("next_token")
        if not next_token:
            break


def x_fetch_bookmarks(access_token, x_user_id, max_items=800):
    """Flat-iteration wrapper over x_fetch_bookmark_pages."""
    for page in x_fetch_bookmark_pages(
            access_token, x_user_id, max_items=max_items):
        yield from page


def x_refresh_access_token(client_id, refresh_token):
    """OAuth2 refresh-token grant for X (PKCE public client).

    Returns the token-endpoint JSON ({access_token, refresh_token, ...}).
    """
    resp = requests.post(
        "https://api.twitter.com/2/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
