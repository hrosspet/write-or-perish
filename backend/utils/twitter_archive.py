"""Twitter/X archive parsing that stays flat in memory, plus a per-user
on-disk stash between the analyze and confirm steps of the import.

Why this exists: ``data/tweets.js`` for a heavy account is a single JSON
array of 60k+ objects. ``json.loads`` of the whole thing materialises
every entry at once (hundreds of MB for a 25 MB file), and the old
analyze endpoint then echoed all of it back to the browser (23 MB) only
so the confirm step could POST the same list back up. That combination
OOM-killed the 512 MB staging backend on the first large archive.

Now analyze walks the array one element at a time with
``json.JSONDecoder.raw_decode`` (peak memory ≈ the decoded text plus one
tweet), writes the compact per-tweet rows it needs as JSON lines under
the shared ``data/`` volume, and hands the browser an opaque token. The
confirm step passes the token back and the Celery worker streams the
rows off disk.

The stash lives under ``<AUDIO_STORAGE_PATH>/../imports/<user_id>/`` —
the same ``data/`` volume the audio files use, which is the one path
that is shared between the web and worker containers in every
deployment. Files are removed when the import finishes and swept when
older than STASH_TTL regardless.
"""
import io
import json
import os
import pathlib
import re
import secrets
import time
import zipfile

STASH_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve().parent / "imports"
STASH_TTL_SECONDS = 24 * 3600
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def approximate_token_count(text):
    """~4 chars per token — the same heuristic the import routes use."""
    return len(text or "") // 4


class TwitterArchiveError(ValueError):
    """User-facing archive problem (missing/unparseable tweets.js)."""


CHUNK_CHARS = 1 << 20  # 1M characters per read while streaming tweets.js


def open_tweets_js(zip_source):
    """Return (ZipFile, text stream) for ``data/tweets.js`` inside an export
    zip; the caller closes the ZipFile. Raises TwitterArchiveError when
    the archive is invalid or has no tweets.js."""
    try:
        zf = zipfile.ZipFile(zip_source, "r")
    except zipfile.BadZipFile:
        raise TwitterArchiveError("Invalid zip file")
    for name in zf.namelist():
        if name == "data/tweets.js" or name.endswith("/data/tweets.js"):
            return zf, io.TextIOWrapper(zf.open(name), encoding="utf-8")
    zf.close()
    raise TwitterArchiveError(
        "Could not find data/tweets.js in the zip archive. "
        "Please upload the original Twitter/X data export."
    )


def read_tweets_js(zip_source):
    """Whole ``data/tweets.js`` as text (small archives / tests)."""
    zf, stream = open_tweets_js(zip_source)
    with zf:
        return stream.read()


def iter_tweets_js(text):
    """Yield tweets from an in-memory tweets.js string."""
    return iter_tweets_stream(io.StringIO(text))


