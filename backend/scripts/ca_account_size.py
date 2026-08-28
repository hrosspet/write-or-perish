"""How much of a Twitter account is in the Community Archive — from the local
Parquet snapshot (own tweets, chars, ≈tokens via chars/2) AND from the live
CA REST API (``num_tweets``, which is current even when the snapshot is stale).
Accounts not in CA at all fall back to the X API (app-only auth from
``TWITTER_API_KEY`` / ``TWITTER_API_SECRET``, env or repo ``.env``) for the
public tweet count and the pay-per-use cost of pulling them.

    python backend/scripts/ca_account_size.py RichDecibels [more handles...] \
        [--parquet ~/data/twitter/community-archive-snapshot-2026-08-25] [--no-api]

Snapshot counts exclude retweets (RT text isn't the user's writing).
Needs ``duckdb`` in the env for the snapshot part.
"""
import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from backend.utils.community_archive import (  # noqa: E402
    ANON_KEY, SUPABASE_URL, TIMEOUT, _duckdb, fetch_account, fetch_account_parquet)

DEFAULT_SNAPSHOT = "~/data/twitter/community-archive-snapshot-2026-08-25"
CHARS_PER_TOKEN = 2.0
# X API pay-per-use (docs.x.com/x-api/getting-started/pricing, Aug 2026)
X_COST_PER_POST_READ = 0.005
X_COST_PER_USER_READ = 0.010


def snapshot_size(handle, snapshot_dir):
    acct = fetch_account_parquet(handle, snapshot_dir)
    if not acct:
        return None
    con, tweets, _ = _duckdb(snapshot_dir)
    n, chars, first, last = con.execute(
        "select count(*), coalesce(sum(length(full_text)), 0), "
        "cast(min(created_at) as date)::varchar, cast(max(created_at) as date)::varchar "
        "from read_parquet(?) where account_id = ? and retweeted_tweet_id is null",
        [tweets, acct["account_id"]]).fetchone()
    return {**acct, "own_tweets": n, "chars": chars,
            "tokens": chars / CHARS_PER_TOKEN, "first": first, "last": last}


def _ca_exact_count(table, account_id):
    """Server-side exact row count via PostgREST ``Prefer: count=exact`` (no rows fetched)."""
    q = urllib.parse.urlencode({"account_id": f"eq.{account_id}", "select": "tweet_id", "limit": 0})
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?{q}",
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 "Prefer": "count=exact", "Range-Unit": "items"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return int(resp.headers["Content-Range"].split("/")[1])


def api_size(handle):
    """CA profile metadata plus the ACTUAL number of tweets stored in the archive.
    ``num_tweets`` is the account's lifetime counter; extension-only opt-ins can
    have hundreds listed but only a handful actually archived."""
    try:
        a = fetch_account(handle)
        if a:
            a["archived"] = _ca_exact_count("tweets", a["account_id"])
        return a
    except Exception as e:  # network / API hiccup shouldn't kill the snapshot report
        return {"error": str(e)}


def _x_credentials():
    key, secret = os.environ.get("TWITTER_API_KEY"), os.environ.get("TWITTER_API_SECRET")
    if key and secret:
        return key, secret
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    vals = {}
    if os.path.exists(env):
        for line in open(env):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    key, secret = vals.get("TWITTER_API_KEY"), vals.get("TWITTER_API_SECRET")
    return (key, secret) if key and secret else None


_x_bearer = None


def x_size(handle):
    """{tweet_count, protected, cost} from the X API v2 user lookup (1 user read ≈ $0.01)."""
    global _x_bearer
    creds = _x_credentials()
    if not creds:
        return {"error": "no TWITTER_API_KEY/SECRET"}
    try:
        if not _x_bearer:
            req = urllib.request.Request(
                "https://api.twitter.com/oauth2/token",
                data=b"grant_type=client_credentials",
                headers={"Authorization": "Basic " + base64.b64encode(
                    f"{creds[0]}:{creds[1]}".encode()).decode()})
            _x_bearer = json.load(urllib.request.urlopen(req, timeout=20))["access_token"]
        q = urllib.parse.urlencode({"user.fields": "public_metrics,protected"})
        req = urllib.request.Request(
            f"https://api.twitter.com/2/users/by/username/{handle}?{q}",
            headers={"Authorization": f"Bearer {_x_bearer}"})
        d = json.load(urllib.request.urlopen(req, timeout=20)).get("data")
    except Exception as e:
        return {"error": str(e)}
    if not d:
        return None
    n = d["public_metrics"]["tweet_count"]
    return {"tweet_count": n, "protected": d.get("protected"),
            "cost": n * X_COST_PER_POST_READ + X_COST_PER_USER_READ}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="+")
    ap.add_argument("--parquet", default=DEFAULT_SNAPSHOT, metavar="SNAPSHOT_DIR")
    ap.add_argument("--no-api", action="store_true", help="skip the live CA REST lookup")
    args = ap.parse_args()
    snap = os.path.expanduser(args.parquet)
    for h in args.handles:
        h = h.lstrip("@")
        s = snapshot_size(h, snap)
        if s:
            snap_str = (f"{s['own_tweets']:,} own tweets ({s['num_tweets']:,} listed), "
                        f"{s['chars']:,} chars ≈ {s['tokens']:,.0f} tokens, {s['first']} → {s['last']}")
        else:
            snap_str = "not in snapshot"
        if args.no_api:
            api_str = ""
        else:
            a = api_size(h)
            if a is None:
                api_str = " | API: not in archive"
                if not s:
                    x = x_size(h)
                    if x is None:
                        api_str += " | X: no such user"
                    elif "error" in x:
                        api_str += f" | X: error ({x['error']})"
                    else:
                        api_str += (f" | X: {x['tweet_count']:,} tweets"
                                    f"{' (protected)' if x['protected'] else ''}"
                                    f", ≈ ${x['cost']:.2f} to fetch")
            elif "error" in a:
                api_str = f" | API: error ({a['error']})"
            else:
                api_str = f" | API: {a['archived']:,} archived (account reports {a['num_tweets']:,})"
        print(f"@{(s or {}).get('username', h)}: snapshot: {snap_str}{api_str}")


if __name__ == "__main__":
    main()
