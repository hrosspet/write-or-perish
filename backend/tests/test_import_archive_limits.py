"""Import uploads unpack within limits (backend/utils/archive_limits.py).

- The notes import (POST /api/import/analyze): the .md text of one upload
  is at most MAX_NOTES_TEXT_BYTES, everything unpacked from its members
  at most MAX_UNPACKED_BYTES_PER_UPLOAD; past either the whole upload is
  refused with a message, nothing of it is sent back, and a .docx member
  is copied out only within the limit. The largest answer fits the
  confirm's body limit. One analyze of an account at a time.
- The X archive import (POST /api/import/twitter/analyze): tweets.js is
  read up to MAX_TWEETS_JS_BYTES, one element up to
  MAX_TWEET_ELEMENT_CHARS, a stash holds at most MAX_TWEET_ROWS rows and
  MAX_STASH_BYTES; a refused or failed analyze leaves no file behind, and
  the sweep also removes the ``.unsorted`` file of an analyze that
  stopped part-way.
- Any zip: the entries are counted before the listing is read, and only
  stored or deflated members are read.
- Real-sized notes and archives are analyzed as before (controls).
"""
import bz2  # noqa: F401  (zipfile needs it for ZIP_BZIP2)
import io
import json
import os
import struct
import time
import zipfile

import pytest

from backend.tests.test_twitter_import import (  # noqa: F401
    app, _make_user, _login,
)
from backend.tests.test_docx_import import _docx
from backend.utils import archive_limits
from backend.utils import docx_import
from backend.utils import twitter_archive as ta

MB = 1024 * 1024


def _zip(entries, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression, compresslevel=9
                         if compression == zipfile.ZIP_DEFLATED else None) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _client(app, name="alice"):
    client = app.test_client()
    user = _make_user(name)
    _login(client, user.id)
    return client, user


def _post(client, url, upload, filename="a.zip"):
    return client.post(url, data={"zip_file": (io.BytesIO(upload), filename)},
                       content_type="multipart/form-data")


def _notes(client, upload):
    return _post(client, "/api/import/analyze", upload, "notes.zip")


def _twitter(client, upload):
    return _post(client, "/api/import/twitter/analyze", upload)


# ── Notes zip ────────────────────────────────────────────────────────────

def _words(size, seed=7):
    """*size* bytes of prose-like text (words from a vocabulary of 5,000),
    which compresses about as much as real notes do."""
    import random
    rnd = random.Random(seed)
    vocab = ["".join(rnd.choice("abcdefghijklmnopqrstuvwxyz")
                     for _ in range(rnd.randint(2, 10))) for _ in range(5000)]
    out, n = [], 0
    while n < size:
        line = " ".join(rnd.choice(vocab) for _ in range(12)) + ".\n"
        out.append(line)
        n += len(line)
    return "".join(out)[:size]


def test_text_over_the_limit_in_a_small_zip_is_refused(app):
    """65 MB of one repeated letter in a zip of under 100 KB: reading stops
    at MAX_NOTES_TEXT_BYTES, and the answer says to split the zip."""
    from backend.routes import import_data
    client, _ = _client(app)
    upload = _zip({"note.md": b"a" * (import_data.MAX_NOTES_TEXT_BYTES + MB)})
    assert len(upload) < 100 * 1024
    r = _notes(client, upload)
    assert r.status_code == 400
    assert "more text than Loore imports at once (64 MB)" in \
        r.get_json()["error"]
    assert "Split them into smaller zips" in r.get_json()["error"]
    assert len(r.data) < 10 * 1024