def iter_tweets_stream(stream):
    """Yield one tweet dict per element of ``window.YTD.tweets.part0 =
    [...]`` while reading ``stream`` in CHUNK_CHARS pieces, so memory is
    bounded by the chunk size plus one tweet — not the file.

    Entries are ``{"tweet": {...}}`` in native exports; the wrapper is
    unwrapped here. Raises TwitterArchiveError on a malformed file.
    """
    decoder = json.JSONDecoder()
    buf = stream.read(CHUNK_CHARS)
    eof = len(buf) < CHUNK_CHARS
    # Header is "window.YTD.tweets.part0 = [" — keep reading until the
    # array opens (or give up at EOF / an implausibly long preamble).
    while True:
        start = buf.find("[", buf.find("=") if "=" in buf else 0)
        if start >= 0:
            break
        if eof or len(buf) > 4096:
            raise TwitterArchiveError("Could not parse tweets.js — unexpected format.")
        more = stream.read(CHUNK_CHARS)
        eof = len(more) < CHUNK_CHARS
        buf += more
    buf = buf[start + 1:]
    pos = 0
    while True:
        while pos < len(buf) and buf[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(buf) or buf[pos] != "]":
            try:
                entry, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError as e:
                if eof:
                    raise TwitterArchiveError(f"Failed to parse tweets JSON: {e}")
                # Element (or the whitespace after it) may be cut off by the
                # chunk boundary: drop what's consumed, read more, retry.
                more = stream.read(CHUNK_CHARS)
                eof = len(more) < CHUNK_CHARS
                buf = buf[pos:] + more
                pos = 0
                continue
            yield entry.get("tweet", entry) if isinstance(entry, dict) else entry
            pos = end
            continue
        return


def compact_row(tweet):
    """The subset of a raw tweet the import needs, or None for retweets."""
    full_text = tweet.get("full_text", "") or ""
    if full_text.startswith("RT @"):
        return None
    return {
        "id_str": tweet.get("id_str", "") or "",
        "full_text": full_text,
        "created_at": tweet.get("created_at", "") or "",
        "is_reply": bool(tweet.get("in_reply_to_status_id_str")),
        "in_reply_to_screen_name": tweet.get("in_reply_to_screen_name"),
        "favorite_count": int(tweet.get("favorite_count", 0) or 0),
        "retweet_count": int(tweet.get("retweet_count", 0) or 0),
        "token_count": approximate_token_count(full_text),
    }


def analyze_archive(zip_source):
    """Small-archive convenience: rows (sorted by created_at) + summary,
    all in memory. The import route uses analyze_archive_to_stash."""
    zf, stream = open_tweets_js(zip_source)
    rows, skipped = [], 0
    with zf:
        tweets = list(iter_tweets_stream(stream))
    for tweet in tweets:
        row = compact_row(tweet)
        if row is None:
            skipped += 1
        else:
            rows.append(row)
    rows.sort(key=lambda r: _sort_key(r["created_at"]))
    return rows, _summarize(rows, skipped)


def analyze_archive_to_stash(zip_source, path):
    """Walk an export once and write its compact rows to ``path`` sorted
    by created_at, holding only one read chunk plus one tweet at a time.

    Two passes on disk: rows go to ``<path>.unsorted`` as they stream by
    (remembering just (sort_key, offset, length) per row), then are
    copied into ``path`` in order via seek. Peak memory is therefore
    independent of the number of tweets except for the index tuples.

    Returns the summary dict for the confirm dialog.
    """
    zf, stream = open_tweets_js(zip_source)
    tmp = pathlib.Path(str(path) + ".unsorted")
    index = []  # (sort_key, offset, length)
    counts = {"total": 0, "originals": 0, "skipped": 0,
              "tokens": 0, "original_tokens": 0, "size": 0}
    with zf, open(tmp, "wb") as f:
        for tweet in iter_tweets_stream(stream):
            row = compact_row(tweet)
            if row is None:
                counts["skipped"] += 1
                continue
            line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
            index.append((_sort_key(row["created_at"]), f.tell(), len(line)))
            f.write(line)
            counts["total"] += 1
            counts["tokens"] += row["token_count"]
            counts["size"] += len(row["full_text"].encode("utf-8"))
            if not row["is_reply"]:
                counts["originals"] += 1
                counts["original_tokens"] += row["token_count"]
    index.sort(key=lambda t: t[0])
    with open(tmp, "rb") as src, open(path, "wb") as dst:
        for _, offset, length in index:
            src.seek(offset)
            dst.write(src.read(length))
    os.remove(tmp)
    return {
        "total_tweets": counts["total"],
        "original_count": counts["originals"],
        "reply_count": counts["total"] - counts["originals"],
        "skipped_retweets": counts["skipped"],
        "total_tokens": counts["tokens"],
        "original_tokens": counts["original_tokens"],
        "total_size": counts["size"],
    }


def _summarize(rows, skipped_retweets):
    originals = [r for r in rows if not r["is_reply"]]
    return {
        "total_tweets": len(rows),
        "original_count": len(originals),
        "reply_count": len(rows) - len(originals),
        "skipped_retweets": skipped_retweets,
        "total_tokens": sum(r["token_count"] for r in rows),
        "original_tokens": sum(r["token_count"] for r in originals),
        "total_size": sum(len(r["full_text"].encode("utf-8")) for r in rows),
    }


def _sort_key(created_at):
    """Chronological key for the export's 'Wed Aug 26 08:17:00 +0000 2026'
    strings; falls back to the raw string when unparseable."""
    from datetime import datetime
    try:
        return (0, datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except (ValueError, TypeError):
        return (1, created_at)


# ---- stash ----------------------------------------------------------------

def _user_dir(user_id):
    return STASH_ROOT / str(int(user_id))


def stash_path(user_id, token):
    """Path for a token, or None when the token is malformed (never
    trust it into a filesystem path)."""
    if not token or not _TOKEN_RE.match(token):
        return None
    return _user_dir(user_id) / f"{token}.jsonl"


def stash_new(user_id):
    """Allocate a fresh (token, path) for a user; sweeps expired files."""
    sweep_expired()
    d = _user_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    return token, stash_path(user_id, token)


def stash_write(user_id, rows):
    """Write rows as JSON lines; returns the token."""
    token, path = stash_new(user_id)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
    return token


def stash_iter(path):
    """Yield rows from a stash file, one at a time."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def stash_count(path):
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def stash_delete(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def sweep_expired(now=None):
    """Remove stash files older than STASH_TTL (abandoned imports)."""
    if not STASH_ROOT.exists():
        return
    now = now or time.time()
    for user_dir in STASH_ROOT.iterdir():
        if not user_dir.is_dir():
            continue
        for f in user_dir.glob("*.jsonl"):
            try:
                if now - f.stat().st_mtime > STASH_TTL_SECONDS:
                    f.unlink()
            except OSError:
                pass
