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
import os
import pathlib
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

# Nightly parquet export (whole corpus: tweets.parquet ~900 MB, profiles
# ~130 KB). latest.json names the current export; versioned URLs expire, so
# always resolve through the manifest.
EXPORT_BUCKET = (
    f"{SUPABASE_URL}/storage/v1/object/public/community-archive-public-export")
LATEST_MANIFEST_URL = f"{EXPORT_BUCKET}/latest.json"
SNAPSHOT_FILES = ("tweets.parquet", "profiles.parquet")
DOWNLOAD_CHUNK = 8 << 20  # 8 MB
# duckdb defaults to 80% of RAM and all cores — far too greedy for a
# 512 MB worker. A filtered scan of a 900 MB parquet streams row groups,
# so a small cap is plenty.
DUCKDB_MEMORY_LIMIT = "256MB"


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
        "select": "account_id,username,account_display_name,num_tweets,created_via",
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


# ── parquet snapshot ──────────────────────────────────────────────────────

def _urlopen(url):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"apikey": ANON_KEY}), timeout=TIMEOUT)


def fetch_latest_manifest():
    """The public latest.json: {export_id, package_paths, manifest_url, ...}."""
    with _urlopen(LATEST_MANIFEST_URL) as resp:
        return json.load(resp)


def snapshot_export_id(snapshot_dir):
    """export_id of the snapshot cached in ``snapshot_dir`` (None if absent
    or incomplete)."""
    d = pathlib.Path(snapshot_dir)
    marker = d / "export_id"
    if not marker.exists() or not all((d / f).exists() for f in SNAPSHOT_FILES):
        return None
    return marker.read_text().strip() or None


def ensure_snapshot(snapshot_dir, on_progress=None, manifest=None):
    """Make ``snapshot_dir`` hold the latest nightly export; download only
    when the export_id changed. Files stream to ``<name>.part`` and are
    renamed on completion, so a crashed download never masquerades as a
    snapshot. ``on_progress(filename, bytes_done, bytes_total)``.

    Returns the export_id in place."""
    manifest = manifest or fetch_latest_manifest()
    export_id = manifest["export_id"]
    d = pathlib.Path(snapshot_dir)
    if snapshot_export_id(d) == export_id:
        return export_id
    d.mkdir(parents=True, exist_ok=True)
    by_name = {os.path.basename(p): p for p in manifest.get("package_paths", [])}
    for name in SNAPSHOT_FILES:
        if name not in by_name:
            raise CommunityArchiveError(
                f"Community Archive export {export_id} has no {name}")
        url = f"{EXPORT_BUCKET}/{by_name[name]}"
        part = d / f"{name}.part"
        with _urlopen(url) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0) or None
            done = 0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(name, done, total)
        os.replace(part, d / name)
    (d / "export_id").write_text(export_id)
    return export_id


def _duckdb(snapshot_dir):
    import duckdb  # lazy: the CLI's REST path and the tests don't need it
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute("SET threads=1")
    d = pathlib.Path(snapshot_dir)
    return con, str(d / "tweets.parquet"), str(d / "profiles.parquet")


def fetch_account_parquet(handle, snapshot_dir):
    """Same shape as fetch_account(), from profiles.parquet."""
    con, _, profiles = _duckdb(snapshot_dir)
    row = con.execute(
        "select account_id, username, display_name, num_tweets "
        "from read_parquet(?) where lower(username) = lower(?)",
        [profiles, (handle or "").strip().lstrip("@")]).fetchone()
    if not row:
        return None
    return dict(zip(
        ["account_id", "username", "account_display_name", "num_tweets"], row))


