"""Pre-fill bundles (#698): the lab's prepared material for one Community
Archive member, kept in private storage and loaded on production.

Layout under the storage location (backend/utils/bundle_storage.py),
keyed by the member's X id, the identity an X sign-in proves:

    bundles/<x_id>/bundle.jsonl.gz   dump_user.py's file (format 2), gzipped
    bundles/<x_id>/manifest.json     what the bundle is (below); no content
    handles/<handle>.json            {"x_id"}: finds an X id for the admin
    excluded/<x_id>.json             {"x_id", "reason", "excluded_at"}

The bundle is a series of gzip members: the header line alone, then
MEMBER_LINES entries per member. ``gzip`` reads it as one file; a load
reads one member at a time by its byte range (``read_member``), checks
that member's sha256, and never downloads or decompresses the rest. So
a load resumes at any member without reading what came before, and holds
one member in memory.

The manifest is written after the bundle, so a bundle without a manifest
(an export that stopped between the two) is never read; the next export
writes both again. It carries the formats, the X id, the handle at
export time, counts, the date range of the entries, when it was
exported, the bundle object's sha256 and size with each member's offset,
length, line count and sha256, and a fingerprint of the lab account's
metadata (an export skips an account whose fingerprint has not changed).

An X id on the exclusion list is never exported again and never loaded:
a person who asked for deletion, declined, or whose account went
protected. Deleting a bundle (``delete_bundle``) puts its X id there.

The handle index is only a way for the admin to type a handle: what a
load writes into is always the account with the bundle's X id.
"""
import gzip
import hashlib
import json
import re
import zlib
from datetime import datetime, timezone

from backend.utils.bundle_storage import storage_for

BUNDLE_FORMAT = 1
# dump_user.FORMAT_VERSION values a load understands.
DUMP_FORMATS = (2,)
# HEURISTIC: entries per gzip member, which is also a load's chunk (one
# commit, one data key): what load_user.py and the imports use. About
# half a megabyte without embeddings, a few megabytes with them.
MEMBER_LINES = 500
_GZIP_LEVEL = 6   # about 4x on tweet JSON, several times faster than 9

BUNDLE_OBJECT = "bundle.jsonl.gz"
MANIFEST_OBJECT = "manifest.json"

EXCLUSION_REASONS = ("deletion_request", "declined", "protected",
                     "owner_request", "other")

_X_ID_RE = re.compile(r"[0-9]{1,20}")
_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
_READ_CHUNK = 1 << 20


class BundleError(Exception):
    """A bundle that must not be loaded; ``code`` names why."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def valid_x_id(x_id):
    return bool(x_id) and bool(_X_ID_RE.fullmatch(str(x_id)))


def clean_handle(handle):
    handle = (handle or "").strip().lstrip("@")
    return handle if _HANDLE_RE.fullmatch(handle) else None


def bundle_name(x_id):
    return f"bundles/{x_id}/{BUNDLE_OBJECT}"


def manifest_name(x_id):
    return f"bundles/{x_id}/{MANIFEST_OBJECT}"


def handle_index_name(handle):
    return f"handles/{handle.lower()}.json"


def exclusion_name(x_id):
    return f"excluded/{x_id}.json"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    """A manifest time (ISO, maybe with Z) as a naive UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ── Configuration ─────────────────────────────────────────────────────────

def configured_storage(config=None):
    """The storage PREFILL_BUNDLE_LOCATION names, or None when unset."""
    if config is None:
        from flask import current_app
        config = current_app.config
    return storage_for(config.get("PREFILL_BUNDLE_LOCATION") or "")


def bundles_enabled(config=None):
    """Loading bundles on this server: the switch is on AND a location is
    set. Off, nothing reads the storage and no load starts or runs."""
    if config is None:
        from flask import current_app
        config = current_app.config
    return bool(config.get("PREFILL_BUNDLES_ENABLED")
                and (config.get("PREFILL_BUNDLE_LOCATION") or "").strip())


# ── Manifests, exclusions, the handle index ───────────────────────────────

def _read_json(storage, name):
    data = storage.read_bytes(name)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(storage, name, value):
    storage.write_bytes(name, json.dumps(value, sort_keys=True,
                                         indent=1).encode("utf-8"))


def read_manifest(storage, x_id):
    """The manifest for ``x_id``, or None when there is no bundle."""
    if not valid_x_id(x_id):
        return None
    return _read_json(storage, manifest_name(x_id))


