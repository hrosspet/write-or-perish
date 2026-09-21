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
# X bills bookmark reads per RETURNED POST (#271), so the page size is
# not a request-count lever any more: it is the cost of the last page —
# the one the sync fetches only to learn it holds nothing new. Pages
# start small and double while every page is all-new; the first page
# that reaches known bookmarks freezes the size (the caller sends
# grow=False), because the next page is most likely the closing one.
# So a quiet night costs the first page, and a night with N new
# bookmarks costs at most N + 2·min(N + 10, 100) posts: the all-new
# pages (≤ N), the page that crosses into known bookmarks and the
# closing page of the same size, each ≤ N + 10 because the all-new
# pages before it sum to that size minus 10. E.g. 1 new = 20 posts,
# 11 new = 50, 31 new = 110, 71 new = 230; a full 800 first import
# stays 800 (max_items caps the last request). Heuristic: the 10 is a
# floor on what a nightly check can cost (10 posts = $0.05), nothing
# more precise.
X_BOOKMARKS_FIRST_PAGE_SIZE = 10
X_BOOKMARKS_PAGE_SIZE = 100  # X's max_results cap for the endpoint


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
        # The archive is an opt-in public corpus: public by construction.
        "public_source": True,
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
    # A bookmark is public only if its author's account is not protected
    # (a follower can bookmark a protected tweet). X says so per author
    # in the `includes.users` expansion; an author X did not return, or a
    # user object without the field, leaves the answer unknown.
    protected = author.get("protected")
    public_source = (not protected) if isinstance(protected, bool) else None
    return {
        "external_id": str(tweet_id),
        "author_handle": handle,
        "content": text,
        "url": (f"https://twitter.com/{handle}/status/{tweet_id}"
                if handle else f"https://twitter.com/i/status/{tweet_id}"),
        "posted_at": _parse_tweet_dt(tweet.get("created_at")),
        "public_source": public_source,
    }


def x_fetch_bookmark_pages(access_token, x_user_id, max_items=800):
    """Fetch the user's X bookmarks (newest-bookmarked first) as PAGES,
    yielding ``(items, posts_returned)`` per request: the normalized
    items, and how many posts X returned for that request — what the
    cost ledger counts (#271). X bills each post once per UTC day, so a
    second manual sync the same day is logged in full though X may bill
    less. The two numbers differ only when a tweet fails to normalize
    (no id/text); max_items never makes them differ because the last
    request asks X for exactly the remainder. Empty pages are yielded
    too (cost 0), so the caller can stop paginating once a page yields
    nothing new.

    Page sizes start at X_BOOKMARKS_FIRST_PAGE_SIZE and double up to
    X_BOOKMARKS_PAGE_SIZE (see the constants for why). A caller driving
    the generator with ``send(False)`` keeps the next page the SAME size
    instead of doubling it — the sync does that once a page has reached
    already-imported bookmarks, so the closing page stays cheap. Plain
    iteration (``send(None)``) always doubles.

    X API v2: GET /2/users/:id/bookmarks — OAuth2 user context with
    bookmark.read; max 800 most recent per X's own cap.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username,protected",
    }
    fetched = 0
    next_token = None
    page_size = X_BOOKMARKS_FIRST_PAGE_SIZE
    while fetched < max_items:
        params["max_results"] = min(page_size, max_items - fetched)
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
        fetched += len(tweets)
        grow = yield page, len(tweets)
        next_token = (payload.get("meta") or {}).get("next_token")
        if not next_token:
            break
        if grow is not False:
            page_size = min(page_size * 2, X_BOOKMARKS_PAGE_SIZE)


# X's oEmbed endpoint (publish.twitter.com 301s here): the free, keyless
# way to ask whether a tweet is publicly embeddable. Not the paid API —
# no credits, no rate-limit headers, no documented quota.
X_OEMBED_URL = "https://publish.x.com/oembed"


def x_tweet_public_status(tweet_id, timeout=10):
    """Is this tweet public, according to X itself?

    Returns ``(verdict, http_status)``: verdict True when X serves the
    embed (200 — a public tweet; probed live 2026-09-21), False when X
    refuses it (403 = protected account, 404 = deleted, suspended or
    never existed — nothing public to point at either way), None when
    there is no answer (throttled, server error, network) so the caller
    can leave the row unknown and stop for now. The request carries the
    tweet id and ``dnt=1``; nothing about the user who saved it.
    """
    try:
        resp = requests.get(X_OEMBED_URL, params={
            "url": f"https://twitter.com/i/status/{tweet_id}",
            "omit_script": "1", "dnt": "1",
        }, timeout=timeout)
    except requests.RequestException:
        return None, None
    if resp.status_code == 200:
        return True, 200
    if resp.status_code in (403, 404):
        return False, resp.status_code
    return None, resp.status_code


X_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"


def x_token_request(grant, client_id, client_secret=None):
    """POST one OAuth2 grant to X's token endpoint, authenticating the
    client the way the app's registration requires.

    An app registered WITH a secret is a confidential client, and X then
    demands HTTP Basic auth on every grant — the refresh grant included.
    A confidential client that sends only a body ``client_id`` gets
    401 invalid_client. Both grants go through this one function so the
    code exchange and the refresh cannot drift apart again: for months
    only the code exchange sent Basic auth, so every nightly refresh
    401'd and the sync parked the account as revoked (#313).

    Returns the token-endpoint JSON ({access_token, refresh_token, ...}).
    """
    auth = (client_id, client_secret) if client_secret else None
    resp = requests.post(
        X_TOKEN_URL,
        data=dict(grant, client_id=client_id),
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def x_exchange_code(client_id, code, redirect_uri, code_verifier,
                    client_secret=None):
    """OAuth2 authorization-code grant (PKCE) — the connect callback."""
    return x_token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }, client_id, client_secret)


def x_refresh_access_token(client_id, refresh_token, client_secret=None):
    """OAuth2 refresh-token grant — the nightly sync's token renewal."""
    return x_token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, client_id, client_secret)