def test_md_text_over_the_limit_is_refused_and_not_sent_back(
        app, monkeypatch):
    from backend.routes import import_data
    monkeypatch.setattr(import_data, "MAX_NOTES_TEXT_BYTES", 2 * MB)
    client, _ = _client(app)
    upload = _many([(f"n{i}.md", _words(MB // 2, seed=i).encode())
                    for i in range(5)])
    r = _notes(client, upload)
    assert r.status_code == 400
    assert "more text than Loore imports at once (2 MB)" in \
        r.get_json()["error"]
    assert len(r.data) < 10 * 1024


def test_a_40_mb_vault_is_analyzed_and_fits_its_confirm(app):
    """3,000 notes with 40 MB of text (accepted before the text limit
    existed); the confirm the apps build from the answer fits the confirm's
    body limit."""
    from backend.utils.request_size import IMPORT_CONFIRM_MAX_BODY_BYTES
    client, _ = _client(app)
    block = _words(2 * MB)
    notes = [(f"Vault/note {i:04d}.md",
              block[(i * 7919) % MB:(i * 7919) % MB + 14_000].encode())
             for i in range(3000)]
    upload = _many(notes)
    r = _notes(client, upload)
    assert r.status_code == 200, r.get_json()
    files = r.get_json()["files"]
    assert len(files) == 3000
    assert sum(len(f["content"]) for f in files) > 40_000_000
    # As the web posts it (JSON.stringify keeps non-ASCII as it is); the
    # iPhone app also escapes "/" as "\/".
    body = json.dumps({"files": files, "import_type": "separate_nodes",
                       "date_ordering": "modified",
                       "privacy_level": "private", "ai_usage": "none"},
                      ensure_ascii=False).encode()
    assert len(body.replace(b"/", b"\\/")) < IMPORT_CONFIRM_MAX_BODY_BYTES


def test_one_analyze_of_an_account_at_a_time(app, monkeypatch):
    from backend.routes import import_data
    from backend.tests.fake_redis import install
    from backend.utils import run_locks
    redis = install(monkeypatch)
    client, user = _client(app)
    key = import_data.NOTES_ANALYZE_LOCK_KEY.format(user_id=user.id)
    upload = _zip({f"n{i}.md": b"hello" for i in range(3)})
    running = run_locks.RunLock(key, "another", 60)
    assert running.acquire()
    r = _notes(client, upload)
    assert r.status_code == 409
    assert r.get_json()["code"] == "import_running"
    assert r.get_json()["error"] == import_data.IMPORT_RUNNING_MESSAGE
    # Another account is not held up.
    from flask import g
    g.pop("_login_user", None)
    other, _ = _client(app, "bob")
    assert _notes(other, upload).status_code == 200
    g.pop("_login_user", None)
    running.release()
    # The key is refreshed before every file and freed at the end, also
    # when the upload is refused.
    beats = []
    real = run_locks.RunLock.beat

    def beat(self):
        beats.append(redis.get(key) is not None)
        return real(self)

    monkeypatch.setattr(run_locks.RunLock, "beat", beat)
    assert _notes(client, upload).status_code == 200
    assert beats == [True] * 3 and redis.get(key) is None
    assert _notes(client, b"not a zip").status_code == 400
    assert redis.get(key) is None


def test_a_stopped_analyze_frees_the_account_after_its_ttl(app, monkeypatch):
    from backend.routes import import_data
    from backend.tests.fake_redis import install
    from backend.utils import run_locks
    redis = install(monkeypatch)
    client, user = _client(app)
    stopped = run_locks.RunLock(
        import_data.NOTES_ANALYZE_LOCK_KEY.format(user_id=user.id), "gone",
        import_data.NOTES_ANALYZE_LOCK_TTL)
    assert stopped.acquire()
    upload = _zip({"n.md": b"hello"})
    assert _notes(client, upload).status_code == 409
    redis.advance(import_data.NOTES_ANALYZE_LOCK_TTL + 1)
    assert _notes(client, upload).status_code == 200


def test_the_most_notes_text_fits_the_confirm():
    """The largest analyze answer, with a quarter for JSON escaping and
    10 MB for the Word text and the files' names and dates, fits the
    confirm's body limit."""
    from backend.routes import import_data
    from backend.utils.request_size import IMPORT_CONFIRM_MAX_BODY_BYTES
    assert (import_data.MAX_NOTES_TEXT_BYTES * 1.25 + 10 * MB
            <= IMPORT_CONFIRM_MAX_BODY_BYTES)


def test_notes_over_the_limit_together_are_refused_before_reading(
        app, monkeypatch):
    """Several notes that fit one by one but not together: refused by the
    sizes the zip states, before any note is read."""
    from backend.routes import import_data
    monkeypatch.setattr(import_data, "MAX_NOTES_TEXT_BYTES", 3 * MB)
    client, _ = _client(app)
    read = []
    monkeypatch.setattr(archive_limits, "read_member",
                        lambda *a: read.append(1))
    r = _notes(client, _zip({f"n{i}.md": b"b" * MB for i in range(4)}))
    assert r.status_code == 400
    assert "more text than Loore imports at once" in r.get_json()["error"]
    assert read == []


def test_each_note_is_read_within_what_is_left_of_the_limit(
        app, monkeypatch):
    from backend.routes import import_data
    monkeypatch.setattr(import_data, "MAX_NOTES_TEXT_BYTES", 3 * MB + 10)
    client, _ = _client(app)
    limits = []
    real = archive_limits.read_member

    def spy(zf, info, limit):
        limits.append(limit)
        return real(zf, info, limit)

    monkeypatch.setattr(archive_limits, "read_member", spy)
    r = _notes(client, _zip({f"n{i}.md": b"b" * MB for i in range(3)}))
    assert r.status_code == 200
    assert limits == [3 * MB + 10, 2 * MB + 10, MB + 10]


class _Member:
    """A zip member whose header states 10 bytes and whose data is 5 MB."""
    compress_type = zipfile.ZIP_DEFLATED
    file_size = 10


class _Data(io.BytesIO):
    read_bytes = 0

    def read(self, n=-1):
        out = super().read(n)
        self.read_bytes += len(out)
        return out


class _Zip:
    def __init__(self):
        self.stream = _Data(b"x" * (5 * MB))

    def open(self, info):
        return self.stream


def test_a_member_is_held_to_its_limit_whatever_its_header_states():
    z = _Zip()
    with pytest.raises(archive_limits.ArchiveTooLarge):
        archive_limits.read_member(z, _Member(), MB)
    assert z.stream.read_bytes <= MB + archive_limits.READ_PIECE_BYTES
    z = _Zip()
    with pytest.raises(archive_limits.ArchiveTooLarge):
        archive_limits.copy_member(z, _Member(), io.BytesIO(), MB)
    assert z.stream.read_bytes <= MB + archive_limits.READ_PIECE_BYTES
    reader = archive_limits.BoundedReader(io.BytesIO(b"x" * (5 * MB)), MB)
    with pytest.raises(archive_limits.ArchiveTooLarge):
        io.BufferedReader(reader).read()


def test_docx_member_is_copied_only_within_the_upload_limit(app, monkeypatch):
    from backend.routes import import_data
    monkeypatch.setattr(import_data, "MAX_UNPACKED_BYTES_PER_UPLOAD", 8 * MB)
    converted = []
    monkeypatch.setattr(docx_import, "convert_docx",
                        lambda *a, **k: converted.append(1))
    client, _ = _client(app)
    upload = _zip({"big.docx": b"\0" * (32 * MB)})
    r = _notes(client, upload)
    assert r.status_code == 400
    assert "unpacks to more than Loore imports" in r.get_json()["error"]
    assert converted == []


def test_member_compressed_with_bzip2_is_not_read(app):
    client, _ = _client(app)
    upload = _zip({"note.md": b"hello"}, compression=zipfile.ZIP_BZIP2)
    r = _notes(client, upload)
    assert r.status_code == 400
    assert "compressed in a way Loore can't read" in r.get_json()["error"]


def test_zip_with_too_many_entries_is_refused_before_the_listing(
        app, monkeypatch):
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 50)
    client, _ = _client(app)
    upload = _zip({f"n{i}.md": b"x" for i in range(60)})
    built = []
    real = zipfile.ZipFile

    def spy(*a, **k):
        built.append(1)
        return real(*a, **k)

    monkeypatch.setattr(archive_limits.zipfile, "ZipFile", spy)
    r = _notes(client, upload)
    assert r.status_code == 400
    assert "more than 50 files and folders" in r.get_json()["error"]
    assert built == []


def _stated_count(data, count):
    """*data* (a zip) with its end record stating *count* entries, its
    central directory unchanged."""
    at = data.rindex(b"PK\x05\x06")
    stated = count.to_bytes(2, "little")
    return data[:at + 8] + stated + stated + data[at + 12:]


def _refused_before_the_listing(monkeypatch, data):
    """The reason open_zip refused *data* for, when zipfile never built
    its listing; None when it was not refused that way."""
    built = []
    real = zipfile.ZipFile

    def spy(*a, **k):
        built.append(1)
        return real(*a, **k)

    monkeypatch.setattr(archive_limits.zipfile, "ZipFile", spy)
    with pytest.raises(archive_limits.ArchiveTooLarge) as refused:
        archive_limits.open_zip(io.BytesIO(data))
    return refused.value.reason if built == [] else None


def test_a_zip_that_states_fewer_entries_than_it_holds_is_counted(
        monkeypatch):
    """A zip whose end record states 1 entry and whose directory holds 60:
    its records are counted, before zipfile reads them."""
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 50)
    data = _stated_count(_zip({f"n{i}.md": b"x" for i in range(60)}), 1)
    assert archive_limits.directory_record(io.BytesIO(data))[0] == 1
    assert _refused_before_the_listing(monkeypatch, data) == \
        archive_limits.TOO_MANY_ENTRIES


