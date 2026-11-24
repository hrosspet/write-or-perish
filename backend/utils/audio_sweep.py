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
  made fewer chunks (#66);
* a streaming session folder, ``streaming/<uid>/<session>/``, that no
  node not yet finalized and no draft names, once nothing in it changed
  for the grace period: what is left when the cleanup could not delete a
  finalized node's session folder, or a recording was abandoned;
* the folder that holds the pieces of an audio file's upload,
  ``chunks/<uid>/<upload>/``, once nothing in it changed for
  STAGED_UPLOAD_SECONDS and its entry no longer takes pieces: an upload
  that stopped arriving (the tab was closed, the app was ended), one whose
  entry is gone or deleted, or pieces left behind by a finished one. Its
  entry, if it still waits for the file, then ends ``failed`` with the
  words the upload routes answer for an upload that stopped arriving,
  the one row change the sweep makes.

A recording in a folder whose node row is gone (an upload,
``chunk_*``/``batch_*``, or a ``nodes/`` folder) is kept until nothing in
the folder changed for the grace period, since it may be the only copy
of a node whose commit failed after the recording was moved in (Peter's
rule: a user's words are never lost); until then only that folder's
speech goes.

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
* a row a waiting "Delete all my content" hid: the request's purge
  deletes it, and a restore needs it (the cleanup skips these too);
* a folder with anything changed in the last 24 hours (RECENT_SECONDS).
  A recording is moved into its node's folder, and an upload's folder is
  made, just before the node's row is committed; and an edit or a
  regeneration that supersedes speech marks the folders of the old
  speech changed (audio_storage.mark_speech_superseded), so a playback
  that started before is not cut;
* a streaming session a node not yet finalized or a draft names, or that
  changed within the grace period;
* an upload's pieces while its entry still takes pieces (activity within
  recording_limits.UPLOAD_COUNTS_FOR), whatever the folder's age;
* anything outside those folders, and anything behind a symlink (never
  followed; paths are built with storage_path, and deleted only when
  they resolve inside the root).

Reads folder names, file sizes and times, row ids, deletion times and
stored audio URLs; never a file's content, except the entry id in an
upload folder's ``metadata.json`` (written by /upload/init). Lists one folder at a time and
looks rows up BATCH_SIZE folders per round of queries, ending each read
transaction, so it is cheap enough to run daily on a small VM. With
``measure_kept=False`` (the daily run) it does not measure the folders it
keeps.
"""
import json
import os
import pathlib
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from backend.constants import SOFT_DELETE_GRACE_DAYS

# ── Heuristics (see the PR for why each value) ──────────────────────────
# A folder with a file (or itself) changed this recently is left for a
# later run: the voice path moves a recording into nodes/<uid>/<id>/
# after the node's row is flushed and before it is committed, an upload
# makes user/<uid>/node/<id>/ the same way, and superseding speech marks
# its folder changed. A day is far longer than either window, and than a
# playback.
RECENT_SECONDS = 24 * 3600
# A user's recording is never lost to the sweep while it could still be
# wanted (Peter's rule: a user's words are never lost): a recording in a
# folder whose node row is gone (a node whose commit failed after the
# recording was moved in), and a streaming session folder no node or
# draft uses, are kept until nothing in them changed for the grace
# period, the same 30 days a deleted node is kept.
GRACE_SECONDS = SOFT_DELETE_GRACE_DAYS * 86400
# The pieces of an audio file's upload (chunks/<uid>/<upload>/) go once
# nothing in the folder changed for this long. An upload takes pieces and
# its finalize while it had activity within UPLOAD_COUNTS_FOR (24 hours:
# a device that slept mid-upload continues where it stopped); after that
# the server refuses both (410). Neither app continues an upload under the
# same name after a failure or a restart: it starts again from the file,
# which stays on the person's device. Twice the server's time leaves a
# day between the last moment a piece could arrive and the first moment
# the folder could go. Live recordings are not stored here (streaming/).
STAGED_UPLOAD_SECONDS = 48 * 3600
# metadata.json holds the file's name and one number per piece (at most
# recording_limits.MAX_UPLOAD_CHUNKS, 200): a few kilobytes. A larger
# file is not read, and its folder is judged by its age alone.
UPLOAD_METADATA_MAX_BYTES = 64 * 1024
# Folders per round of database lookups.
BATCH_SIZE = 500

_ID_RE = re.compile(r"[0-9]{1,18}")
# Generated speech (tasks/tts.py, utils/tts_stream.py), plain or encrypted.
SPEECH_FILE_RE = re.compile(r"tts(?:_chunk_[0-9]{1,6})?\.mp3(?:\.enc)?")
# A recording in a user/<uid>/node/<id>/ folder: an upload (original.*)
# or streamed chunks (chunk_*, batch_*). A nodes/<uid>/<id>/ folder is a
# recording as a whole.
RECORDING_FILE_RE = re.compile(r"(?:original\.|chunk_|batch_).+")
# An upload's folder: the name the client chose (routes/nodes.py
# _UPLOAD_ID_RE), with ".finalizing" while its finalize runs.
UPLOAD_DIR_RE = re.compile(r"[A-Za-z0-9_-]{1,64}(?:\.finalizing)?")

ROW_GONE = "row gone"
SPEECH_ONLY = "row gone, speech only (recording younger than grace period kept)"
PAST_GRACE = "node deleted > grace period"
SUPERSEDED = "superseded speech of a live row"
SESSION_UNUSED = "no node or draft uses the session"
UPLOAD_ENDED = "upload no longer takes pieces"
TO_DELETE = (ROW_GONE, SPEECH_ONLY, PAST_GRACE, SUPERSEDED, SESSION_UNUSED,
             UPLOAD_ENDED)
LIVE = "live (kept)"
IN_GRACE = "node deleted, within grace period (kept)"
HIDDEN = "hidden by a waiting 'Delete all my content' (kept)"
SHARED = "row gone, files used by another row (kept)"
RECORDING_HELD = "row gone, recording younger than grace period (kept)"
SESSION_IN_USE = "session of a node or draft (kept)"
RECENT = "changed in the last 24 h (kept for a later run)"
RECENT_GRACE = "changed within the grace period (kept for a later run)"
UPLOAD_TAKING_PIECES = "upload still takes pieces (kept)"
UPLOAD_RECENT = "changed in the last 48 h (kept for a later run)"
SYMLINK = "symlink (left alone)"

_USER_KINDS = ("node", "item", "profile")
_NODES_LABEL = "nodes/<uid>/<id>"
_SESSION_LABEL = "streaming/<uid>/<session>"
_UPLOAD_LABEL = "chunks/<uid>/<upload>"


class Folder(NamedTuple):
    kind: str          # "node", "item", "profile", "session" or "upload"
    label: str         # its layout, ids left out
    owner: int         # the user id in its path
    row_id: object     # the row id; a "session"'s id; an "upload"'s name
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


def _session_dirs(folder):
    """(session id, entry) of the entries of *folder* named like a
    streaming session (audio_storage.is_storage_id), symlinks included
    (never followed)."""
    from backend.utils.audio_storage import is_storage_id
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    return sorted(((e.name, e) for e in entries
                   if is_storage_id(e.name)
                   and (e.is_symlink() or e.is_dir(follow_symlinks=False))),
                  key=lambda pair: pair[0])


def _upload_dirs(folder):
    """(name, entry) of the entries of *folder* named like an upload's
    folder (UPLOAD_DIR_RE), symlinks included (never followed)."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    return sorted(((e.name, e) for e in entries
                   if UPLOAD_DIR_RE.fullmatch(e.name)
                   and (e.is_symlink() or e.is_dir(follow_symlinks=False))),
                  key=lambda pair: pair[0])