def manifest_problem(manifest, x_id):
    """Why a manifest must not be loaded for ``x_id`` (a BundleError), or
    None. Checked before anything is downloaded."""
    if manifest is None:
        return BundleError("no_bundle", "No prepared material for this X id.")
    if str(manifest.get("x_id")) != str(x_id):
        return BundleError("x_id_mismatch",
                           "The bundle's manifest names another X id.")
    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        return BundleError(
            "format", f"Bundle format {manifest.get('bundle_format')!r} is "
            f"not {BUNDLE_FORMAT}; update the code on one side.")
    if manifest.get("dump_format") not in DUMP_FORMATS:
        return BundleError(
            "format", f"Dump format {manifest.get('dump_format')!r} is not "
            "one this server loads; update the code on one side.")
    obj = manifest.get("object") or {}
    if not obj.get("sha256") or not isinstance(obj.get("lines"), int):
        return BundleError("manifest", "The manifest has no checksum.")
    if member_problem(obj):
        return BundleError("manifest", "The manifest's member index is not "
                                       "consistent.")
    return None


def member_problem(obj):
    """Whether a manifest object's member index is unusable: members must
    be contiguous byte ranges, the first one the header line alone, with a
    sha256 each, adding up to the object's lines and bytes."""
    members = obj.get("members")
    if not isinstance(members, list) or not members:
        return True
    at, lines = 0, 0
    for m in members:
        if not isinstance(m, dict) or m.get("offset") != at \
                or not isinstance(m.get("length"), int) or m["length"] <= 0 \
                or not isinstance(m.get("lines"), int) or m["lines"] <= 0 \
                or not m.get("sha256"):
            return True
        at += m["length"]
        lines += m["lines"]
    return (members[0]["lines"] != 1 or lines != obj.get("lines")
            or (obj.get("bytes") is not None and at != obj.get("bytes")))


def write_manifest(storage, manifest):
    _write_json(storage, manifest_name(manifest["x_id"]), manifest)


def exclusion(storage, x_id):
    if not valid_x_id(x_id):
        return None
    return _read_json(storage, exclusion_name(x_id))


def excluded_x_ids(storage):
    """Every X id on the exclusion list (one listing)."""
    out = set()
    for name in storage.list("excluded"):
        stem = name.rsplit("/", 1)[-1]
        if stem.endswith(".json") and valid_x_id(stem[:-5]):
            out.add(stem[:-5])
    return out


def write_exclusion(storage, x_id, reason):
    if not valid_x_id(x_id):
        raise ValueError("an X id is digits only")
    if reason not in EXCLUSION_REASONS:
        raise ValueError(f"reason must be one of {', '.join(EXCLUSION_REASONS)}")
    _write_json(storage, exclusion_name(x_id), {
        "x_id": str(x_id), "reason": reason, "excluded_at": now_iso()})


def remove_exclusion(storage, x_id):
    return storage.delete(exclusion_name(x_id))


def write_handle_index(storage, handle, x_id):
    _write_json(storage, handle_index_name(handle),
                {"x_id": str(x_id), "handle": handle})


def lookup_handle(storage, handle):
    """The X id the last export saw under ``handle``, or None."""
    handle = clean_handle(handle)
    if not handle:
        return None
    entry = _read_json(storage, handle_index_name(handle))
    x_id = str((entry or {}).get("x_id") or "")
    return x_id if valid_x_id(x_id) else None


def delete_bundle(storage, x_id, reason):
    """Remove the bundle of ``x_id`` and put the X id on the exclusion
    list, so no export writes it again and no load reads it. Returns
    the names deleted. Works whether or not a bundle exists."""
    if not valid_x_id(x_id):
        raise ValueError("an X id is digits only")
    manifest = read_manifest(storage, x_id)
    # The exclusion first, then the objects: an export checks the list
    # just before it writes an account and again after, and removes what
    # it wrote when the X id was excluded meanwhile, so either it sees
    # the exclusion or this deletes what it wrote.
    write_exclusion(storage, x_id, reason)
    return remove_objects(storage, x_id, (manifest or {}).get("handle"))


def remove_objects(storage, x_id, handle=None):
    """Delete the bundle, the manifest and the handle index entry of
    ``x_id`` (the entry only while it points at this X id). The
    exclusion list is not touched. Returns the names deleted."""
    deleted = []
    for name in (manifest_name(x_id), bundle_name(x_id)):
        if storage.delete(name):
            deleted.append(name)
    handle = clean_handle(handle)
    if handle and lookup_handle(storage, handle) == str(x_id):
        if storage.delete(handle_index_name(handle)):
            deleted.append(handle_index_name(handle))
    return deleted