def test_a_directory_past_its_size_is_refused_whatever_its_count(
        monkeypatch):
    """40 entries (under the 50 allowed) with names so long that the list
    of files is past 50 × DIRECTORY_BYTES_PER_ENTRY bytes."""
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 50)
    data = _zip({f"{i:03d}" + "n" * 400 + ".md": b"x" for i in range(40)})
    assert _refused_before_the_listing(monkeypatch, data) == \
        archive_limits.DIRECTORY_TOO_LARGE


def test_notes_upload_with_a_misstated_count_is_refused(app, monkeypatch):
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 50)
    client, _ = _client(app)
    data = _stated_count(_zip({f"n{i}.md": b"x" for i in range(60)}), 1)
    r = _notes(client, data)
    assert r.status_code == 400
    assert "more than 50 files and folders" in r.get_json()["error"]
    data = _zip({f"{i:03d}" + "n" * 400 + ".md": b"x" for i in range(40)})
    r = _notes(client, data)
    assert r.status_code == 400
    assert "list of files in this zip is larger" in r.get_json()["error"]


# The extra fields Finder and Info-ZIP write per entry: a timestamp
# ("UT") and the owner's ids ("ux"), 24 bytes.
_UNIX_EXTRA = (struct.pack("<HHBl", 0x5455, 5, 3, 1700000000)
               + struct.pack("<HHBBIBI", 0x7875, 11, 1, 4, 501, 4, 20))


