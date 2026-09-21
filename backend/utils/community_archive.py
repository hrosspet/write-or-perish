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
import contextlib
import fcntl
import json
import os
import pathlib
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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


_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,64}")


class CommunityArchiveError(Exception):
    """User-facing problem (unknown handle, API failure)."""


def _get(table, params, timeout=None):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?{query}",
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
        return json.load(resp)


def fetch_account(handle, timeout=None):
    """{account_id, username, account_display_name, num_tweets} for the one
    archive account whose username equals ``handle`` (case-insensitively),
    or None. Raises CommunityArchiveError when several accounts match.

    The REST filter is ``ilike``, whose ``_`` and ``%`` are wildcards, so
    ``jane_doe`` also fetches ``jane1doe``; rows are therefore matched
    exactly here, never taken as returned. Loore keys X logins on the id
    this returns, so a look-alike's id would let that other X user into
    the account. Same exact rule as fetch_account_parquet."""
    handle = (handle or "").strip().lstrip("@")
    # X handles are [A-Za-z0-9_]; anything else cannot be in the archive,
    # and "*" / "%" would be wildcards in the filter below.
    if not _HANDLE_RE.fullmatch(handle):
        return None
    rows = _get("all_account", {
        # "_" is ilike's single-character wildcard: escaped, or "_____"
        # matches every five-letter username and can overrun the server's
        # row cap, dropping the exact match or hiding a duplicate.
        "username": "ilike." + handle.replace("_", "\\_"),
        "select": "account_id,username,account_display_name,num_tweets,created_via",
    }, **({"timeout": timeout} if timeout else {}))
    exact = {}
    for row in rows:
        if (row.get("username") or "").lower() == handle.lower():
            exact.setdefault(str(row.get("account_id")), row)
    if len(exact) > 1:
        raise CommunityArchiveError(
            f"@{handle}: {len(exact)} Community Archive accounts have exactly "
            "that username; refusing to guess which one")
    return next(iter(exact.values()), None)


def iter_tweets(account_id, page_size=PAGE_SIZE, on_page=None, timeout=None):
    """Yield enriched_tweets rows for an account in tweet_id order, one page
    at a time (memory = one page). ``on_page(fetched_so_far)`` is called
    after each page for progress reporting. Keyed on the account id, not
    the username: a username filter is ``ilike`` (``_`` is a wildcard) and
    could pull a look-alike account's tweets into the wrong archive."""
    last_id, fetched = None, 0
    while True:
        params = {
            "account_id": f"eq.{account_id}",
            "order": "tweet_id.asc",
            "limit": page_size,
        }
        if last_id is not None:
            params["tweet_id"] = f"gt.{last_id}"
        rows = _get("enriched_tweets", params, **({"timeout": timeout} if timeout else {}))
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


@contextlib.contextmanager
def _snapshot_lock(snapshot_dir, shared=False):
    """One downloader per snapshot dir on this host, and no swap under a
    reader. The downloader takes the lock exclusively; a render or a
    lookup takes it shared, so it never sees half a swap (the two
    parquets and the export marker are replaced one after another) and
    a read that starts during a download waits for it — minutes at
    most, bounded by the transfer (a stalled one raises after TIMEOUT)
    — and then reads the fresh export. An fcntl lock dies with its
    process, so a crashed holder never leaves the dir locked."""
    d = pathlib.Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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
    with _snapshot_lock(d):
        if snapshot_export_id(d) == export_id:
            return export_id  # another caller downloaded it meanwhile
        return _download_snapshot(d, manifest, on_progress)


def refresh_snapshot(snapshot_dir):
    """Bring an EXISTING snapshot up to the latest nightly export. Returns
    (export_id, refreshed). (None, False) when nothing is cached: the
    first copy is fetched by the pre-fill import or the CLI, never as a
    side effect of a read (a gigabyte per deploy on staging otherwise)."""
    current = snapshot_export_id(snapshot_dir)
    if not current:
        return None, False
    manifest = fetch_latest_manifest()
    latest = manifest["export_id"]
    if latest == current:
        return current, False
    ensure_snapshot(snapshot_dir, manifest=manifest)
    return latest, True


def _download_snapshot(d, manifest, on_progress):
    export_id = manifest["export_id"]
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


def count_parquet(account_id, snapshot_dir):
    """Rows the cached snapshot holds for an account (0 if absent)."""
    con, tweets, _ = _duckdb(snapshot_dir)
    row = con.execute("select count(*) from read_parquet(?) where account_id = ?",
                      [tweets, str(account_id)]).fetchone()
    return int(row[0]) if row else 0


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
    out.update(_summarize_rows(iter_tweets(account["account_id"])))
    out["detail_source"] = "rest"
    if archived is not None and archived != out["archived"]:
        out["archived_live"] = archived
    return out