# ── The bundle file ───────────────────────────────────────────────────────

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_READ_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def write_members(lines, dest_path, member_lines=None):
    """Write ``lines`` (JSON strings: the header, then the entries) to
    ``dest_path`` as gzip members: the header alone, then ``member_lines``
    (MEMBER_LINES) entries each. Returns the manifest's ``object`` part
    (sha256, bytes, lines, members). One member in memory at a time."""
    member_lines = member_lines or MEMBER_LINES
    whole = hashlib.sha256()
    members = []
    state = {"at": 0, "lines": 0}

    with open(dest_path, "wb") as out:
        def flush(batch):
            comp = zlib.compressobj(_GZIP_LEVEL, zlib.DEFLATED, 31)
            data = comp.compress("".join(batch).encode("utf-8")) + comp.flush()
            out.write(data)
            whole.update(data)
            members.append({"offset": state["at"], "length": len(data),
                            "lines": len(batch),
                            "sha256": hashlib.sha256(data).hexdigest()})
            state["at"] += len(data)
            state["lines"] += len(batch)

        batch, header_done = [], False
        for line in lines:
            if not line.strip():
                continue
            batch.append(line if line.endswith("\n") else line + "\n")
            if not header_done or len(batch) >= member_lines:
                flush(batch)
                batch, header_done = [], True
        if batch:
            flush(batch)
    return {"name": BUNDLE_OBJECT, "sha256": whole.hexdigest(),
            "bytes": state["at"], "lines": state["lines"], "members": members}


def member_start(members, index):
    """How many lines come before member ``index`` (the header counts)."""
    return sum(m["lines"] for m in members[:index])


def member_at(members, entries_done):
    """The member that starts after ``entries_done`` entries (the header
    not counted), or None when every entry has been read."""
    seen = 0
    for i, m in enumerate(members[1:], 1):
        if seen == entries_done:
            return i
        seen += m["lines"]
    return None


def verify_bundle(storage, x_id, members, sha256):
    """Check every part, and the whole object, against the manifest before
    a load writes anything: one streaming read, one part in memory at a
    time, nothing decompressed. BundleError("checksum") on a mismatch."""
    whole = hashlib.sha256()
    for member in members:
        data = storage.read_range(bundle_name(x_id), member["offset"],
                                  member["length"])
        if data is None:
            raise BundleError("no_bundle", "The bundle is gone from storage.")
        if len(data) != member["length"] or \
                hashlib.sha256(data).hexdigest() != member["sha256"]:
            raise BundleError("checksum", "A part of the bundle does not "
                                          "match its manifest.")
        whole.update(data)
    if whole.hexdigest() != sha256:
        raise BundleError("checksum", "The bundle does not match its manifest.")


def split_lines(text):
    """The lines of a decompressed member, split on "\\n" only, as
    write_members ends them; a "\\r" before the "\\n" is dropped and blank
    lines are skipped, as write_members skips them. Not str.splitlines:
    that also splits at characters an entry holds unescaped (json.dumps
    leaves U+2028, U+2029 and U+0085 as they are), which cuts the entry
    in two."""
    out = []
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.strip():
            out.append(line)
    return out


def read_member(storage, x_id, member):
    """The parsed lines of one member, read by its byte range and checked
    against its sha256 before anything is parsed. As many as the manifest
    says the member holds: a load counts its progress by them."""
    data = storage.read_range(bundle_name(x_id), member["offset"],
                              member["length"])
    if data is None:
        raise BundleError("no_bundle", "The bundle is gone from storage.")
    if len(data) != member["length"] or \
            hashlib.sha256(data).hexdigest() != member["sha256"]:
        raise BundleError("checksum", "A part of the bundle does not match "
                                      "its manifest.")
    try:
        text = zlib.decompress(data, 31).decode("utf-8")
        entries = [json.loads(line) for line in split_lines(text)]
    except (zlib.error, UnicodeDecodeError, ValueError):
        raise BundleError("format", "A part of the bundle cannot be read.")
    if len(entries) != member["lines"]:
        raise BundleError("format", "A part of the bundle does not hold the "
                                    "entries its manifest counts.")
    return entries


def open_bundle(path, skip=0):
    """(header, entries): the dump's header line and a generator over its
    entries, read one line at a time from the gzipped file, so no more
    than one entry is in memory. The first ``skip`` entries are passed
    over unparsed (a resumed run). The file is closed when the generator
    is exhausted or closed."""
    f = gzip.open(path, "rt", encoding="utf-8")
    try:
        first = f.readline()
        header = json.loads(first) if first.strip() else None
    except ValueError:
        f.close()
        raise BundleError("format", "The bundle's first line is not a header.")
    if not isinstance(header, dict) or header.get("format") not in DUMP_FORMATS:
        f.close()
        raise BundleError("format", "The bundle is not a dump this server loads.")

    def entries():
        try:
            passed = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if passed < skip:
                    passed += 1
                    continue
                yield json.loads(line)
        finally:
            f.close()
    return header, entries()
