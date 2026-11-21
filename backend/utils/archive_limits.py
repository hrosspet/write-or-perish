"""Reading the members of an uploaded zip within limits.

Used for every zip an import unpacks: the notes archive (.md and .docx
members), the X archive's tweets.js, and a Word document itself.

- Before ``zipfile.ZipFile`` builds its listing (one object per entry,
  in memory), the central directory's size is checked and its records
  are counted, as zipfile will read them but without building them:
  ``open_zip``.
- A member is read only when it is stored or deflated, which is what
  every exporter writes. zipfile unpacks a bzip2 or LZMA member a whole
  read at a time, with no bound on what one read unpacks to.
- A member is read in pieces, and reading stops once it passes its limit,
  whatever its header says: ``read_member``, ``copy_member`` and
  ``BoundedReader`` (for a member that is parsed as it streams).

Each raises ``ArchiveTooLarge`` past a limit and ``UnsupportedMember`` for
other compression; the caller turns them into its own message.
"""
import io
import struct
import zipfile

# ── Heuristics (named here, listed in the PR) ─────────────────────────────
# Entries in one uploaded zip. A real X archive of a heavy account holds a
# few thousand to some tens of thousands of files (tweets.js, media, the
# other data files; the owner's archive has 6,500), and a notes vault a
# few thousand (twice that from Finder, which adds a __MACOSX twin per
# file). Each entry is an object of a few hundred bytes while the zip is
# open, so 100,000 is about 50 MB at most.
MAX_ARCHIVE_ENTRIES = 100_000
# Bytes of the central directory allowed per entry. A record is 46 bytes
# plus its name and extra fields: 88 to 153 bytes in real archives (Finder
# and X exports, vaults with long attachment paths), so 300 leaves room
# for names twice as long. 100,000 entries: a directory of up to 30 MB,
# read once before the records are counted.
DIRECTORY_BYTES_PER_ENTRY = 300

READ_PIECE_BYTES = 1024 * 1024
SUPPORTED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

# What ArchiveTooLarge.reason says was too large.
TOO_MANY_ENTRIES = "entries"
DIRECTORY_TOO_LARGE = "directory"
MEMBER_TOO_LARGE = "member"


class ArchiveTooLarge(ValueError):
    """A zip, or one of its members, is past a limit. ``reason``: which
    one (TOO_MANY_ENTRIES, DIRECTORY_TOO_LARGE, MEMBER_TOO_LARGE)."""

    def __init__(self, reason=MEMBER_TOO_LARGE):
        super().__init__(reason)
        self.reason = reason


class UnsupportedMember(ValueError):
    """A member compressed in a way the import does not read."""


def _end_record(fileobj):
    """The zip's end record as zipfile reads it (the zip64 one when there
    is one), or None when it cannot be read here: zipfile then refuses the
    file as it would."""
    try:
        pos = fileobj.tell()
        try:
            end = zipfile._EndRecData(fileobj)
        finally:
            fileobj.seek(pos)
    except (AttributeError, OSError, ValueError, struct.error):
        return None
    return end or None


def directory_record(fileobj):
    """(stated entry count, central directory size in bytes), or None."""
    end = _end_record(fileobj)
    if end is None:
        return None
    return end[zipfile._ECD_ENTRIES_TOTAL], end[zipfile._ECD_SIZE]