def _many(entries, extra=b""):
    """A zip of *entries* ((name, bytes) pairs) as real tools write it:
    deflated, with *extra* fields on every entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, (2024, 1, 1, 10, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.extra = extra
            z.writestr(info, data)
    return buf.getvalue()


def test_a_vault_with_many_attachments_is_analyzed(app):
    """2,000 notes and 30,000 attachments with long paths (32,000 entries,
    records of about 150 bytes)."""
    client, _ = _client(app)
    long_dir = ("Vault/attachments/2024/screenshots/some-long-folder-name/"
                "another-folder/Screenshot 2024-01-01 at 10.00.")
    entries = [(f"Vault/Notes/note-{i:05d}.md", b"hello") for i in range(2000)]
    entries += [(f"{long_dir}{i:05d}.png", b"") for i in range(30000)]
    upload = _many(entries)
    assert archive_limits.directory_record(io.BytesIO(upload))[1] > 4_600_000
    r = _notes(client, upload)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["total_files"] == 2000


def test_a_finder_zip_of_18500_notes_is_analyzed(app):
    """Finder (ditto) adds a __MACOSX twin per file: 37,000 entries with
    records of about 124 bytes, a list of files of 4.6 MB."""
    client, _ = _client(app)
    entries = []
    for i in range(18500):
        entries.append((f"Journal/Entries/2019/entry-{i:05d} morning pages.md",
                        b"x"))
        entries.append((f"__MACOSX/Journal/Entries/2019/._entry-{i:05d} "
                        "morning pages.md", b"\0" * 120))
    upload = _many(entries, extra=_UNIX_EXTRA)
    count, size = archive_limits.directory_record(io.BytesIO(upload))
    assert count == 37000 and size > 4_600_000
    r = _notes(client, upload)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["total_files"] == 18500


def test_an_x_archive_with_45000_media_files_is_analyzed(app):
    client, user = _client(app)
    entries = [("data/tweets.js", _tweets_js(50).encode())]
    entries += [(f"data/tweets_media/{1700000000000000000 + i}-"
                 f"AbCdEfGhIjKl.jpg", b"") for i in range(45000)]
    upload = _many(entries, extra=_UNIX_EXTRA)
    assert archive_limits.directory_record(io.BytesIO(upload))[1] > 4_600_000
    r = _twitter(client, upload)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["total_tweets"] == 50


def test_notes_zip_of_real_size_is_analyzed(app):
    """Control: 2,000 notes and a Word document, 6 MB of text."""
    client, _ = _client(app)
    entries = {f"vault/note {i}.md": ("# Note %d\n\n" % i + "word " * 600)
               for i in range(2000)}
    entries["vault/Thesis.docx"] = _docx(["Preface", "More text"])
    r = _notes(client, _zip(entries))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["total_files"] == 2001
    assert body["errors"] == []


def test_word_document_compressed_with_bzip2_is_refused():
    buf = io.BytesIO(_docx(["Text"]))
    with zipfile.ZipFile(buf) as z:
        parts = {i.filename: z.read(i) for i in z.infolist()}
    odd = io.BytesIO(_zip(parts, compression=zipfile.ZIP_BZIP2))
    with pytest.raises(docx_import.DocxImportError,
                       match="unsupported format"):
        docx_import.convert_docx(odd)


# ── X archive ────────────────────────────────────────────────────────────

def _tweet(n, i=1):
    return json.dumps({"tweet": {
        "id_str": str(i), "full_text": "a" * n,
        "created_at": "Wed Aug 26 08:17:00 +0000 2026"}})


def _tweets_js(n, size=100, tail="]"):
    return ("window.YTD.tweets.part0 = ["
            + ",".join(_tweet(size, i) for i in range(n)) + tail)


def _left(user):
    d = ta.STASH_ROOT / str(user.id)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_stash_over_its_byte_limit_is_refused_and_removed(app, monkeypatch):
    monkeypatch.setattr(ta, "MAX_STASH_BYTES", MB)
    client, user = _client(app)
    upload = _zip({"data/tweets.js": _tweets_js(300, size=10_000)})
    r = _twitter(client, upload)
    assert r.status_code == 400
    assert "more than Loore imports at once" in r.get_json()["error"]
    assert _left(user) == []


def test_archive_over_the_row_limit_is_refused_and_removed(app, monkeypatch):
    monkeypatch.setattr(ta, "MAX_TWEET_ROWS", 100)
    client, user = _client(app)
    r = _twitter(client, _zip({"data/tweets.js": _tweets_js(150)}))
    assert r.status_code == 400
    assert _left(user) == []


def test_tweets_js_is_read_only_up_to_its_limit(app, monkeypatch):
    monkeypatch.setattr(ta, "MAX_TWEETS_JS_BYTES", MB)
    client, user = _client(app)
    r = _twitter(client, _zip({"data/tweets.js": _tweets_js(2000, 1000)}))
    assert r.status_code == 400
    assert "more than Loore imports at once" in r.get_json()["error"]
    assert _left(user) == []


def test_malformed_tail_leaves_no_file(app):
    client, user = _client(app)
    upload = _zip({"data/tweets.js": _tweets_js(2000, tail=",{oops")})
    for _ in range(3):
        assert _twitter(client, upload).status_code == 400
    assert _left(user) == []


def test_sweep_removes_an_unsorted_file_left_by_a_stopped_analyze(
        app, monkeypatch, tmp_path):
    user_dir = ta.STASH_ROOT / "7"
    user_dir.mkdir(parents=True)
    left = user_dir / "tok.jsonl.unsorted"
    left.write_bytes(b"x")
    fresh = user_dir / "new.jsonl.unsorted"
    fresh.write_bytes(b"x")
    old = time.time() - ta.STASH_TTL_SECONDS - 10
    os.utime(left, (old, old))
    ta.sweep_expired()
    assert not left.exists()
    assert fresh.exists()


class _Counting(io.StringIO):
    def __init__(self, text):
        super().__init__(text)
        self.chars = 0

    def read(self, n=-1):
        out = super().read(n)
        self.chars += len(out)
        return out


def test_one_endless_element_is_read_only_up_to_the_element_limit():
    stream = _Counting('window.YTD.tweets.part0 = ["' + "a" * (24 * MB))
    with pytest.raises(ta.TwitterArchiveError):
        list(ta.iter_tweets_stream(stream))
    assert stream.chars <= ta.MAX_TWEET_ELEMENT_CHARS + 2 * ta.CHUNK_CHARS


def test_a_tweets_date_is_kept_to_a_fixed_length():
    """The analyze step keeps one sort key per row in memory: a date that
    does not parse is kept as at most MAX_CREATED_AT_CHARS characters,
    and one that is not text as none."""
    long_date = "z" * 100_000
    row = ta.compact_row({"full_text": "hi", "created_at": long_date})
    assert row["created_at"] == "z" * ta.MAX_CREATED_AT_CHARS
    assert ta._sort_key(long_date) == (1, "z" * ta.MAX_CREATED_AT_CHARS)
    big = {"nested": ["x" * 10_000] * 10}
    assert ta.compact_row({"full_text": "hi", "created_at": big})[
        "created_at"] == ""
    assert ta._sort_key(big) == (1, "")
    real = "Wed Aug 26 08:17:00 +0000 2026"
    assert ta.compact_row({"full_text": "hi", "created_at": real})[
        "created_at"] == real
    assert ta._sort_key(real)[0] == 0


def test_archive_with_long_or_odd_dates_is_analyzed(app):
    client, user = _client(app)
    tweets = [json.dumps({"tweet": {
        "id_str": str(i), "full_text": "hello",
        "created_at": ("q" * 50_000) if i % 2 else {"odd": [i]}}})
        for i in range(40)]
    upload = _zip({"data/tweets.js": "window.YTD.tweets.part0 = ["
                   + ",".join(tweets) + "]"})
    r = _twitter(client, upload)
    assert r.status_code == 200, r.get_json()
    rows = list(ta.stash_iter(ta.stash_path(user.id,
                                            r.get_json()["import_token"])))
    assert len(rows) == 40
    assert all(len(row["created_at"]) <= ta.MAX_CREATED_AT_CHARS
               for row in rows)


def test_tweets_js_compressed_with_bzip2_is_refused(app):
    client, user = _client(app)
    upload = _zip({"data/tweets.js": _tweets_js(3)},
                  compression=zipfile.ZIP_BZIP2)
    r = _twitter(client, upload)
    assert r.status_code == 400
    assert "compressed in a way Loore can't read" in r.get_json()["error"]
    assert _left(user) == []


def test_archive_of_real_size_is_analyzed(app):
    """Control: 20,000 tweets of real length, plus media files."""
    client, user = _client(app)
    entries = {"data/tweets.js": _tweets_js(20_000, size=200)}
    entries.update({f"data/tweets_media/{i}.jpg": b"\xff" * 2000
                    for i in range(500)})
    r = _twitter(client, _zip(entries))
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["total_tweets"] == 20_000
    assert [n for n in _left(user) if n.endswith(".unsorted")] == []


def test_limits_are_above_real_archives():
    """A heavy account's export: 60,000+ tweets, about 1.7 KB of tweets.js
    each (the owner's archive), and its compact rows."""
    assert ta.MAX_TWEETS_JS_BYTES >= 3 * 60_000 * 1700
    assert ta.MAX_TWEET_ROWS >= 10 * 60_000
    assert ta.MAX_STASH_BYTES >= ta.MAX_TWEET_ROWS * 350