# ── recent-tweets render for the {ca_tweets} placeholder (PoC) ────────────

def snapshot_newest_tweet_at(snapshot_dir):
    """UTC 'YYYY-MM-DD HH:MM:SS' of the newest tweet in the cached
    snapshot, or None when the snapshot is absent."""
    if not snapshot_export_id(snapshot_dir):
        return None
    con, tweets, _ = _duckdb(snapshot_dir)
    row = con.execute(
        "select strftime(max(created_at) at time zone 'UTC', "
        "'%Y-%m-%d %H:%M:%S') from read_parquet(?)", [tweets]).fetchone()
    return row[0] if row else None


CA_CITATION_RE = re.compile(r"(?<![\w/#\[])#(\d{1,6})\b")


def render_recent_tweets(snapshot_dir, days=1, exclude_usernames=(),
                         include_usernames=None, exclude_tweet_ids=()):
    """The last ``days`` days of the whole Community Archive corpus as
    prompt text: one ``# Tweets by <user>`` section per account, one
    ``[#n] text`` line per tweet in time order, retweets dropped like
    the import drops them. ``n`` is a sequential number within the
    render (2 tokens) instead of the 19-digit tweet id (~10 tokens, and
    a copy-fidelity hazard when the model cites 20 of them out of 5k);
    the model cites ``#n`` and expand_ca_citations() turns that into the
    x.com link. No per-tweet timestamp: the window is in the header,
    order is preserved by the numbering, and a stamp was ~10 tokens per
    tweet (a quarter of the prompt) for nothing the model could use. The window ends at the newest tweet in the snapshot (the
    export lags a few hours; see placeholders.py).

    ``exclude_usernames`` drops those accounts (case-insensitive) — the
    reader's own handle, so the feed never recommends their own tweets.
    ``include_usernames`` (None = everyone) keeps only those accounts:
    the follows scope. ``exclude_tweet_ids`` drops those tweets: what the
    reader has already seen (read-marked picks, bookmarks), which must
    never reach the model as candidates again; how many fell in the
    window is reported as ``excluded`` in stats and named in the header.

    Returns (text, stats, refs) — stats: {export_id, window_start,
    window_end (UTC 'YYYY-MM-DD HH:MM'), window_start_at, window_end_at
    (datetimes), tweets, accounts, excluded, scope}; refs: {n: {username,
    tweet_id, text, posted_at}} — everything a pick needs to become a
    saved reference. The render is deterministic for a snapshot and an
    exclusion set (ordered by username, time, id); neither is stable over
    a batch's lifetime, which is why the submit pins the numbering (see
    FeedRender) instead of re-rendering on collect.
    Raises CommunityArchiveError when no snapshot is cached (this PoC
    never downloads one in the request path — a day's render must not
    wait on a 900 MB download)."""
    export_id = snapshot_export_id(snapshot_dir)
    if not export_id:
        raise CommunityArchiveError(
            f"No Community Archive snapshot cached at {snapshot_dir}")
    newest = snapshot_newest_tweet_at(snapshot_dir)
    excluded = sorted({(u or "").strip().lstrip("@").lower()
                       for u in exclude_usernames if u}) or ["\0"]
    included = None
    if include_usernames is not None:
        included = sorted({(u or "").strip().lstrip("@").lower()
                           for u in include_usernames if u}) or ["\0"]
    seen = {str(i) for i in exclude_tweet_ids if i}
    with _snapshot_lock(snapshot_dir, shared=True):
        return _render_recent_tweets(
            snapshot_dir, export_id, newest, days, excluded, included, seen)