def _count_records(fileobj, end, most):
    """The records in the central directory, counted as zipfile reads them
    (records follow each other until the directory's stated size is used
    up), without building them; counting stops past *most*. None when the
    directory cannot be read here (zipfile then refuses the file)."""
    size, offset = end[zipfile._ECD_SIZE], end[zipfile._ECD_OFFSET]
    concat = end[zipfile._ECD_LOCATION] - size - offset
    if end[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
        concat -= zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
    try:
        pos = fileobj.tell()
        try:
            fileobj.seek(offset + concat)
            data = fileobj.read(size)
        finally:
            fileobj.seek(pos)
    except (OSError, ValueError):
        return None
    count = at = 0
    while at + zipfile.sizeCentralDir <= len(data):
        if data[at:at + 4] != zipfile.stringCentralDir:
            break  # zipfile refuses the file there
        names = struct.unpack_from("<HHH", data, at + 28)
        at += zipfile.sizeCentralDir + sum(names)
        count += 1
        if count > most:
            break
    return count


def check_directory(fileobj, max_entries):
    """ArchiveTooLarge unless the zip's central directory is at most
    *max_entries* × DIRECTORY_BYTES_PER_ENTRY bytes and holds at most
    *max_entries* records. zipfile reads every record that fits in the
    directory's size, whatever count the end record states, so the records
    are counted here."""
    end = _end_record(fileobj)
    if end is None:
        return
    if end[zipfile._ECD_ENTRIES_TOTAL] > max_entries:
        raise ArchiveTooLarge(TOO_MANY_ENTRIES)
    if end[zipfile._ECD_SIZE] > max_entries * DIRECTORY_BYTES_PER_ENTRY:
        raise ArchiveTooLarge(DIRECTORY_TOO_LARGE)
    records = _count_records(fileobj, end, max_entries)
    if records is not None and records > max_entries:
        raise ArchiveTooLarge(TOO_MANY_ENTRIES)


def open_zip(fileobj, max_entries=None):
    """``zipfile.ZipFile(fileobj)`` for a zip of at most *max_entries*
    entries (MAX_ARCHIVE_ENTRIES when not given), checked on its central
    directory before zipfile reads it; ArchiveTooLarge for more.
    zipfile.BadZipFile as before."""
    if max_entries is None:
        max_entries = MAX_ARCHIVE_ENTRIES
    check_directory(fileobj, max_entries)
    zf = zipfile.ZipFile(fileobj, "r")
    if len(zf.infolist()) > max_entries:
        zf.close()
        raise ArchiveTooLarge(TOO_MANY_ENTRIES)
    return zf


def check_member(info):
    """UnsupportedMember unless *info* is stored or deflated."""
    if info.compress_type not in SUPPORTED_COMPRESSION:
        raise UnsupportedMember(info.compress_type)


def _pieces(zf, info, limit):
    check_member(info)
    if info.file_size > limit:
        raise ArchiveTooLarge(MEMBER_TOO_LARGE)
    total = 0
    with zf.open(info) as src:
        while True:
            piece = src.read(READ_PIECE_BYTES)
            if not piece:
                return
            total += len(piece)
            if total > limit:
                raise ArchiveTooLarge(MEMBER_TOO_LARGE)
            yield piece


def read_member(zf, info, limit):
    """The bytes of member *info*, at most *limit* of them."""
    return b"".join(_pieces(zf, info, limit))


def copy_member(zf, info, dst, limit):
    """Copy member *info* into the file *dst*, at most *limit* bytes.
    Returns the number of bytes copied."""
    total = 0
    for piece in _pieces(zf, info, limit):
        dst.write(piece)
        total += len(piece)
    return total


class BoundedReader(io.RawIOBase):
    """A readable stream over *raw* (a zip member open for reading) that
    raises ArchiveTooLarge once more than *limit* bytes were read."""

    def __init__(self, raw, limit):
        self._raw = raw
        self._limit = limit
        self._read = 0

    def readable(self):
        return True

    def readinto(self, buf):
        # One byte past the limit is enough to know the member is larger.
        want = min(len(buf), self._limit + 1 - self._read)
        if want <= 0:
            raise ArchiveTooLarge(MEMBER_TOO_LARGE)
        data = self._raw.read(want)
        n = len(data)
        self._read += n
        if self._read > self._limit:
            raise ArchiveTooLarge(MEMBER_TOO_LARGE)
        buf[:n] = data
        return n

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()