def iter_folders(root):
    """Every row folder under *root*: ``user/<uid>/node|item|profile/<id>``,
    ``nodes/<uid>/<id>``, every streaming session folder,
    ``streaming/<uid>/<session>``, and every upload's folder of pieces,
    ``chunks/<uid>/<upload>``. A user folder (or its ``node``, ``item`` or
    ``profile`` folder) that is a symlink is passed over, never followed.
    Paths are built with storage_path (an upload's from a name that
    matched UPLOAD_DIR_RE, one plain folder name), so they stay inside
    the root."""
    from backend.utils.audio_storage import storage_path
    root = pathlib.Path(root)
    yield from _row_folders(root, storage_path)
    top = root / "streaming"
    if not top.is_symlink() and top.is_dir():
        for owner_id, owner in _id_dirs(top):
            if owner.is_symlink():
                continue
            for session_id, entry in _session_dirs(owner.path):
                yield Folder("session", _SESSION_LABEL, owner_id, session_id,
                             storage_path(root, "streaming", owner_id,
                                          session_id),
                             entry.is_symlink())
    top = root / "chunks"
    if top.is_symlink() or not top.is_dir():
        return
    for owner_id, owner in _id_dirs(top):
        if owner.is_symlink():
            continue
        for name, entry in _upload_dirs(owner.path):
            yield Folder("upload", _UPLOAD_LABEL, owner_id, name,
                         storage_path(root, "chunks", owner_id) / name,
                         entry.is_symlink())


