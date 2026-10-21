"""Delete the audio folders of entries, profiles and saved references
that are gone (#541).

Until #541 the daily cleanup of deleted entries (tasks/node_cleanup.py)
deleted an entry's rows and cleared its audio URLs but left its audio
files on disk: the original recording, a recording's chunks and the
generated speech (an AI reply's is under the model account's folder).
The account purge finds a user's folders through their rows, so the
folders of entries cleaned up before it also outlived the account. The
cleanup now deletes the files; this removes what is already there. After
a restore from backup it also removes the media of rows the restored
database no longer has (docs/RUNBOOKS.md, "Restore").

A folder is deleted only when its owner is certainly gone:

* ``user/<uid>/node/<nid>/`` and ``nodes/<uid>/<nid>/`` (an entry's
  audio): there is no node <nid>; or node <nid> is a tombstone that links
  no audio (deleted, its content, recording and speech URLs cleared) and
  can't come back ("cleaned up"): deleted more than
  SOFT_DELETE_GRACE_DAYS ago, or the account <uid> it was written under
  is deleted for good.
* ``streaming/<uid>/<session>/`` (the older streaming upload's chunks,
  made for one entry, whose ``streaming_session_id`` names it): every
  node that names the session is such a cleaned-up tombstone, and no
  draft or transcript chunk names it.
* ``user/<uid>/profile/<pid>/`` and ``user/<uid>/item/<iid>/``: there is
  no such profile or saved reference (rows a waiting "Delete all my
  writing" hid count as there).

and never when

* account <uid>, or the entry's account, can still come back: an account
  deletion in its grace period, or a "Delete all my writing" (or an
  account deletion's purge) waiting, running or failed;
* a stored audio URL (an entry's, a profile's, a saved reference's or a
  speech chunk's) points into the folder: a profile version and a merged
  saved reference use another row's folder;
* anything in it changed in the last MIN_AGE_HOURS: a folder can be
  written before its row is committed;
* it holds a symlink or anything else that is not a plain file or folder.

Everything else is kept and counted: a recording session folder no row
names, under ``drafts/`` or under ``streaming/`` (Discard deletes a
voice draft's folder itself, so one no row names may be a recording lost
to an error rather than one the user threw away), the upload staging
under ``chunks/``, and every path the layout above does not explain.

Against the wrong database (an empty one, or another instance's) nearly
every folder would look orphaned, so --apply deletes nothing when more
than MAX_DELETE_SHARE of the folders it could delete (entries, streaming
recordings, profiles, references) would go; the output names the
database it read.

Reads ids, owners, deletion dates and whether a column is empty, never
content, so nothing is decrypted. The directory walk and the database
lookups go CHUNK_SIZE folders at a time; only the folders to delete are
kept in memory. The output is the storage root, the database name, and
counts and bytes per kind of folder and reason; the only other path it
prints is that of a folder that could not be deleted, which holds ids
only.

    cd /path/to/write-or-perish
    python backend/scripts/sweep_orphan_audio.py            # dry run
    python backend/scripts/sweep_orphan_audio.py --apply

Verify after: a second dry run reports nothing to delete.
"""
import argparse
import os
import pathlib
import re
import shutil
import stat
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

from sqlalchemy import or_  # noqa: E402

from backend.constants import SOFT_DELETE_GRACE_DAYS  # noqa: E402
from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    Draft, ExternalItem, Node, NodeTranscriptChunk, TTSChunk, User,
    UserDataPurge, UserDataPurgeHidden, UserProfile,
)
from backend.utils.audio_storage import is_storage_id  # noqa: E402
from backend.utils.hidden_rows import (  # noqa: E402
    hidden_ids, including_hidden_rows,
)

# ── Heuristics (see the PR for why each value) ──────────────────────────
# A folder with anything changed this recently is kept: its row may not
# be committed yet. Far above the longest task (Celery's hard limit, 1 h).
MIN_AGE_HOURS = 24
# Folders per batch of database lookups.
CHUNK_SIZE = 500
# Rows per fetch when scanning the stored audio URLs.
URL_FETCH_SIZE = 2000
# --apply deletes nothing when more than this share of the folders it
# could delete (SWEPT_KINDS) would go: against the wrong database (an
# empty one, or another instance's) nearly every folder looks orphaned.
# --max-share raises it.
MAX_DELETE_SHARE = 0.5

