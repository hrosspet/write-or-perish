"""Delete the audio files that finalized nodes left behind (one-off).

Until the daily cleanup deleted a node's audio files with its rows
(backend/tasks/node_cleanup.py), a purged node's files, and a wiped
tombstone's, stayed on disk. This script deletes them: the node folders
under the audio storage root

    user/<user id>/node/<node id>/
    nodes/<user id>/<node id>/

whose node row no longer exists, or whose node was deleted more than
SOFT_DELETE_GRACE_DAYS ago. Encrypted (``.enc``) and plain files alike,
recordings and generated speech.

Never touched:

* a live node's folder;
* a node deleted within the grace period (it can still be restored);
* a node a waiting "Delete all my writing" hid: the request's purge
  deletes it, and a restore needs it;
* a folder with anything changed in the last 24 hours (RECENT_SECONDS):
  a recording is moved into its node's folder just before the node's
  row is committed;
* anything outside those node folders, and anything behind a symlink
  (never followed).

Reads folder names, file sizes and times, and node ids with their
deletion times; never a file's content. Walks one node folder at a time
and looks nodes up BATCH_SIZE ids per query, so it runs on a small VM.

Run from the repo root with the app's environment (the same audio
storage root as the app; the script prints it first):

    cd /path/to/write-or-perish
    python backend/scripts/purge_orphaned_node_audio.py             # dry run
    python backend/scripts/purge_orphaned_node_audio.py --verbose   # + node ids
    python backend/scripts/purge_orphaned_node_audio.py --apply

Verify after: a dry run reports 0 folders to delete. The dry run prints
counts and GB per category; ``--verbose`` adds the node ids of the
folders it deletes (or would). Exits 1 when ``--apply`` could not delete
a file; running it again retries.
"""
import argparse
import os
import pathlib
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

# ── Heuristics (see the PR for why each value) ──────────────────────────
# A folder with a file (or itself) changed this recently is left for a
# later run: the voice path moves a recording into nodes/<uid>/<id>/
# after the node's row is flushed and before it is committed, and an
# upload writes user/<uid>/node/<id>/ the same way. A day is far longer
# than either window.
RECENT_SECONDS = 24 * 3600
# Node ids per database lookup.
BATCH_SIZE = 500

LAYOUTS = ("user", "nodes")
_ID_RE = re.compile(r"[0-9]{1,18}")

ROW_GONE = "node row gone"
PAST_GRACE = "node deleted > grace period"
TO_DELETE = (ROW_GONE, PAST_GRACE)
LIVE = "live node (kept)"
IN_GRACE = "node deleted, within grace period (kept)"
HIDDEN = "hidden by a waiting 'Delete all my writing' (kept)"
RECENT = "changed in the last 24 h (kept for a later run)"
SYMLINK = "symlink (left alone)"


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


def iter_node_folders(root):
    """(layout, node id, path, is_symlink) of every node folder under
    *root*: ``user/<uid>/node/<id>`` and ``nodes/<uid>/<id>``. A user
    folder (or its ``node`` folder) that is a symlink is passed over,
    never followed. Paths are built with storage_path, so they stay
    inside the root."""
    from backend.utils.audio_storage import storage_path
    root = pathlib.Path(root)
    for layout in LAYOUTS:
        top = root / layout
        if top.is_symlink() or not top.is_dir():
            continue
        for owner_id, owner in _id_dirs(top):
            if owner.is_symlink():
                continue
            if layout == "user":
                base = storage_path(root, "user", owner_id, "node")
                if base.is_symlink() or not base.is_dir():
                    continue
            else:
                base = storage_path(root, "nodes", owner_id)
            for node_id, entry in _id_dirs(base):
                parts = ((layout, owner_id, "node", node_id)
                         if layout == "user" else (layout, owner_id, node_id))
                yield (layout, node_id, storage_path(root, *parts),
                       entry.is_symlink())


def folder_stats(path):
    """(files, bytes, newest change time) of a folder: lstat only,
    symlinks counted as entries and never followed."""
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


def _lookup(node_ids):
    """({node id: deleted_at} of the rows that exist, set of the ids a
    waiting "Delete all my writing" hid). Ids and times only."""
    from backend.extensions import db
    from backend.models import Node, UserDataPurgeHidden
    rows = dict(db.session.query(Node.id, Node.deleted_at).filter(
        Node.id.in_(node_ids)).all())
    hidden = {r for (r,) in db.session.query(UserDataPurgeHidden.row_id)
              .filter(UserDataPurgeHidden.kind == "node",
                      UserDataPurgeHidden.row_id.in_(node_ids))}
    # Read-only: end the transaction so none stays open during the walk.
    db.session.rollback()
    return rows, hidden