def _row_folders(root, storage_path):
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
                label = f"user/<uid>/{kind}/<id>" if kind else _NODES_LABEL
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
    hidden: dict   # kind -> {row id} a waiting "Delete all my content" hid
    refs: dict     # ("node", node id) / (kind, user id) -> {path keys}
    sessions: set = frozenset()   # streaming session ids still in use
    # (owner, upload name) of the upload folders whose entry still takes
    # pieces, and -> entry id of the others whose entry is their owner's.
    uploads: set = frozenset()
    upload_nodes: dict = {}


def _upload_node_id(path):
    """The entry id an upload's folder was made for (its metadata.json,
    written by /upload/init), or None when the file is missing, a symlink,
    larger than UPLOAD_METADATA_MAX_BYTES or not as init writes it. The
    one file the sweep opens; never a piece."""
    try:
        fd = os.open(os.path.join(path, "metadata.json"),
                     os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(UPLOAD_METADATA_MAX_BYTES + 1)
        if len(raw) > UPLOAD_METADATA_MAX_BYTES:
            return None
        node_id = json.loads(raw).get("node_id")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(node_id, bool) or not isinstance(node_id, int) \
            or not 0 < node_id < 10 ** 18:
        return None
    return node_id


def _uploads(folders, now):
    """(still taking pieces, entry ids of the rest) for the upload folders
    in *folders*, keyed (owner, name). An upload takes pieces while its
    entry is its owner's, not deleted, has no file yet and had activity
    within UPLOAD_COUNTS_FOR of *now* (a POSIX time): what the upload
    routes check (recording_limits.upload_expired)."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.recording_limits import UPLOAD_COUNTS_FOR
    named = {}
    for f in folders:
        node_id = _upload_node_id(f.path)
        if node_id is not None:
            named[(f.owner, f.row_id)] = node_id
    if not named:
        return set(), {}
    since = (datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)
             - UPLOAD_COUNTS_FOR)
    nodes = {r.id: r for r in db.session.query(
        Node.id, Node.user_id, Node.deleted_at, Node.audio_original_url,
        Node.upload_activity_at, Node.created_at).filter(
        Node.id.in_(sorted(set(named.values()))))}
    taking, ended = set(), {}
    for key, node_id in named.items():
        r = nodes.get(node_id)
        if r is None or r.user_id != key[0]:
            continue
        last = r.upload_activity_at or r.created_at
        if (r.deleted_at is None and r.audio_original_url is None
                and last is not None and last >= since):
            taking.add(key)
        else:
            ended[key] = node_id
    return taking, ended


def _sessions_in_use(session_ids, cutoff):
    """The streaming sessions a node not yet finalized names (live, in
    the grace period, or hidden; any node when *cutoff* is None) or a
    draft names. A node past the grace period does not count: the
    cleanup deletes its session folder when it finalizes it."""
    from backend.extensions import db
    from backend.models import Draft, Node
    sids = sorted(session_ids)
    in_use = set()
    finalizing = {}
    for sid, nid, deleted_at in db.session.query(
            Node.streaming_session_id, Node.id, Node.deleted_at).filter(
            Node.streaming_session_id.in_(sids)):
        if cutoff is None or deleted_at is None or deleted_at > cutoff:
            in_use.add(sid)
        else:
            finalizing[nid] = sid
    in_use |= {finalizing[nid] for nid in _hidden("node", finalizing)}
    in_use |= {s for (s,) in db.session.query(Draft.session_id).filter(
        Draft.session_id.in_(sids))}
    return in_use


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


def lookup(folders, cutoff=None, now=None):
    """What the rows say about *folders*: ids, deletion times, stored
    audio URLs, the streaming sessions in use (see _sessions_in_use;
    *cutoff* is the grace period's), the uploads that still take pieces
    at *now* (see _uploads). Hidden rows are read too (and kept by
    classify)."""
    from backend.extensions import db
    from backend.models import ExternalItem, TTSChunk, UserProfile
    from backend.utils.hidden_rows import including_hidden_rows
    ids = defaultdict(set)
    owners = defaultdict(set)
    uploads = []
    for f in folders:
        if f.is_link:
            continue
        if f.kind == "upload":
            uploads.append(f)
            continue
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
        sessions = (_sessions_in_use(ids["session"], cutoff)
                    if ids["session"] else set())
        taking, ended = _uploads(
            uploads, time.time() if now is None else now)
    # Read-only: end the transaction so none stays open during the walk.
    db.session.rollback()
    return Rows(rows, hidden, refs, sessions, taking, ended)


def classify(f, rows, cutoff):
    """The category of a folder, before the files are looked at."""
    if f.is_link:
        return SYMLINK
    if f.kind == "session":
        return SESSION_IN_USE if f.row_id in rows.sessions else SESSION_UNUSED
    if f.kind == "upload":
        return (UPLOAD_TAKING_PIECES if (f.owner, f.row_id) in rows.uploads
                else UPLOAD_ENDED)
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
    if cat in (PAST_GRACE, SESSION_UNUSED, UPLOAD_ENDED) or (
            cat == ROW_GONE and f.kind == "node"):
        return cat, None
    if cat not in (ROW_GONE, LIVE):
        return cat, []
    if cat == LIVE and f.label == _NODES_LABEL:
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


def _holds_recording(f):
    """True when a node folder holds a recording: a nodes/ folder always
    does; a user/ folder when an upload or streamed chunks are in it."""
    if f.label == _NODES_LABEL:
        return True
    return any(RECORDING_FILE_RE.fullmatch(p.name)
               for p in _children(f.path, speech_only=False))


def _assess(f, rows, cutoff, now, root, measure_kept):
    """(category, files to delete or None for the whole folder, files,
    bytes) of one folder, after the age checks: nothing that changed in
    the last 24 h; no upload's pieces that changed within
    STAGED_UPLOAD_SECONDS; no unused streaming session, and no recording
    in a folder whose node row is gone, that changed within the grace
    period (only that folder's speech goes)."""
    cat, gone = victims(f, classify(f, rows, cutoff), rows, root)
    if cat not in TO_DELETE:
        if measure_kept and not f.is_link:
            files, size, _ = folder_stats(f.path)
            return cat, [], files, size
        return cat, [], 0, 0
    files, size, newest = folder_stats(f.path)
    age = now - newest
    if cat == UPLOAD_ENDED:
        if age < STAGED_UPLOAD_SECONDS:
            return f"{cat}, {UPLOAD_RECENT}", [], files, size
        return cat, gone, files, size
    if age < RECENT_SECONDS:
        return f"{cat}, {RECENT}", [], files, size
    if age < GRACE_SECONDS:
        if cat == SESSION_UNUSED:
            return f"{cat}, {RECENT_GRACE}", [], files, size
        if cat == ROW_GONE and f.kind == "node" and _holds_recording(f):
            speech = ([] if f.label == _NODES_LABEL
                      else _children(f.path, speech_only=True))
            if not speech:
                return RECORDING_HELD, [], files, size
            cat, gone = SPEECH_ONLY, speech
    if gone is not None:
        files, size = len(gone), _size(gone)
    return cat, gone, files, size


def _end_upload_entry(node_id, owner):
    """The entry of an upload whose pieces the sweep removed, if it still
    waits for its file, ends ``failed`` with the words the upload routes
    answer for an upload that stopped arriving (as /upload/cleanup ends
    one the app reports as failed). ``updated_at`` stays: not an edit."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.recording_limits import UPLOAD_EXPIRED_TEXT
    Node.query.filter(
        Node.id == node_id, Node.user_id == owner,
        Node.transcription_status == "pending",
        Node.audio_original_url.is_(None),
    ).update({Node.transcription_status: "failed",
              Node.transcription_error: UPLOAD_EXPIRED_TEXT,
              Node.updated_at: Node.updated_at},
             synchronize_session=False)
    db.session.commit()


def _delete(f, gone, root, totals, rows=None):
    from backend.utils import audio_storage
    result = (audio_storage.delete_folders([f.path], root) if gone is None
              else audio_storage.delete_files(gone, root))
    if f.kind == "upload" and rows is not None and not result.failed \
            and not result.skipped:
        node_id = rows.upload_nodes.get((f.owner, f.row_id))
        if node_id is not None:
            _end_upload_entry(node_id, f.owner)
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
        rows = lookup(batch, cutoff, now)
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
                _delete(f, gone, root, totals, rows)
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