# The folders under the storage root whose second level is an account id
# (routes/media.py, OWNER_DIRS).
_TOP_FOLDERS = ("user", "nodes", "drafts", "streaming", "chunks")
# A user, node, profile or reference id as the app writes it in a path.
_ID_RE = re.compile(r"[1-9][0-9]{0,18}")
# The folder a stored audio URL points into, wherever it sits in the URL.
_URL_FOLDER_RE = re.compile(
    r"(?:^|/)(user/[0-9]+/(?:node|profile|item)/[0-9]+|nodes/[0-9]+/[0-9]+)/")
# Purge jobs whose user's data can still come back. "failed" stays here:
# its rows are still hidden, and an admin runs it again.
_PENDING_PURGE_STATUSES = UserDataPurge.ACTIVE_STATUSES + ("failed",)

DELETE, KEEP, GONE = "delete", "keep", "gone"
# The kinds of folder the sweep may delete.
SWEPT_KINDS = ("node", "streaming", "profile", "item")

# Reasons, as printed.
NODE_GONE = "entry folders: the entry is gone"
TOMBSTONE = "entry folders: the entry was deleted and cleaned up"
PROFILE_GONE = "profile folders: the profile is gone"
ITEM_GONE = "saved-reference folders: the reference is gone"
OWNER_THERE = "the entry, profile or reference is there"
RESTORABLE = ("the entry is deleted, inside its grace period or not yet "
              "cleaned up")
PENDING = "an account or writing deletion can still be undone"
REFERENCED = "a stored audio URL points into it"
RECENT = "changed in the last {hours:g} h"
ODD = "holds a symlink or a special file"
STREAM_TOMBSTONE = ("streaming recording folders: the entry was deleted and "
                    "cleaned up")
STREAM_ROW = "streaming recording folders: a row names the session"
STREAM_NO_ROW = ("streaming recording folders: no row names the session "
                 "(not swept)")
DRAFT_ROW = "voice draft folders: a row names the session"
DRAFT_NO_ROW = "voice draft folders: no row names the session (not swept)"
UPLOAD = "upload staging folders (not swept)"
UNKNOWN = "paths the layout does not explain"
VANISHED = "gone before it was deleted"
FAILED = "could not be deleted"


class Folder:
    """One folder (or stray path) under the storage root."""
    __slots__ = ("kind", "path", "rel", "uid", "oid", "files", "bytes",
                 "newest", "odd", "gone")

    def __init__(self, kind, path, rel, uid=None, oid=None):
        self.kind = kind    # SWEPT_KINDS, draft, upload or unknown
        self.path = path
        self.rel = rel      # path under the root, e.g. "user/5/node/12"
        self.uid = uid      # the account id in the path
        self.oid = oid      # the owner's id (int), or a session id (str)
        self.files = self.bytes = 0
        self.newest = 0.0
        self.odd = self.gone = False


class Report:
    """Folders, files and bytes per (action, reason). ``refused`` is the
    share that was exceeded when --apply deleted nothing, else None."""

    def __init__(self):
        self.folders = Counter()
        self.files = Counter()
        self.bytes = Counter()
        self.owner_folders = 0   # folders of SWEPT_KINDS seen
        self.refused = None

    def add(self, key, folder):
        self.folders[key] += 1
        self.files[key] += folder.files
        self.bytes[key] += folder.bytes

    def total(self, action):
        keys = [k for k in self.folders if k[0] == action]
        return (sum(self.folders[k] for k in keys),
                sum(self.files[k] for k in keys),
                sum(self.bytes[k] for k in keys))


# ── The walk ────────────────────────────────────────────────────────────

def _entries(path):
    """The entries of folder *path*, sorted by name."""
    with os.scandir(path) as it:
        return sorted(it, key=lambda e: e.name)


def _plain_dir(entry):
    return entry.is_dir(follow_symlinks=False)


def iter_folders(root):
    """Every owner folder under *root*, and every path the layout does
    not explain (kind "unknown"), one at a time. Never follows a
    symlink."""
    for top in _entries(pathlib.Path(root)):
        if not _plain_dir(top) or top.name not in _TOP_FOLDERS:
            yield Folder("unknown", top.path, top.name)
            continue
        for udir in _entries(top.path):
            rel = f"{top.name}/{udir.name}"
            if not _plain_dir(udir) or not _ID_RE.fullmatch(udir.name):
                yield Folder("unknown", udir.path, rel)
            elif top.name == "user":
                yield from _user_folders(udir, rel)
            else:
                yield from _session_and_node_folders(top.name, udir, rel)