def iter_tweets_parquet(account_id, snapshot_dir, batch=1000, on_page=None):
    """Yield enriched_tweets-shaped rows for an account from the snapshot,
    in tweet_id order, ``batch`` rows in memory at a time.
    reply_to_username resolves via profiles.parquet (None when the
    replied-to account isn't in the archive)."""
    con, tweets, profiles = _duckdb(snapshot_dir)
    cur = con.execute(
        "select t.tweet_id, "
        # ISO text with an explicit offset: duckdb needs pytz to return
        # TIMESTAMPTZ as aware datetimes; to_export_entry parses strings.
        "strftime(t.created_at at time zone 'UTC', '%Y-%m-%dT%H:%M:%S+00:00') "
        "as created_at, t.full_text, t.favorite_count, "
        "t.retweet_count, t.reply_to_tweet_id, "
        "t.reply_to_account_id as reply_to_user_id, "
        "p.username as reply_to_username "
        "from read_parquet(?) t "
        "left join read_parquet(?) p on p.account_id = t.reply_to_account_id "
        "where t.account_id = ? "
        "order by try_cast(t.tweet_id as bigint), t.tweet_id",
        [tweets, profiles, str(account_id)])
    cols = [c[0] for c in cur.description]
    fetched = 0
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for r in rows:
            yield dict(zip(cols, r))
        fetched += len(rows)
        if on_page:
            on_page(fetched)


# ── coverage check (before paying for an import) ──────────────────────────

CHECK_SCAN_LIMIT = 20000  # rows the REST summary will walk before giving up on detail


def count_archived(account_id):
    """Exact number of tweets the archive holds for an account — from the
    Content-Range header, independent of paging."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/tweets?account_id=eq.{account_id}&select=tweet_id",
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 "Prefer": "count=exact", "Range": "0-0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        total = (resp.headers.get("Content-Range") or "/").split("/")[-1]
    return int(total) if total.isdigit() else None


def _summarize_rows(rows):
    total = retweets = replies = chars = 0
    seen = set()
    for r in rows:
        if r["tweet_id"] in seen:
            continue
        seen.add(r["tweet_id"])
        total += 1
        text = r.get("full_text") or ""
        if text.startswith("RT @"):
            retweets += 1
            continue
        if r.get("reply_to_tweet_id"):
            replies += 1
        chars += len(text)
    return {"archived": total, "retweets": retweets, "replies": replies,
            "originals": total - retweets - replies,
            "est_tokens": chars // 4}


def coverage_summary(handle, snapshot_dir=None, scan_limit=CHECK_SCAN_LIMIT):
    """What the archive actually holds for a handle, so a pre-fill can be
    judged before it runs. Reads a cached parquet snapshot when one is
    given and the account is in it; otherwise walks the REST view (up to
    ``scan_limit`` rows — beyond that only the exact count is reported).

    Returns None for unknown handles; else {account_id, username,
    account_num_tweets, ingestion, archived, retweets, replies, originals,
    est_tokens, detail_source}. ``ingestion`` is the archive's
    ``created_via``: 'twitter_import' = browser-extension / timeline
    ingestion (partial, grows over time), other values = an uploaded
    data export."""
    account = fetch_account(handle)
    if not account:
        return None
    out = {
        "account_id": account["account_id"], "username": account["username"],
        "account_num_tweets": account.get("num_tweets") or 0,
        "ingestion": account.get("created_via"),
    }
    archived = count_archived(account["account_id"])
    if snapshot_dir and snapshot_export_id(snapshot_dir):
        con, tweets, _ = _duckdb(snapshot_dir)
        row = con.execute(
            "select count(*), "
            "sum(case when full_text like 'RT @%' then 1 else 0 end), "
            "sum(case when full_text not like 'RT @%' and reply_to_tweet_id is not null "
            "then 1 else 0 end), "
            "sum(case when full_text like 'RT @%' then 0 else length(full_text) end) "
            "from read_parquet(?) where account_id = ?",
            [tweets, account["account_id"]]).fetchone()
        if row and row[0]:
            n, rt, rp, chars = int(row[0]), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)
            out.update({"archived": n, "retweets": rt, "replies": rp,
                        "originals": n - rt - rp, "est_tokens": chars // 4,
                        "detail_source": "parquet"})
            # The live archive may hold more than the nightly snapshot.
            if archived is not None and archived > n:
                out["archived_live"] = archived
            return out
    if archived is not None and archived > scan_limit:
        out.update({"archived": archived, "retweets": None, "replies": None,
                    "originals": None, "est_tokens": None,
                    "detail_source": "count_only"})
        return out
    out.update(_summarize_rows(iter_tweets(account["username"])))
    out["detail_source"] = "rest"
    if archived is not None and archived != out["archived"]:
        out["archived_live"] = archived
    return out