def _render_recent_tweets(snapshot_dir, export_id, newest, days, excluded,
                          included, seen):
    con, tweets, profiles = _duckdb(snapshot_dir)
    # Window bound computed in Python: duckdb can't correlate a subquery
    # through the outer join, and a naive-UTC comparison keeps the
    # TIMESTAMPTZ column out of the parameter path (no pytz needed).
    from_where = (
        "from read_parquet(?) t "
        "left join read_parquet(?) p on p.account_id = t.account_id "
        "where (t.created_at at time zone 'UTC') "
        "> (?::TIMESTAMP - to_days(?)) "
        "and t.full_text not like 'RT @%' "
        "and lower(coalesce(p.username, t.account_id)) not in "
        "(select unnest(?::VARCHAR[])) "
        + ("and lower(coalesce(p.username, t.account_id)) in "
           "(select unnest(?::VARCHAR[])) " if included is not None else ""))
    params = ([tweets, profiles, newest, int(days), excluded]
              + ([included] if included is not None else []))
    # Seen tweets are skipped (and counted) in the row loop below rather
    # than in SQL: one scan of the parquet, not two.
    excluded_tweets = 0
    cur = con.execute(
        "select coalesce(p.username, t.account_id) as username, "
        "t.tweet_id, t.full_text, "
        "strftime(t.created_at at time zone 'UTC', '%Y-%m-%d %H:%M:%S') "
        "as posted "
        + from_where
        + "order by lower(coalesce(p.username, t.account_id)), t.created_at, "
        "t.tweet_id",
        params)
    sections = []
    body = []
    current = None
    count = 0
    total = 0
    refs = {}

    def flush():
        if current is None:
            return
        sections.append(
            f"# Tweets by {current} (Community Archive) — {count} tweets")
        sections.append("")
        sections.extend(body)
        sections.append("---")
        sections.append("")

    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for username, tweet_id, text, posted in rows:
            if str(tweet_id) in seen:
                excluded_tweets += 1
                continue
            if username != current:
                flush()
                current, count, body = username, 0, []
            count += 1
            total += 1
            text = (text or "").strip()
            refs[total] = {
                "username": username, "tweet_id": str(tweet_id),
                "text": text,
                "posted_at": datetime.strptime(posted, "%Y-%m-%d %H:%M:%S")
                if posted else None,
            }
            body.append(f"[#{total}] {text}")
            body.append("")
    flush()
    accounts = sum(1 for line in sections if line.startswith("# Tweets by "))
    end = datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")
    start = end - timedelta(days=int(days))
    newest = end.strftime("%Y-%m-%d %H:%M")
    window_start = start.strftime("%Y-%m-%d %H:%M")
    scope_note = (" Only accounts the reader follows." if included is not None
                  else "")
    seen_note = (f" {excluded_tweets} tweets the reader had already seen "
                 "(read earlier, or bookmarked) are left out."
                 if excluded_tweets else "")
    header = (
        f"# Community Archive — tweets from {window_start} to {newest} UTC "
        f"(last {int(days)} day(s) of export {export_id}): {total} tweets "
        f"by {accounts} accounts, retweets omitted.{scope_note}{seen_note} "
        f"Each tweet is numbered; cite a tweet by its number, e.g. #123.")
    text = "\n".join([header, ""] + sections).rstrip() + "\n"
    stats = {"export_id": export_id, "window_start": window_start,
             "window_end": newest, "window_start_at": start,
             "window_end_at": end, "tweets": total, "accounts": accounts,
             "excluded": excluded_tweets,
             "scope": "follows" if included is not None else "all"}
    return text, stats, refs


def fetch_tweets_by_id(snapshot_dir, tweet_ids):
    """{tweet_id: {username, tweet_id, text, posted_at}} for the given
    ids, from the cached snapshot — the same shape as a render's refs.
    The collect of a pinned batch resolves its few picks this way instead
    of re-rendering the day (which would number the tweets differently
    once the snapshot or the reader's seen set changed). Ids the snapshot
    no longer holds are simply absent."""
    ids = sorted({str(i) for i in tweet_ids if i})
    if not ids:
        return {}
    with _snapshot_lock(snapshot_dir, shared=True):
        con, tweets, profiles = _duckdb(snapshot_dir)
        rows = con.execute(
            "select coalesce(p.username, t.account_id), t.tweet_id, "
            "t.full_text, "
            "strftime(t.created_at at time zone 'UTC', '%Y-%m-%d %H:%M:%S') "
            "from read_parquet(?) t "
            "left join read_parquet(?) p on p.account_id = t.account_id "
            "where t.tweet_id in (select unnest(?::VARCHAR[]))",
            [tweets, profiles, ids]).fetchall()
    out = {}
    for username, tweet_id, text, posted in rows:
        out[str(tweet_id)] = {
            "username": username, "tweet_id": str(tweet_id),
            "text": (text or "").strip(),
            "posted_at": datetime.strptime(posted, "%Y-%m-%d %H:%M:%S")
            if posted else None,
        }
    return out


def following_handles(snapshot_dir, username):
    """The X accounts ``username`` follows, from
    ``<snapshot_dir>/following/<username>.json`` ({"usernames": [...]},
    saved from a live pull — the archive's own following data only
    comes from uploads and goes stale). None when no list is saved."""
    path = pathlib.Path(snapshot_dir) / "following" / f"{username}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return [u for u in data.get("usernames", []) if u]


def expand_ca_citations(reply, refs):
    """Turn ``#n`` citations in a model reply into markdown links to the
    tweet: ``#12`` → ``[#12](https://x.com/<user>/status/<id>)``. Numbers
    not in ``refs`` (a hashtag, a year) are left alone; a ``#n`` already
    inside a link or URL is skipped by the lookbehind."""
    if not reply or not refs:
        return reply

    def sub(m):
        n = int(m.group(1))
        ref = refs.get(n)
        if not ref:
            return m.group(0)
        return (f"[#{n}](https://x.com/{ref['username']}/status/"
                f"{ref['tweet_id']})")
    return CA_CITATION_RE.sub(sub, reply)
