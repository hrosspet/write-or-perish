"""The nightly sweep for audio files that nothing needs any more.

Run by the daily cleanup (backend/tasks/node_cleanup.py) after it has
finalized that day's nodes, and by hand through
backend/scripts/purge_orphaned_node_audio.py (a dry run by default, with
counts per category). Under the audio storage root it deletes:

* a node's folders, ``user/<uid>/node/<id>/`` and ``nodes/<uid>/<id>/``,
  when the node row is gone or the node was deleted more than
  SOFT_DELETE_GRACE_DAYS ago: what earlier code left behind, and what the
  cleanup could not delete when it finalized the node;
* the speech of a saved reference or a profile version,
  ``user/<uid>/item/<id>/`` or ``user/<uid>/profile/<id>/``, when its row
  is gone;
* superseded speech in the folder of a live node, reference or profile:
  ``tts.mp3`` and ``tts_chunk_<i>.mp3`` (plain or ``.enc``) that no row
  points at any more, after an edit cleared the speech or a regeneration
  made fewer chunks (#66).

A file a row points at is never deleted. Profile versions share speech
files (routes/profile.py ``_copy_audio``) and merged reference copies keep
the copy's (scripts/backfill_read_picks.py), so a reference's or a
profile's file counts as used while any row of the same user and kind
points at it. Node audio is never shared between nodes, so a node's own
row and speech chunks decide. Only speech is ever superseded: a node's
recording stays as long as the node.

Never touched:

* a live row's folder beyond its superseded speech;
* a node deleted within the grace period (it can still be restored);
* a row a waiting "Delete all my writing" hid: the request's purge
  deletes it, and a restore needs it (the cleanup skips these too);
* a folder with anything changed in the last 24 hours (RECENT_SECONDS).
  A recording is moved into its node's folder, and an upload's folder is
  made, just before the node's row is committed; and an edit or a
  regeneration that supersedes speech marks the folders of the old
  speech changed (audio_storage.mark_speech_superseded), so a playback
  that started before is not cut;
* anything outside those folders, and anything behind a symlink (never
  followed; paths are built with storage_path, and deleted only when
  they resolve inside the root).

Reads folder names, file sizes and times, row ids, deletion times and
stored audio URLs; never a file's content. Lists one folder at a time and
looks rows up BATCH_SIZE folders per round of queries, ending each read
transaction, so it is cheap enough to run daily on a small VM. With
``measure_kept=False`` (the daily run) it does not measure the folders it
keeps.
"""
import os
import pathlib
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# ── Heuristics (see the PR for why each value) ──────────────────────────
# A folder with a file (or itself) changed this recently is left for a
# later run: the voice path moves a recording into nodes/<uid>/<id>/
# after the node's row is flushed and before it is committed, an upload
# makes user/<uid>/node/<id>/ the same way, and superseding speech marks
# its folder changed. A day is far longer than either window, and than a
# playback.
RECENT_SECONDS = 24 * 3600
# Folders per round of database lookups.
BATCH_SIZE = 500

_ID_RE = re.compile(r"[0-9]{1,18}")
# Generated speech (tasks/tts.py, utils/tts_stream.py), plain or encrypted.
SPEECH_FILE_RE = re.compile(r"tts(?:_chunk_[0-9]{1,6})?\.mp3(?:\.enc)?")

ROW_GONE = "row gone"
PAST_GRACE = "node deleted > grace period"
SUPERSEDED = "superseded speech of a live row"
TO_DELETE = (ROW_GONE, PAST_GRACE, SUPERSEDED)
LIVE = "live (kept)"
IN_GRACE = "node deleted, within grace period (kept)"
HIDDEN = "hidden by a waiting 'Delete all my writing' (kept)"
SHARED = "row gone, files used by another row (kept)"
RECENT = "changed in the last 24 h (kept for a later run)"
SYMLINK = "symlink (left alone)"

_USER_KINDS = ("node", "item", "profile")


class Folder(NamedTuple):
    kind: str          # the row it belongs to: "node", "item" or "profile"
    label: str         # its layout, ids left out
    owner: int         # the user id in its path
    row_id: int
    path: pathlib.Path
    is_link: bool


