"""Community Archive client (https://www.community-archive.org).

An opt-in public corpus of user-uploaded Twitter archives, served through a
public Supabase REST API (the anon key is published in their docs). Used by
the admin "pre-fill from Community Archive" action to bootstrap an account's
profile from its public tweets before the person ever writes in Loore.

Stdlib only — the CLI in backend/scripts/fetch_community_archive.py reuses
it without Loore's dependencies.

Paging is keyset (``tweet_id > last``), not offset: the REST view 500s on
deep offsets, which is what made large accounts impractical before.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = "https://fabxmporizzqflnftavs.supabase.co"
# Public anon key, from
# https://github.com/TheExGenesis/community-archive/blob/main/docs/api-doc.md
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZhYnhtcG9yaXp6cWZsbmZ0YXZzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MjIyNDQ5MTIs"
    "ImV4cCI6MjAzNzgyMDkxMn0."
    "UIEJiUNkLsW28tBHmG-RQDW-I5JNlJLt62CSk9D_qG8"
)
PAGE_SIZE = 1000
TIMEOUT = 60


class CommunityArchiveError(Exception):
    """User-facing problem (unknown handle, API failure)."""


def _get(table, params):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?{query}",
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def fetch_account(handle):
    """{account_id, username, account_display_name, num_tweets} or None."""
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        return None
    rows = _get("all_account", {
        "username": f"ilike.{handle}",
        "select": "account_id,username,account_display_name,num_tweets",
    })
    return rows[0] if rows else None


def iter_tweets(handle, page_size=PAGE_SIZE, on_page=None):
    """Yield enriched_tweets rows for a handle in tweet_id order, one page
    at a time (memory = one page). ``on_page(fetched_so_far)`` is called
    after each page for progress reporting."""
    last_id, fetched = None, 0
    while True:
        params = {
            "username": f"ilike.{handle}",
            "order": "tweet_id.asc",
            "limit": page_size,
        }
        if last_id is not None:
            params["tweet_id"] = f"gt.{last_id}"
        rows = _get("enriched_tweets", params)
        for row in rows:
            yield row
        fetched += len(rows)
        if rows:
            last_id = rows[-1]["tweet_id"]
        if on_page:
            on_page(fetched)
        if len(rows) < page_size:
            return


def to_export_entry(row):
    """Map an enriched_tweets row to the ``{"tweet": {...}}`` shape found
    in a native export's data/tweets.js, so the Twitter importer's
    compact_row() applies unchanged."""
    created = row["created_at"]
    if isinstance(created, str):
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    else:
        dt = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    tweet_id = str(row["tweet_id"])
    return {"tweet": {
        "id": tweet_id,
        "id_str": tweet_id,
        "full_text": row.get("full_text") or "",
        "created_at": dt.strftime("%a %b %d %H:%M:%S +0000 %Y"),
        "favorite_count": str(row.get("favorite_count") or 0),
        "retweet_count": str(row.get("retweet_count") or 0),
        "in_reply_to_status_id_str": row.get("reply_to_tweet_id"),
        "in_reply_to_user_id_str": row.get("reply_to_user_id"),
        "in_reply_to_screen_name": row.get("reply_to_username"),
        "lang": "und",
        "truncated": False,
        "source": "community-archive",
        "entities": {},
    }}