def _user_folders(udir, rel_u):
    """``user/<uid>/{node,profile,item}/<id>/``."""
    uid = int(udir.name)
    for sub in _entries(udir.path):
        rel_s = f"{rel_u}/{sub.name}"
        if not _plain_dir(sub) or sub.name not in ("node", "profile", "item"):
            yield Folder("unknown", sub.path, rel_s)
            continue
        for f in _entries(sub.path):
            rel = f"{rel_s}/{f.name}"
            if _plain_dir(f) and _ID_RE.fullmatch(f.name):
                yield Folder(sub.name, f.path, rel, uid, int(f.name))
            else:
                yield Folder("unknown", f.path, rel)


def _session_and_node_folders(top, udir, rel_u):
    """``nodes/<uid>/<nid>/``, ``drafts/<uid>/<session>/``,
    ``streaming/<uid>/<session>/`` and ``chunks/<uid>/<upload>/``."""
    uid = int(udir.name)
    for f in _entries(udir.path):
        rel = f"{rel_u}/{f.name}"
        if not _plain_dir(f):
            yield Folder("unknown", f.path, rel)
        elif top == "nodes" and _ID_RE.fullmatch(f.name):
            yield Folder("node", f.path, rel, uid, int(f.name))
        elif top == "drafts" and is_storage_id(f.name):
            yield Folder("draft", f.path, rel, uid, f.name)
        elif top == "streaming" and is_storage_id(f.name):
            yield Folder("streaming", f.path, rel, uid, f.name)
        elif top == "chunks":
            yield Folder("upload", f.path, rel, uid, f.name)
        else:
            yield Folder("unknown", f.path, rel)


def _count(folder, st):
    """Add one entry's lstat result to *folder*'s counts."""
    folder.newest = max(folder.newest, st.st_mtime)
    if stat.S_ISDIR(st.st_mode):
        return
    folder.files += 1
    if stat.S_ISREG(st.st_mode):
        folder.bytes += st.st_size
    else:
        folder.odd = True   # a symlink, socket, device or pipe


def measure(folder):
    """Count the files and bytes of *folder* and note the newest change
    and anything that is not a plain file or folder. Never follows a
    symlink. Returns *folder*."""
    folder.files = folder.bytes = 0
    folder.newest = 0.0
    folder.odd = folder.gone = False
    try:
        st = os.lstat(folder.path)
    except FileNotFoundError:
        folder.gone = True
        return folder
    _count(folder, st)
    if not stat.S_ISDIR(st.st_mode):
        return folder

    def onerror(_err):
        folder.odd = True

    for dirpath, dirnames, filenames in os.walk(
            folder.path, onerror=onerror, followlinks=False):
        for name in dirnames + filenames:
            try:
                _count(folder, os.lstat(os.path.join(dirpath, name)))
            except OSError:
                folder.odd = True
    return folder


def _batches(iterable, size):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ── Who owns a folder ───────────────────────────────────────────────────

def pending_user_ids():
    """Accounts whose data can still come back: an account deletion in
    its grace period (``User.deleted_at``), or a purge job (a "Delete all
    my writing" or an account deletion's) waiting, running or failed."""
    ids = {i for (i,) in db.session.query(User.id).filter(
        User.deleted_at.isnot(None))}
    ids |= {i for (i,) in db.session.query(UserDataPurge.user_id).filter(
        UserDataPurge.status.in_(_PENDING_PURGE_STATUSES))}
    return ids


def _node_rows(cond):
    """The nodes matching *cond*: ids, owners, the deletion date, the
    streaming session and whether content and audio URLs are empty, never
    content."""
    return db.session.query(
        Node.id, Node.user_id, Node.human_owner_id, Node.deleted_at,
        Node.streaming_session_id,
        Node.content.is_(None).label("no_content"),
        Node.audio_original_url.is_(None).label("no_recording"),
        Node.audio_tts_url.is_(None).label("no_speech"),
    ).filter(cond).all()


def _hidden_nodes(ids):
    """The ids of *ids* that a "Delete all my writing" hid."""
    if not ids:
        return set()
    return {i for (i,) in db.session.execute(hidden_ids("node").where(
        UserDataPurgeHidden.row_id.in_(sorted(ids))))}