def _id_dirs(folder):
    """(id, entry) of the entries of *folder* named by a plain number,
    in id order. Symlinks are yielded too (never followed), so the
    caller can count them; anything else that is not a folder is not."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    out = []
    for e in entries:
        if not _ID_RE.fullmatch(e.name):
            continue
        if e.is_symlink() or e.is_dir(follow_symlinks=False):
            out.append((int(e.name), e))
    return sorted(out, key=lambda pair: pair[0])


def iter_folders(root):
    """Every row folder under *root*: ``user/<uid>/node|item|profile/<id>``
    and ``nodes/<uid>/<id>``. A user folder (or its ``node``, ``item`` or
    ``profile`` folder) that is a symlink is passed over, never followed.
    Paths are built with storage_path, so they stay inside the root."""
    from backend.utils.audio_storage import storage_path
    root = pathlib.Path(root)
    for top_name in ("user", "nodes"):
        top = root / top_name
        if top.is_symlink() or not top.is_dir():
            continue
        for owner_id, owner in _id_dirs(top):
            if owner.is_symlink():
                continue
            for kind in (_USER_KINDS if top_name == "user" else (None,)):
                prefix = (("user", owner_id, kind) if kind
                          else ("nodes", owner_id))
                base = storage_path(root, *prefix)
                if base.is_symlink() or not base.is_dir():
                    continue
                label = (f"user/<uid>/{kind}/<id>" if kind
                         else "nodes/<uid>/<id>")
                for row_id, entry in _id_dirs(base):
                    yield Folder(kind or "node", label, owner_id, row_id,
                                 storage_path(root, *prefix, row_id),
                                 entry.is_symlink())


def folder_stats(path):
    """(files, bytes, newest change time) of a folder: lstat only,
    symlinks counted as entries and never followed. A file's ctime
    counts as a change, since a move keeps its mtime."""
    files = size = 0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        try:
            newest = max(newest, os.lstat(dirpath).st_mtime)
        except OSError:
            pass
        links = [n for n in dirnames
                 if os.path.islink(os.path.join(dirpath, n))]
        for name in filenames + links:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime, st.st_ctime)
    return files, size, newest


def _key(rel):
    """A stored path as a URL names it: an encrypted file's ``.enc``
    left out."""
    return rel[:-len(".enc")] if rel.endswith(".enc") else rel


def _add_refs(refs, *urls):
    from backend.utils.audio_storage import media_rel_path
    for url in urls:
        rel = media_rel_path(url)
        if rel is not None:
            refs.add(_key(rel))


class Rows(NamedTuple):
    rows: dict     # kind -> {row id: (deleted_at, user_id)}
    hidden: dict   # kind -> {row id} a waiting "Delete all my writing" hid
    refs: dict     # ("node", node id) / (kind, user id) -> {path keys}


def _hidden(kind_name, ids):
    from backend.extensions import db
    from backend.models import UserDataPurgeHidden as H
    if not ids:
        return set()
    return {r for (r,) in db.session.query(H.row_id).filter(
        H.kind == kind_name, H.row_id.in_(sorted(ids)))}


def _lookup_nodes(ids, rows, refs):
    from backend.extensions import db
    from backend.models import Node, TTSChunk
    for r in db.session.query(
            Node.id, Node.deleted_at, Node.user_id, Node.audio_tts_url,
            Node.audio_original_url).filter(Node.id.in_(sorted(ids))):
        rows[r.id] = (r.deleted_at, r.user_id)
        _add_refs(refs[("node", r.id)], r.audio_tts_url, r.audio_original_url)
    live = sorted(i for i, (deleted_at, _) in rows.items()
                  if deleted_at is None)
    if live:
        for nid, url in db.session.query(
                TTSChunk.node_id, TTSChunk.audio_url).filter(
                TTSChunk.node_id.in_(live), TTSChunk.audio_url.isnot(None)):
            _add_refs(refs[("node", nid)], url)


def _lookup_owned(kind, model, fk, ids, owners, rows, refs):
    """Rows of a reference or profile folder, and every speech URL of
    their users' rows of that kind (files are shared within a user)."""
    from backend.extensions import db
    from backend.models import TTSChunk
    for rid, uid in db.session.query(model.id, model.user_id).filter(
            model.id.in_(sorted(ids))):
        rows[rid] = (None, uid)
    uids = sorted(set(owners) | {uid for _, uid in rows.values()})
    for uid, url in db.session.query(model.user_id, model.audio_tts_url).filter(
            model.user_id.in_(uids), model.audio_tts_url.isnot(None)):
        _add_refs(refs[(kind, uid)], url)
    for uid, url in db.session.query(model.user_id, TTSChunk.audio_url).join(
            model, fk == model.id).filter(
            model.user_id.in_(uids), TTSChunk.audio_url.isnot(None)):
        _add_refs(refs[(kind, uid)], url)


