"""Fetch a Twitter account's tweets from the Community Archive and write them
as a Twitter/X data-export-shaped zip that Loore's Twitter importer accepts.

Community Archive (https://www.community-archive.org) is an opt-in public
corpus of user-uploaded Twitter archives. This pulls the ``enriched_tweets``
view through its public Supabase REST API (anon key is published in their
docs) and re-serialises each row into the native export format
(``data/tweets.js`` = ``window.YTD.tweets.part0 = [...]``), so the result
can be uploaded via Loore's "Import Twitter/X archive" flow as-is.

Usage (from anywhere; stdlib only — shares the client in
backend/utils/community_archive.py, which the admin pre-fill uses too):

    python backend/scripts/fetch_community_archive.py TylerAlterman ~/data/twitter

Writes ``<output_dir>/community-archive-<handle>.zip``.

Big accounts (tens of thousands of tweets) are slow through the REST API
(1000-row keyset pages). Point ``--parquet`` at a local nightly
snapshot directory (``tweets.parquet`` + ``profiles.parquet``, see
``latest.json`` in their public bucket) to read from disk instead — needs
``duckdb`` in the env:

    python backend/scripts/fetch_community_archive.py RichDecibels ~/data/twitter \
        --parquet ~/data/twitter/community-archive-snapshot-2026-08-25
"""
import argparse
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from backend.utils.community_archive import (  # noqa: E402
    fetch_account, iter_tweets, to_export_entry)


def fetch_tweets(handle):
    return list(iter_tweets(
        handle, on_page=lambda n: print(f"  fetched {n}", file=sys.stderr)))


def fetch_account_parquet(handle, snapshot_dir):
    import duckdb
    rows = duckdb.sql(f"""
        select account_id, username, display_name as account_display_name, num_tweets
        from '{snapshot_dir}/profiles.parquet' where lower(username) = lower('{handle}')
    """).fetchall()
    if not rows:
        return None
    return dict(zip(["account_id", "username", "account_display_name", "num_tweets"], rows[0]))


def fetch_tweets_parquet(account_id, snapshot_dir):
    """Same row shape as enriched_tweets, read from the local snapshot.
    reply_to_username is resolved via profiles.parquet (NULL when the
    replied-to account isn't in the archive)."""
    import duckdb
    rel = duckdb.sql(f"""
        select t.tweet_id, t.created_at, t.full_text, t.favorite_count, t.retweet_count,
               t.reply_to_tweet_id, t.reply_to_account_id as reply_to_user_id,
               p.username as reply_to_username
        from '{snapshot_dir}/tweets.parquet' t
        left join '{snapshot_dir}/profiles.parquet' p on p.account_id = t.reply_to_account_id
        where t.account_id = '{account_id}'
        order by cast(t.tweet_id as bigint)
    """)
    cols = [c for c in rel.columns]
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def build_zip(handle, output_dir, snapshot_dir=None):
    account = (fetch_account_parquet(handle, snapshot_dir) if snapshot_dir
               else fetch_account(handle))
    if not account:
        sys.exit(f"@{handle} not found in the Community Archive")
    print(f"@{account['username']}: {account['num_tweets']} tweets listed", file=sys.stderr)

    rows = (fetch_tweets_parquet(account["account_id"], snapshot_dir) if snapshot_dir
            else fetch_tweets(account["username"]))
    seen, entries = set(), []
    for row in rows:
        if row["tweet_id"] in seen:
            continue
        seen.add(row["tweet_id"])
        entries.append(to_export_entry(row))

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"community-archive-{account['username']}.zip")
    tweets_js = "window.YTD.tweets.part0 = " + json.dumps(entries, ensure_ascii=False, indent=2)
    account_js = "window.YTD.account.part0 = " + json.dumps([{"account": {
        "username": account["username"],
        "accountId": account["account_id"],
        "accountDisplayName": account.get("account_display_name") or "",
    }}])
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data/tweets.js", tweets_js)
        z.writestr("data/account.js", account_js)
    print(f"{len(entries)} tweets -> {out_path} ({os.path.getsize(out_path) // 1024} KB)")
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("handle", help="Twitter handle (with or without @)")
    parser.add_argument("output_dir", help="Directory to write community-archive-<handle>.zip into")
    parser.add_argument("--parquet", metavar="SNAPSHOT_DIR", default=None,
                        help="read from a local Parquet snapshot dir instead of the REST API")
    args = parser.parse_args()
    build_zip(args.handle.lstrip("@"), args.output_dir,
              os.path.expanduser(args.parquet) if args.parquet else None)


if __name__ == "__main__":
    main()