def _accounts(folders):
    """The account ids in the folders' paths that still exist."""
    uids = sorted({f.uid for f in folders})
    return {i for (i,) in db.session.query(User.id).filter(
        User.id.in_(uids))}


def _owners(folder, rows):
    owners = {folder.uid}
    for r in rows:
        owners |= {r.user_id, r.human_owner_id}
    return owners - {None}


def _cleaned_up(r, folder, accounts, cutoff):
    """Node row *r* is a tombstone that links no audio and can't come
    back: deleted, its content and audio URLs cleared, and deleted before
    *cutoff* or written under an account deleted for good."""
    return (r.deleted_at is not None and r.no_content and r.no_recording
            and r.no_speech
            and (r.deleted_at <= cutoff or folder.uid not in accounts))


def _node_verdicts(folders, pending, now):
    """{folder: (action, reason)} for entry folders."""
    rows = {r.id: r for r in _node_rows(
        Node.id.in_(sorted({f.oid for f in folders})))}
    hidden = _hidden_nodes(rows)
    accounts = _accounts(folders)
    cutoff = now - timedelta(days=SOFT_DELETE_GRACE_DAYS)
    out = {}
    for f in folders:
        r = rows.get(f.oid)
        if _owners(f, [r] if r else []) & pending or f.oid in hidden:
            out[f] = (KEEP, PENDING)
        elif r is None:
            out[f] = (DELETE, NODE_GONE)
        elif r.deleted_at is None:
            out[f] = (KEEP, OWNER_THERE)
        elif _cleaned_up(r, f, accounts, cutoff):
            out[f] = (DELETE, TOMBSTONE)
        else:
            out[f] = (KEEP, RESTORABLE)
    return out


def _streaming_verdicts(folders, pending, now):
    """{folder: (action, reason)} for ``streaming/<uid>/<session>/``: it
    goes when every node naming the session is cleaned up and no draft or
    transcript chunk names it. One that no row names is kept."""
    sids = sorted({f.oid for f in folders})
    by_sid = {}
    for r in _node_rows(Node.streaming_session_id.in_(sids)):
        by_sid.setdefault(r.streaming_session_id, []).append(r)
    hidden = _hidden_nodes({r.id for rows in by_sid.values() for r in rows})
    other = _sessions_named(sids, (Draft.session_id,
                                   NodeTranscriptChunk.session_id))
    accounts = _accounts(folders)
    cutoff = now - timedelta(days=SOFT_DELETE_GRACE_DAYS)
    out = {}
    for f in folders:
        rows = by_sid.get(f.oid, [])
        if _owners(f, rows) & pending or any(r.id in hidden for r in rows):
            out[f] = (KEEP, PENDING)
        elif f.oid in other:
            out[f] = (KEEP, STREAM_ROW)
        elif not rows:
            out[f] = (KEEP, STREAM_NO_ROW)
        elif all(_cleaned_up(r, f, accounts, cutoff) for r in rows):
            out[f] = (DELETE, STREAM_TOMBSTONE)
        else:
            out[f] = (KEEP, STREAM_ROW)
    return out


def _row_verdicts(folders, model, gone_reason, pending):
    """{folder: (action, reason)} for profile or saved-reference folders:
    there while a row with the folder's id exists, hidden rows included."""
    ids = sorted({f.oid for f in folders})
    with including_hidden_rows():
        there = {i for (i,) in db.session.query(model.id).filter(
            model.id.in_(ids))}
    out = {}
    for f in folders:
        if f.uid in pending:
            out[f] = (KEEP, PENDING)
        elif f.oid in there:
            out[f] = (KEEP, OWNER_THERE)
        else:
            out[f] = (DELETE, gone_reason)
    return out


def _sessions_named(sids, cols):
    """The session ids of *sids* that a row names in one of *cols*
    (hidden drafts included)."""
    named = set()
    with including_hidden_rows():
        for col in cols:
            named |= {s for (s,) in db.session.query(col).filter(
                col.in_(sids))}
    return named


def _draft_verdicts(folders):
    """Voice draft folders are never deleted, only counted by whether a
    row names the session."""
    named = _sessions_named(
        sorted({f.oid for f in folders}),
        (Draft.session_id, Node.streaming_session_id,
         NodeTranscriptChunk.session_id))
    return {f: (KEEP, DRAFT_ROW if f.oid in named else DRAFT_NO_ROW)
            for f in folders}