def lookup(folders):
    """What the rows say about *folders*: ids, deletion times, stored
    audio URLs. Hidden rows are read too (and kept by classify)."""
    from backend.extensions import db
    from backend.models import ExternalItem, TTSChunk, UserProfile
    from backend.utils.hidden_rows import including_hidden_rows
    ids = defaultdict(set)
    owners = defaultdict(set)
    for f in folders:
        if not f.is_link:
            ids[f.kind].add(f.row_id)
            owners[f.kind].add(f.owner)
    rows = {kind: {} for kind in _USER_KINDS}
    refs = defaultdict(set)
    tables = {"node": "node", "item": ExternalItem.__tablename__,
              "profile": UserProfile.__tablename__}
    with including_hidden_rows():
        if ids["node"]:
            _lookup_nodes(ids["node"], rows["node"], refs)
        for kind, model, fk in (("item", ExternalItem, TTSChunk.item_id),
                                ("profile", UserProfile, TTSChunk.profile_id)):
            if ids[kind]:
                _lookup_owned(kind, model, fk, ids[kind], owners[kind],
                              rows[kind], refs)
        hidden = {kind: _hidden(tables[kind], ids[kind])
                  for kind in _USER_KINDS}
    # Read-only: end the transaction so none stays open during the walk.
    db.session.rollback()
    return Rows(rows, hidden, refs)


def classify(f, rows, cutoff):
    """The category of a folder, before the files are looked at."""
    if f.is_link:
        return SYMLINK
    row = rows.rows[f.kind].get(f.row_id)
    if row is None:
        return ROW_GONE
    if f.row_id in rows.hidden[f.kind]:
        return HIDDEN
    deleted_at = row[0]
    if deleted_at is None:
        return LIVE
    if deleted_at > cutoff:
        return IN_GRACE
    return PAST_GRACE


def _refs_for(f, rows):
    if f.kind == "node":
        return rows.refs.get(("node", f.row_id), set())
    out = set(rows.refs.get((f.kind, f.owner), set()))
    row = rows.rows[f.kind].get(f.row_id)
    if row is not None and row[1] != f.owner:
        out |= rows.refs.get((f.kind, row[1]), set())
    return out


def _children(path, speech_only):
    """The files directly in a folder (never a symlink or a folder)."""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return []
    return sorted(pathlib.Path(e.path) for e in entries
                  if e.is_file(follow_symlinks=False)
                  and (not speech_only or SPEECH_FILE_RE.fullmatch(e.name)))


def victims(f, cat, rows, root):
    """(category, files): what of folder *f* goes. Files None means the
    whole folder (a node's folders past the grace period or of a gone
    row, a gone reference's or profile's when no other row uses a file
    in it); else the files to delete, which may be none."""
    if cat == PAST_GRACE or (cat == ROW_GONE and f.kind == "node"):
        return cat, None
    if cat not in (ROW_GONE, LIVE):
        return cat, []
    if cat == LIVE and f.label == "nodes/<uid>/<id>":
        return cat, []                    # a recording: never superseded
    refs = _refs_for(f, rows)
    files = _children(f.path, speech_only=(cat == LIVE))
    unused = [p for p in files
              if _key(p.relative_to(root).as_posix()) not in refs]
    if cat == ROW_GONE:
        if len(unused) == len(files):
            return cat, None
        return (cat, unused) if unused else (SHARED, [])
    return (SUPERSEDED, unused) if unused else (LIVE, [])


def _size(paths):
    total = 0
    for p in paths:
        try:
            total += os.lstat(p).st_size
        except OSError:
            pass
    return total


class _Totals:
    def __init__(self):
        self.stats = defaultdict(lambda: [0, 0, 0])  # category -> folders, files, bytes
        self.planned = [0, 0, 0]
        self.done = [0, 0, 0]
        self.failed = 0
        self.errors = set()
        self.failed_ids = []

    def count(self, key, files, size):
        row = self.stats[key]
        row[0] += 1
        row[1] += files
        row[2] += size


def _assess(f, rows, cutoff, now, root, measure_kept):
    """(category, files to delete or None for the whole folder, files,
    bytes) of one folder, after the recent-change check."""
    cat, gone = victims(f, classify(f, rows, cutoff), rows, root)
    if cat not in TO_DELETE:
        if measure_kept and not f.is_link:
            files, size, _ = folder_stats(f.path)
            return cat, [], files, size
        return cat, [], 0, 0
    files, size, newest = folder_stats(f.path)
    if now - newest < RECENT_SECONDS:
        return f"{cat}, {RECENT}", [], files, size
    if gone is not None:
        files, size = len(gone), _size(gone)
    return cat, gone, files, size