def classify(node_id, rows, hidden, cutoff):
    """The category of a node folder (one of the labels above, before
    the recent-change check)."""
    if node_id not in rows:
        return ROW_GONE
    deleted_at = rows[node_id]
    if deleted_at is None:
        return LIVE
    if node_id in hidden:
        return HIDDEN
    if deleted_at > cutoff:
        return IN_GRACE
    return PAST_GRACE


def _batches(iterable, size):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _report(out, stats, planned, done, failed, errors):
    """The table of categories, then what was deleted (*done*, after
    --apply) or would be (*planned*)."""
    out("")
    out(f"{'category':<78} {'folders':>8} {'files':>8} {'GB':>8}")
    for cat, (folders, files, size) in sorted(stats.items()):
        out(f"{cat:<78} {folders:>8} {files:>8} {size / 1e9:>8.3f}")
    out("")
    if done is not None:
        out(f"deleted: {done[1]} files, {done[2] / 1e9:.3f} GB, "
            f"from {done[0]} node folders")
        if failed:
            out(f"NOT deleted: {failed} files ({', '.join(sorted(errors))}); "
                f"run --apply again to retry")
    else:
        out(f"dry run: would delete {planned[1]} files, "
            f"{planned[2] / 1e9:.3f} GB, from {planned[0]} node folders "
            f"(--apply to delete)")


def run(apply=False, verbose=False, root=None, now=None, out=print):
    """Classify every node folder and, with *apply*, delete those of the
    TO_DELETE categories. Returns a dict of totals."""
    from backend.constants import SOFT_DELETE_GRACE_DAYS
    from backend.utils import audio_storage

    root = pathlib.Path(audio_storage.AUDIO_STORAGE_ROOT
                        if root is None else root)
    now = time.time() if now is None else now
    # Node.deleted_at is naive UTC.
    cutoff = (datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)
              - timedelta(days=SOFT_DELETE_GRACE_DAYS))
    out(f"audio root: {root}")
    if not root.is_dir():
        out("audio root not found: nothing to do")
        return {"to_delete": 0, "deleted_files": 0, "failed": 0}

    stats = defaultdict(lambda: [0, 0, 0])   # category -> folders, files, bytes
    planned = [0, 0, 0]                      # the TO_DELETE folders, files, bytes
    done = [0, 0, 0]                         # deleted folders, files, bytes
    failed = 0
    errors = set()
    for batch in _batches(iter_node_folders(root), BATCH_SIZE):
        rows, hidden = _lookup(sorted({b[1] for b in batch}))
        for layout, node_id, path, is_link in batch:
            if is_link:
                cat, files, size = SYMLINK, 0, 0
            else:
                cat = classify(node_id, rows, hidden, cutoff)
                files, size, newest = folder_stats(path)
                if cat in TO_DELETE and now - newest < RECENT_SECONDS:
                    cat = f"{cat}, {RECENT}"
            row = stats[f"{layout}/: {cat}"]
            row[0] += 1
            row[1] += files
            row[2] += size
            if cat not in TO_DELETE:
                continue
            planned[0] += 1
            planned[1] += files
            planned[2] += size
            if verbose:
                verb = "deleting" if apply else "would delete"
                out(f"  {verb} node {node_id} ({layout}/, {cat}, "
                    f"{files} files)")
            if apply:
                result = audio_storage.delete_folders([path], root)
                done[0] += 1
                done[1] += result.files
                done[2] += result.bytes
                failed += result.failed
                errors.update(result.errors)
                if result.failed or result.skipped:
                    out(f"  node {node_id} ({layout}/): {result.failed} "
                        f"file(s) not deleted {list(result.errors)}, "
                        f"{result.skipped} folder(s) left alone")

    _report(out, stats, planned, done if apply else None, failed, errors)
    return {"to_delete": planned[0], "deleted_files": done[1],
            "deleted_bytes": done[2], "failed": failed,
            "categories": {k: tuple(v) for k, v in stats.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Delete the audio folders of nodes that are gone, or "
                    "deleted more than the grace period ago (dry run by "
                    "default; never reads a file's content).")
    parser.add_argument("--apply", action="store_true",
                        help="delete the files (default: dry run)")
    parser.add_argument("--verbose", action="store_true",
                        help="print the node id of each folder deleted")
    args = parser.parse_args(argv)
    from backend import create_app
    app = create_app()
    with app.app_context():
        result = run(apply=args.apply, verbose=args.verbose)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