def verdicts(folders, pending, now):
    """{folder: (action, reason)} from the database, for one batch."""
    by_kind = {}
    for f in folders:
        by_kind.setdefault(f.kind, []).append(f)
    out = {}
    if by_kind.get("node"):
        out.update(_node_verdicts(by_kind["node"], pending, now))
    if by_kind.get("profile"):
        out.update(_row_verdicts(by_kind["profile"], UserProfile,
                                 PROFILE_GONE, pending))
    if by_kind.get("item"):
        out.update(_row_verdicts(by_kind["item"], ExternalItem, ITEM_GONE,
                                 pending))
    if by_kind.get("streaming"):
        out.update(_streaming_verdicts(by_kind["streaming"], pending, now))
    if by_kind.get("draft"):
        out.update(_draft_verdicts(by_kind["draft"]))
    for f in by_kind.get("upload", ()):
        out[f] = (KEEP, UPLOAD)
    for f in by_kind.get("unknown", ()):
        out[f] = (KEEP, UNKNOWN)
    return out


def url_folder(url):
    """The folder under the storage root that a stored audio URL points
    into ("user/5/node/12"), or None."""
    m = _URL_FOLDER_RE.search(url or "")
    return m.group(1) if m else None


def referenced(rels):
    """The folders of *rels* that a stored audio URL points into: an
    entry's recording or speech, a profile's or saved reference's
    speech, or a speech chunk. Streams the URL columns."""
    found = set()
    queries = (
        db.session.query(Node.audio_original_url, Node.audio_tts_url).filter(
            or_(Node.audio_original_url.isnot(None),
                Node.audio_tts_url.isnot(None))),
        db.session.query(TTSChunk.audio_url).filter(
            TTSChunk.audio_url.isnot(None)),
        db.session.query(UserProfile.audio_tts_url).filter(
            UserProfile.audio_tts_url.isnot(None)),
        db.session.query(ExternalItem.audio_tts_url).filter(
            ExternalItem.audio_tts_url.isnot(None)),
    )
    with including_hidden_rows():
        for query in queries:
            for row in query.yield_per(URL_FETCH_SIZE):
                for url in row:
                    rel = url_folder(url)
                    if rel is not None and rel in rels:
                        found.add(rel)
    return found


# ── The sweep ───────────────────────────────────────────────────────────

def _delete(folder):
    """Delete *folder* and everything in it. True when it is gone."""
    try:
        shutil.rmtree(folder.path)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        print(f"could not delete {folder.rel} ({type(e).__name__})",
              file=sys.stderr)
        return False


class _Run:
    """One sweep's settings and report."""

    def __init__(self, now, min_age, chunk_size):
        self.now = now
        self.min_age = min_age
        self.chunk_size = chunk_size
        self.recent = RECENT.format(hours=min_age.total_seconds() / 3600)
        self.report = Report()

    def gate(self, folder):
        """The reason to keep a folder the database would let go, or
        None. ``now`` is naive UTC, as the database's dates; file times
        are epoch seconds."""
        if folder.odd:
            return ODD
        cutoff = (self.now - self.min_age).replace(tzinfo=timezone.utc)
        if folder.newest > cutoff.timestamp():
            return self.recent
        return None


def _decide_all(run, root):
    """Walk and look up, a batch at a time. Counts what is kept and
    returns [(folder, reason)] for the folders to delete."""
    pending = pending_user_ids()
    candidates = []
    for batch in _batches(iter_folders(root), run.chunk_size):
        for f in batch:
            measure(f)
            if f.kind in SWEPT_KINDS:
                run.report.owner_folders += 1
        decided = verdicts(batch, pending, run.now)
        for f in batch:
            action, reason = decided[f]
            gate = run.gate(f) if action == DELETE else None
            if action == DELETE and gate is None:
                candidates.append((f, reason))
            else:
                run.report.add((KEEP, gate or reason), f)
        db.session.rollback()   # ends the batch's read transaction
    return candidates


def _drop_referenced(run, candidates):
    """A folder a stored audio URL points into is kept."""
    if not candidates:
        return candidates
    refs = referenced({f.rel for f, _ in candidates})
    db.session.rollback()
    for f, _ in candidates:
        if f.rel in refs:
            run.report.add((KEEP, REFERENCED), f)
    return [(f, r) for f, r in candidates if f.rel not in refs]