def _delete(f, gone, root, totals):
    from backend.utils import audio_storage
    result = (audio_storage.delete_folders([f.path], root) if gone is None
              else audio_storage.delete_files(gone, root))
    totals.done[0] += 1
    totals.done[1] += result.files
    totals.done[2] += result.bytes
    if result.failed:
        totals.failed += result.failed
        totals.errors.update(result.errors)
        totals.failed_ids.append(f"{f.kind} {f.row_id}")


def sweep(apply=False, verbose=False, root=None, now=None, out=None,
          measure_kept=True):
    """Classify every row folder under *root* (default the audio storage
    root) and, with *apply*, delete what the TO_DELETE categories name.
    *now* is a POSIX time (default the current time); *out* gets a line
    per folder with *verbose* (kind and row id, never a path).

    Returns a dict: ``categories`` ({"<layout>: <category>": (folders,
    files, bytes)}; kept folders count 0 files without *measure_kept*),
    ``planned`` and ``done`` ((folders, files, bytes) to delete, and
    deleted), ``failed`` (files not deleted), ``errors`` (their error
    types), ``failed_ids`` ("<kind> <id>"), ``root``."""
    from backend.constants import SOFT_DELETE_GRACE_DAYS
    from backend.utils import audio_storage

    root = pathlib.Path(audio_storage.AUDIO_STORAGE_ROOT
                        if root is None else root)
    now = time.time() if now is None else now
    # Node.deleted_at is naive UTC.
    cutoff = (datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)
              - timedelta(days=SOFT_DELETE_GRACE_DAYS))
    totals = _Totals()
    folders = iter_folders(root) if root.is_dir() else ()
    for batch in _batches(folders, BATCH_SIZE):
        rows = lookup(batch)
        for f in batch:
            cat, gone, files, size = _assess(f, rows, cutoff, now, root,
                                             measure_kept)
            totals.count(f"{f.label}: {cat}", files, size)
            if cat not in TO_DELETE:
                continue
            totals.planned[0] += 1
            totals.planned[1] += files
            totals.planned[2] += size
            if verbose and out is not None:
                verb = "deleting" if apply else "would delete"
                out(f"  {verb} {f.kind} {f.row_id} ({f.label}, {cat}, "
                    f"{files} files)")
            if apply:
                _delete(f, gone, root, totals)
    return {"root": root,
            "categories": {k: tuple(v) for k, v in totals.stats.items()},
            "planned": tuple(totals.planned), "done": tuple(totals.done),
            "failed": totals.failed, "errors": tuple(sorted(totals.errors)),
            "failed_ids": totals.failed_ids}


def _batches(iterable, size):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def delete_row_speech(kind, owner_id, row_id, root=None):
    """Delete the speech folder of a reference or profile whose row is
    gone (``user/<owner>/<kind>/<id>/``), except files another row of
    the same user and kind still points at. For the moment the row's
    delete is committed (a reference has no grace period). Returns the
    FolderDeletion; nothing happens while the row still exists."""
    from backend.utils import audio_storage
    root = pathlib.Path(audio_storage.AUDIO_STORAGE_ROOT
                        if root is None else root)
    path = audio_storage.storage_path(root, "user", owner_id, kind, row_id)
    if not os.path.lexists(path):
        return audio_storage.FolderDeletion()
    f = Folder(kind, f"user/<uid>/{kind}/<id>", owner_id, row_id, path,
               path.is_symlink())
    rows = lookup([f])
    cat, gone = victims(f, classify(f, rows, datetime.min), rows, root)
    if cat != ROW_GONE:
        return audio_storage.FolderDeletion()
    if gone is None:
        return audio_storage.delete_folders([path], root)
    return audio_storage.delete_files(gone, root)


def report(result, apply, out=print):
    """Print a sweep's table of categories, then what was deleted (after
    *apply*) or would be."""
    out(f"audio root: {result['root']}")
    out("")
    out(f"{'category':<78} {'folders':>8} {'files':>8} {'GB':>8}")
    for cat, (folders, files, size) in sorted(result["categories"].items()):
        out(f"{cat:<78} {folders:>8} {files:>8} {size / 1e9:>8.3f}")
    out("")
    if apply:
        done = result["done"]
        out(f"deleted: {done[1]} files, {done[2] / 1e9:.3f} GB, "
            f"from {done[0]} folders")
        if result["failed"]:
            out(f"NOT deleted: {result['failed']} files "
                f"({', '.join(result['errors'])}) in the folders of "
                f"{', '.join(result['failed_ids'])}; run --apply again to "
                f"retry")
    else:
        planned = result["planned"]
        out(f"dry run: would delete {planned[1]} files, "
            f"{planned[2] / 1e9:.3f} GB, from {planned[0]} folders "
            f"(--apply to delete)")