def _delete_all(run, candidates):
    """Delete the candidates, a batch at a time, after looking again:
    the rows and the folder as they are now, not as at the walk."""
    pending = pending_user_ids()
    for batch in _batches(candidates, run.chunk_size):
        folders = [measure(f) for f, _ in batch]
        decided = verdicts(folders, pending, run.now)
        for f, reason in batch:
            if f.gone:
                run.report.add((GONE, VANISHED), f)
                continue
            action, again = decided[f]
            gate = run.gate(f)
            if action != DELETE or gate is not None:
                run.report.add((KEEP, gate or again), f)
            elif _delete(f):
                run.report.add((DELETE, reason), f)
            else:
                run.report.add((KEEP, FAILED), f)
        db.session.rollback()


def sweep(root, *, apply=False, now=None, min_age=None,
          chunk_size=CHUNK_SIZE, max_share=MAX_DELETE_SHARE):
    """Walk *root*, decide every folder, and with *apply* delete the
    folders whose owner is gone (see the module docstring). Returns a
    Report. Dry run unless *apply*. With *apply*, nothing is deleted when
    more than *max_share* of the folders of SWEPT_KINDS would go
    (``report.refused``; the report then counts what would)."""
    run = _Run(now or datetime.utcnow(),
               timedelta(hours=MIN_AGE_HOURS) if min_age is None else min_age,
               chunk_size)
    root = pathlib.Path(root)
    if not root.is_dir():
        return run.report
    candidates = _drop_referenced(run, _decide_all(run, root))
    if apply and len(candidates) > max_share * run.report.owner_folders:
        run.report.refused = max_share
        apply = False
    if apply:
        _delete_all(run, candidates)
    else:
        for f, reason in candidates:
            run.report.add((DELETE, reason), f)
    return run.report


def _size(n):
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"


def print_report(report, root, database, apply):
    apply = apply and report.refused is None
    print(f"Audio storage root: {root}")
    print(f"Database: {database}")
    print("Entry, streaming recording, profile and reference folders: "
          f"{report.owner_folders}")
    for action, title in ((DELETE, "deleted" if apply else "to delete"),
                          (KEEP, "kept"), (GONE, "already gone")):
        keys = sorted(k for k in report.folders if k[0] == action)
        if action == GONE and not keys:
            continue
        folders, files, size = report.total(action)
        print(f"\n{title}: {folders} folder(s), {files} file(s), "
              f"{_size(size)}")
        for key in keys:
            print(f"  {key[1]}: {report.folders[key]} folder(s), "
                  f"{report.files[key]} file(s), {_size(report.bytes[key])}")
    if report.refused is not None:
        print(f"\nnothing deleted: more than {report.refused:.0%} of the "
              "entry, streaming recording, profile and reference folders "
              "would go. Check that "
              "the database above is the one these files belong to; if the "
              "share is expected, rerun with a higher --max-share")
    elif not apply:
        print("\ndry run, nothing deleted; rerun with --apply")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Delete the audio folders of entries, profiles and "
                    "saved references that are gone (#541)")
    parser.add_argument("--apply", action="store_true",
                        help="delete; without it this only reports")
    parser.add_argument("--min-age-hours", type=float, default=MIN_AGE_HOURS,
                        help="keep folders with anything changed this "
                             f"recently (default {MIN_AGE_HOURS})")
    parser.add_argument("--max-share", type=float, default=MAX_DELETE_SHARE,
                        help="with --apply, delete nothing when more than "
                             "this share of the entry, streaming recording, "
                             "profile and reference folders would go "
                             f"(default {MAX_DELETE_SHARE})")
    args = parser.parse_args(argv)
    if args.min_age_hours < 0:
        parser.error("--min-age-hours must not be negative")
    if not 0 <= args.max_share <= 1:
        parser.error("--max-share must be between 0 and 1")

    from backend import create_app
    app = create_app()
    with app.app_context():
        # The app's own value, read after the backend package loaded the env.
        from backend.utils.audio_storage import AUDIO_STORAGE_ROOT
        report = sweep(AUDIO_STORAGE_ROOT, apply=args.apply,
                       min_age=timedelta(hours=args.min_age_hours),
                       max_share=args.max_share)
        print_report(report, AUDIO_STORAGE_ROOT, db.engine.url.database,
                     args.apply)
        if report.refused is not None:
            return 2
        return 1 if report.folders[(KEEP, FAILED)] else 0


if __name__ == "__main__":
    sys.exit(main())
