"""Delete all of one user's data (#268).

One operation, used by the user's "Delete all my writing" (after a grace
period) and by the admin dashboard's "Purge data" (at once):

* ``schedule_purge`` of the user's own request hides at once everything
  the purge will delete (Peter, 2026-10-09: "a soft-delete of everything
  immediately + real deletion after 30 days"): the user's nodes are
  soft-deleted and the rows of the per-user tables are left out of every
  query (backend/utils/hidden_rows.py). What it hid is recorded per job
  (UserDataPurgeHidden), so ``cancel_purge`` ("Restore my writing")
  brings back exactly that, in one transaction, and the purge after the
  grace period deletes exactly that (``Scope``): what the user writes
  after the request stays.

* ``stop_in_flight`` stops what would write the user's data again behind
  the purge: queued Celery tasks are revoked, provider batches that carry
  only this user's requests are cancelled, the user's items are taken out
  of shared batch jobs and the profile pipeline flags are reset.
* ``purge_user_content`` deletes the rows and files. It reads ids and
  metadata only, never content, so nothing is ever decrypted, and every
  statement is scoped to the one user: other users' rows are changed only
  where a foreign key to a deleted row requires it (a reply's
  ``linked_node_id``, a draft's ``parent_id``), and then only that column.
  Before it deletes the stored X connection, it revokes the connection's
  tokens at X; a failed call is logged and never stops the purge.
* ``count_user_data`` is the dry run: the same counts, nothing changed.

The account itself stays (login, username, settings, plan). Cost rows
stay too, moved to the ``loore-erased`` system account with every field
that could lead back to the person cleared.

Which nodes are the user's (S): ``user_id`` or ``human_owner_id`` is the
user (their own writing, imports, and the AI replies they asked for,
which are stored under the model's account), plus legacy AI replies with
no ``human_owner_id`` whose owner by ``privacy.find_human_owner`` is the
user. A node of S that has another user's node anywhere below it is kept
as an empty tombstone (K), as soft-delete does, so the other user's
reply keeps its parent; the rest (D) is deleted, deepest first, in chunks
of PURGE_CHUNK_SIZE with a commit per chunk.

Files: every node of S has its folders deleted (``user/<author>/node/<id>``
and ``nodes/<author>/<id>`` for the author and the human owner, which is
how AI-reply audio stored under the model account's folder is found),
then the user's own folders (``user/``, ``nodes/``, ``drafts/``,
``chunks/``, ``streaming/`` under AUDIO_STORAGE_PATH and the import stash
``imports/<id>/``) and the X API dumps of the handle the account was
pre-filled from. Every stored audio URL points inside these folders.

Re-running is safe: each step selects what is still there. A purge that
crashed half way continues where it stopped.

Verified by counting: after the last round the dry-run count must be
zero for every table and file, and no batch job may still carry the
user's items. Otherwise the run fails with PurgeIncomplete and is
retried; it is never reported done with anything left.
"""
import json
import logging
import os
import pathlib
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from sqlalchemy import and_, case, false, func, or_, select

from backend.extensions import db
from backend.models import (
    HIDDEN_ROW_TABLES, APICostLog, ArtifactView, Draft, ExternalAccount,
    ExternalDigestBatchJob, ExternalItem, ExternalItemEmbedding, FeedPick,
    FeedRender, Node, NodeContextArtifact, NodeEmbedding, NodeTranscriptChunk,
    NodeVersion, PollDraftBatchJob, PollResponse, ProfileBatchJob,
    RecentContextBatchJob, ReferenceAction, ShareDraft, TTSChunk, Thread,
    User, UserArtifact, UserDataPurge, UserDataPurgeHidden, UserFeedback,
    UserNotification, UserProfile, UserPrompt, UserRecentContext, UserTodo,
)
from backend.utils.hidden_rows import hidden_ids, including_hidden_rows

logger = logging.getLogger(__name__)

# ── Heuristics (see the PR for why each value) ──────────────────────────
# The grace period of a user's own request: Petr's deletion rule
# (LOORE-ESSENCE "Deletion", 2026-10-02), the same 30 days as account
# deletion (#269).
PURGE_GRACE_DAYS = 30
# Ids per statement and per commit. The 2026-08-28 manual purge ran
# 91,850 nodes in 27 s with IN lists of 500.
PURGE_CHUNK_SIZE = 500
# Passes over everything, to pick up rows written while the purge ran.
PURGE_MAX_ROUNDS = 3
# A running job whose runner has not sent a heartbeat for this long is
# claimed again (a chunk takes seconds; a deploy kills the worker).
PURGE_STALE_AFTER = timedelta(minutes=10)
# Runs that started and did not finish before a job is marked failed and
# logged as an error. A claim whose runner never started does not count.
PURGE_MAX_ATTEMPTS = 3
# A due job with no runner started for this long is logged as an error
# (still dispatched again each time it goes stale).
PURGE_START_OVERDUE = timedelta(hours=1)
# While the user's tasks are still running, look again after this long...
PURGE_WAIT_RETRY_SECONDS = 120
# ...for at most this long: Celery's hard time limit (task_time_limit,
# 1 h) has killed any task by then.
PURGE_MAX_WAIT = timedelta(minutes=65)
# Each call to X's token revoke endpoint (connect and read, each): the
# purge does not wait longer for X, and a call that times out is logged
# and the stored connection deleted anyway. X answers in well under a
# second; at most two calls per connection.
X_REVOKE_TIMEOUT_SECONDS = 10

IN_FLIGHT_STATUSES = ("pending", "processing")
# The user's own folders under AUDIO_STORAGE_PATH.
USER_AUDIO_FOLDERS = ("user", "nodes", "drafts", "chunks", "streaming")
# The live states of a Read's provider batch (llm_completion).
_LIVE_BATCH_STATUSES = ("submitted", "cancelling")
# x_api_dump_path: <handle>-<UTC stamp>.jsonl
_X_DUMP_STAMP = r"-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.jsonl"
_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,64}")

# Keys that describe what the purge keeps, not what it deletes.
INFO_KEYS = ("node_tombstoned", "others_replies_kept")


class PurgeRefused(Exception):
    """The account must never be purged (an AI or system account)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class PurgeSuperseded(Exception):
    """Another runner claimed the job; this one stops."""


class PurgeIncomplete(Exception):
    """The user's rows, files or batch items are still there after the
    purge. The run fails: the beat retries it, and after
    PURGE_MAX_ATTEMPTS the job is marked failed and logged as an error.
    The message names tables and counts, never content."""


def _now():
    return datetime.utcnow()


def _chunks(seq, size=None):
    size = size or PURGE_CHUNK_SIZE
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ── Who may be purged ───────────────────────────────────────────────────

def purge_refusal(user):
    """Why *user* must never be purged, or None.

    AI replies are stored with ``user_id`` = the ``llm-<model>`` account,
    so a purge of that account would delete every AI reply in Loore. The
    rule is the one ``User.profile_eligible_query`` uses (the account
    authors ``node_type='llm'`` nodes), plus the old ``llm`` id prefix and
    the system accounts by name."""
    from backend.utils.system_accounts import SYSTEM_USERNAMES
    if user is None:
        return "no such account"
    if user.username in SYSTEM_USERNAMES:
        return "system account"
    if (user.twitter_id or "").startswith("llm"):
        return "AI account"
    authors_ai = db.session.query(Node.id).filter(
        Node.user_id == user.id, Node.node_type == "llm").first()
    if authors_ai is not None:
        return "AI account"
    return None


# ── The user's nodes ────────────────────────────────────────────────────

def _legacy_ai_reply_ids(user_id):
    """AI replies with no ``human_owner_id`` that ``find_human_owner``
    gives to *user_id*: the nearest ancestor that is not an AI reply is
    the user's. Reads ids, types and authors only."""
    cands = db.session.query(Node.id, Node.parent_id).filter(
        Node.node_type == "llm", Node.human_owner_id.is_(None)).all()
    if not cands:
        return set()
    known = {nid: ("llm", None, pid) for nid, pid in cands}
    need = {pid for _, pid in cands if pid is not None and pid not in known}
    while need:
        for chunk in _chunks(need):
            for r in db.session.query(
                    Node.id, Node.node_type, Node.user_id, Node.parent_id
            ).filter(Node.id.in_(chunk)):
                known[r.id] = (r.node_type, r.user_id, r.parent_id)
        nxt = set()
        for nid in need:
            row = known.get(nid)
            if (row and row[0] == "llm" and row[2] is not None
                    and row[2] not in known):
                nxt.add(row[2])
        need = nxt

    def owner(nid):
        seen = set()
        cur = known[nid][2]
        while cur is not None and cur not in seen:
            seen.add(cur)
            row = known.get(cur)
            if row is None:
                return None
            if row[0] != "llm":
                return row[1]
            cur = row[2]
        return None

    return {nid for nid, _ in cands if owner(nid) == user_id}


def _owned(user_id, extras):
    """Filter for the user's nodes (S)."""
    conds = [Node.user_id == user_id, Node.human_owner_id == user_id]
    if extras:
        conds.append(Node.id.in_(sorted(extras)))
    return or_(*conds)


def _not_owned(user_id, extras):
    """The exact complement of _owned. Spelled out so a NULL
    human_owner_id (legacy rows) counts as not the user's: NOT (a OR
    NULL) is NULL in SQL and would drop the row."""
    conds = [Node.user_id != user_id,
             or_(Node.human_owner_id.is_(None),
                 Node.human_owner_id != user_id)]
    if extras:
        conds.append(~Node.id.in_(sorted(extras)))
    return and_(*conds)


class Scope(NamedTuple):
    """What a purge deletes.

    Everything of the user's (``job_id`` None): the admin purge, an
    account deletion, an admin bringing a user's request forward.

    What one "Delete all my writing" request hid (``job_id`` set,
    UserDataPurgeHidden): the nodes it soft-deleted and the ones the
    user had deleted before (still deleted now; a re-import that brought
    one back made it the user's again), the rows of the per-user tables
    it hid, the folders that existed then, and the cost rows written
    until the request. What the user wrote afterwards stays. The X
    connection, notifications and artifact views go either way, as the
    dialog says."""
    user_id: int
    job_id: Optional[int] = None
    requested_at: Optional[datetime] = None

    @property
    def everything(self):
        return self.job_id is None

    def rows(self, model):
        """The user's rows of a per-user table that the purge deletes."""
        cond = model.user_id == self.user_id
        if self.everything or model not in HIDDEN_ROW_TABLES:
            return cond
        return and_(cond, model.id.in_(
            hidden_ids(model.__tablename__, self.job_id)))

    def other_rows(self, model):
        """The exact complement of rows(): other users' rows, and the
        user's rows the purge keeps."""
        if self.everything or model not in HIDDEN_ROW_TABLES:
            return model.user_id != self.user_id
        return or_(model.user_id != self.user_id, ~model.id.in_(
            hidden_ids(model.__tablename__, self.job_id)))

    def nodes(self, extras):
        """Filter for the nodes the purge deletes (S)."""
        if self.everything:
            return _owned(self.user_id, extras)
        return and_(Node.deleted_at.isnot(None),
                    or_(Node.id.in_(hidden_ids("node", self.job_id)),
                        Node.id.in_(hidden_ids("node_deleted", self.job_id))))

    def not_nodes(self, extras):
        """The exact complement of nodes()."""
        if self.everything:
            return _not_owned(self.user_id, extras)
        return or_(Node.deleted_at.is_(None),
                   and_(~Node.id.in_(hidden_ids("node", self.job_id)),
                        ~Node.id.in_(hidden_ids("node_deleted",
                                                self.job_id))))

    def cost_rows(self):
        cond = APICostLog.user_id == self.user_id
        if self.everything:
            return cond
        return and_(cond, APICostLog.created_at <= self.requested_at)


def scope_of(job):
    """The Scope a job purges."""
    if job is not None and job.scope == "hidden":
        return Scope(job.user_id, job.id, job.requested_at)
    return Scope(job.user_id if job is not None else None)


class NodePlan(NamedTuple):
    extras: set        # legacy AI replies that are the user's
    parents: dict      # S: node id -> parent id
    authors: dict      # S: node id -> {user_id, human_owner_id}
    keep: set          # K: tombstones (another user's node is below)
    deleted: list      # D: deepest first
    others_under: int  # nodes not in S directly under a node of S
    scope: Scope = None

    def owned_select(self, user_id):
        return select(Node.id).where(self._scope(user_id).nodes(self.extras))

    def deleted_select(self, user_id):
        cond = self._scope(user_id).nodes(self.extras)
        if self.keep:
            cond = and_(cond, ~Node.id.in_(sorted(self.keep)))
        return select(Node.id).where(cond)

    def not_owned(self, user_id):
        return self._scope(user_id).not_nodes(self.extras)

    def _scope(self, user_id):
        return self.scope if self.scope is not None else Scope(user_id)


def plan_nodes(user_id, scope=None):
    """Which of the user's nodes are deleted (D) and which stay as
    tombstones (K). Ids and authors only."""
    scope = scope or Scope(user_id)
    extras = _legacy_ai_reply_ids(user_id) if scope.everything else set()
    parents, authors = {}, {}
    for r in db.session.query(
            Node.id, Node.parent_id, Node.user_id, Node.human_owner_id
    ).filter(scope.nodes(extras)):
        parents[r.id] = r.parent_id
        authors[r.id] = {r.user_id, r.human_owner_id} - {None}

    keep, others_under = set(), 0
    for chunk in _chunks(parents):
        for cid, pid in db.session.query(Node.id, Node.parent_id).filter(
                Node.parent_id.in_(chunk)):
            if cid in parents:
                continue
            others_under += 1
            p = pid
            while p is not None and p in parents and p not in keep:
                keep.add(p)
                p = parents[p]

    depth = {}
    for n in parents:
        path, on_path, cur = [], set(), n
        while cur in parents and cur not in depth and cur not in on_path:
            path.append(cur)
            on_path.add(cur)
            cur = parents[cur]
        base = depth.get(cur, -1) if cur in parents else -1
        for x in reversed(path):
            base += 1
            depth[x] = base
    deleted = sorted((n for n in parents if n not in keep),
                     key=lambda n: (-depth.get(n, 0), n))
    return NodePlan(extras, parents, authors, keep, deleted, others_under,
                    scope)


def _session_ids(user_id, plan):
    """Streaming sessions of the user: their drafts' sessions, their
    nodes' sessions and the session folders under ``drafts/<id>/`` (left
    by abandoned recordings whose draft is gone). Draft session ids are
    server uuids, so none can be another user's."""
    from backend.utils.audio_storage import is_storage_id
    scope = plan._scope(user_id)
    sessions = {s for (s,) in db.session.query(Draft.session_id).filter(
        scope.rows(Draft), Draft.session_id.isnot(None))}
    for chunk in _chunks(plan.parents):
        sessions.update(s for (s,) in db.session.query(
            Node.streaming_session_id).filter(
            Node.id.in_(chunk), Node.streaming_session_id.isnot(None)))
    if scope.everything:
        folder = _audio_root() / "drafts" / str(user_id)
        if folder.is_dir() and not folder.is_symlink():
            sessions.update(p.name for p in folder.iterdir() if p.is_dir())
    else:
        # The session folders that existed at the request.
        prefix = f"audio:drafts/{user_id}/"
        sessions.update(p[len(prefix):] for p in _recorded_paths(scope)
                        if p.startswith(prefix) and "/" not in p[len(prefix):])
    return sorted(s for s in sessions if is_storage_id(s))


def _session_chunk_filter(sessions, owned_ids):
    """Transcript chunks of the user's sessions that are not attached to
    another user's node (a session moves only onto its owner's node, so
    this is a guard, not a case)."""
    return and_(NodeTranscriptChunk.session_id.in_(sessions),
                or_(NodeTranscriptChunk.node_id.is_(None),
                    NodeTranscriptChunk.node_id.in_(owned_ids)))


# ── Files ───────────────────────────────────────────────────────────────

def _audio_root():
    from backend.utils import audio_storage
    return pathlib.Path(audio_storage.AUDIO_STORAGE_ROOT)


def _stash_root():
    from backend.utils import twitter_archive
    return pathlib.Path(twitter_archive.STASH_ROOT)


def _node_dirs(plan, node_ids):
    from backend.utils.audio_storage import storage_path
    root = _audio_root()
    dirs = []
    for nid in node_ids:
        for author in sorted(plan.authors.get(nid, ())):
            dirs.append(storage_path(root, "user", author, "node", nid))
            dirs.append(storage_path(root, "nodes", author, nid))
    return dirs


def _user_dirs(user_id):
    from backend.utils.audio_storage import storage_path
    root = _audio_root()
    dirs = [storage_path(root, name, user_id) for name in USER_AUDIO_FOLDERS]
    dirs.append(storage_path(_stash_root(), user_id))
    return dirs


def _x_api_root():
    return _stash_root().parent / "x-api"


def _x_dump_files(handle):
    """X API dumps (backend.tasks.imports.x_api_dump_path) of the handle
    the account was pre-filled from: that account's public posts."""
    if not handle or not _HANDLE_RE.fullmatch(handle):
        return []
    folder = _x_api_root()
    if not folder.is_dir() or folder.is_symlink():
        return []
    pattern = re.compile(re.escape(handle) + _X_DUMP_STAMP, re.IGNORECASE)
    return [p for p in folder.iterdir()
            if p.is_file() and pattern.fullmatch(p.name)]


# Recorded paths ("<root>:<relative path>") of a "Delete all my writing"
# request: what existed in the user's storage when it was made.
_PATH_ROOTS = {"audio": _audio_root, "stash": _stash_root,
               "xapi": _x_api_root}


def _entries(folder):
    folder = pathlib.Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        return []
    return sorted(folder.iterdir())


def paths_at_request(user):
    """The folders and files of the user's storage that exist now, as
    recorded paths: every entry of the user's audio folders (one level
    deeper under ``user/<id>/``, whose ``node``, ``profile`` and ``item``
    folders also hold what is written later), of the import stash, and
    the X API dumps of the handle the account was pre-filled from."""
    from backend.utils.audio_storage import storage_path
    out = []
    audio = _audio_root()
    for name in USER_AUDIO_FOLDERS:
        base = storage_path(audio, name, user.id)
        for entry in _entries(base):
            if name == "user" and entry.is_dir() and not entry.is_symlink():
                out.extend(sub for sub in _entries(entry))
            else:
                out.append(entry)
    rel = [f"audio:{p.relative_to(audio).as_posix()}" for p in out]
    stash = _stash_root()
    rel += [f"stash:{p.relative_to(stash).as_posix()}"
            for p in _entries(storage_path(stash, user.id))]
    rel += [f"xapi:{p.name}" for p in _x_dump_files(user.prefilled_handle)]
    return rel


def _resolve(recorded):
    """The absolute path of a recorded path, or None when it does not
    name something inside its root (never followed outside it)."""
    key, _, rel = recorded.partition(":")
    root_of = _PATH_ROOTS.get(key)
    if root_of is None or not rel or rel.startswith("/"):
        return None
    root = pathlib.Path(root_of())
    path = root / rel
    base = os.path.normpath(str(root))
    if os.path.commonpath([base, os.path.normpath(str(path))]) != base:
        return None
    return path


_ROW_FOLDER_RE = re.compile(
    r"audio:(?:user/\d+/(?P<kind>node|item|profile)/(?P<id>\d+)"
    r"|nodes/\d+/(?P<node>\d+)"
    r"|(?P<sdir>drafts|streaming)/\d+/(?P<sid>[A-Za-z0-9_-]+))$")


def _kept_row_ids(scope):
    """What the purge of *scope* keeps although the request recorded its
    folder: an entry the user re-imported (live again), a reference or
    profile taken out of the hidden set (saved again), and the session
    folders of drafts and nodes it keeps. Their files stay with them."""
    with including_hidden_rows():
        live_nodes = select(Node.id).where(Node.deleted_at.is_(None))
        kept = {}
        for kind, model in (("item", ExternalItem), ("profile", UserProfile)):
            kept[kind] = {i for (i,) in db.session.query(model.id).filter(
                model.user_id == scope.user_id,
                ~model.id.in_(hidden_ids(model.__tablename__,
                                         scope.job_id)))}
        kept["node"] = {i for (i,) in db.session.query(Node.id).filter(
            Node.id.in_(live_nodes),
            or_(Node.user_id == scope.user_id,
                Node.human_owner_id == scope.user_id))}
        sessions = {s for (s,) in db.session.query(Draft.session_id).filter(
            Draft.user_id == scope.user_id, Draft.session_id.isnot(None),
            ~Draft.id.in_(hidden_ids("draft", scope.job_id)))}
        sessions |= {s for (s,) in db.session.query(
            Node.streaming_session_id).filter(
            Node.deleted_at.is_(None), Node.streaming_session_id.isnot(None),
            or_(Node.user_id == scope.user_id,
                Node.human_owner_id == scope.user_id))}
        kept["session"] = sessions
    return kept


def _recorded_paths(scope):
    """The paths a "Delete all my writing" request recorded, less those
    of rows the purge keeps (_kept_row_ids)."""
    if scope.everything:
        return []
    paths = [p for (p,) in db.session.query(UserDataPurgeHidden.path).filter(
        UserDataPurgeHidden.job_id == scope.job_id,
        UserDataPurgeHidden.kind == "file")]
    if not paths:
        return []
    kept = _kept_row_ids(scope)
    out = []
    for path in paths:
        m = _ROW_FOLDER_RE.match(path)
        if m is not None:
            if m.group("kind") and int(m.group("id")) in kept[m.group("kind")]:
                continue
            if m.group("node") and int(m.group("node")) in kept["node"]:
                continue
            if m.group("sid") and m.group("sid") in kept["session"]:
                continue
        out.append(path)
    return out


def _scope_files(scope, user):
    """(dirs, files) of the user's storage the purge deletes, besides
    the node folders: every folder of the user's and the pre-fill's X
    dumps, or, for a "Delete all my writing" request, what existed when
    it was made."""
    if scope.everything:
        return (_user_dirs(scope.user_id),
                _x_dump_files(user.prefilled_handle if user else None))
    dirs, files = [], []
    for recorded in _recorded_paths(scope):
        path = _resolve(recorded)
        if path is None:
            continue
        (dirs if path.is_dir() and not path.is_symlink() else files).append(
            path)
    return dirs, files


def _files_in(dirs):
    """Every file (and symlink, never followed) under *dirs*."""
    out = set()
    for d in dirs:
        d = pathlib.Path(d)
        if d.is_symlink() or not d.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
            for name in filenames:
                out.add(pathlib.Path(dirpath) / name)
            for name in dirnames:
                p = pathlib.Path(dirpath) / name
                if p.is_symlink():
                    out.add(p)
    return out


def _delete_files(dirs, files=()):
    """Delete the files under *dirs* and the *files*, then the emptied
    folders. Returns how many files were deleted. A file that cannot be
    deleted is logged by path (paths hold ids, never content) and the
    purge goes on; the count at the end still finds it, so the run fails
    and the next run tries again."""
    deleted = 0
    for f in sorted(_files_in(dirs) | set(files)):
        try:
            os.unlink(f)
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("user purge: could not delete %s (%s)", f,
                           type(e).__name__)
    for d in dirs:
        d = pathlib.Path(d)
        if d.is_symlink() or not d.is_dir():
            continue
        for dirpath, _, _ in os.walk(d, topdown=False, followlinks=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return deleted


# ── Stop what is in flight ──────────────────────────────────────────────

class InFlight(NamedTuple):
    counts: dict
    running: list       # task ids still executing
    lock_busy: bool     # a batch pipeline (profile, recent context) holds its lock

    @property
    def must_wait(self):
        return bool(self.running) or self.lock_busy


def _task_ids(user_id, plan):
    ids = set()
    node_cols = (
        (Node.llm_task_id, Node.llm_task_status),
        (Node.transcription_task_id, Node.transcription_status),
        (Node.tts_task_id, Node.tts_task_status),
    )
    for chunk in _chunks(plan.parents):
        for id_col, status_col in node_cols:
            ids.update(t for (t,) in db.session.query(id_col).filter(
                Node.id.in_(chunk), status_col.in_(IN_FLIGHT_STATUSES),
                id_col.isnot(None)))
        ids.update(t for (t,) in db.session.query(
            NodeTranscriptChunk.task_id).filter(
            NodeTranscriptChunk.node_id.in_(chunk),
            NodeTranscriptChunk.status.in_(IN_FLIGHT_STATUSES),
            NodeTranscriptChunk.task_id.isnot(None)))
    sessions = _session_ids(user_id, plan)
    for chunk in _chunks(sessions):
        ids.update(t for (t,) in db.session.query(
            NodeTranscriptChunk.task_id).filter(
            NodeTranscriptChunk.session_id.in_(chunk),
            NodeTranscriptChunk.status.in_(IN_FLIGHT_STATUSES),
            NodeTranscriptChunk.task_id.isnot(None)))
    scope = plan._scope(user_id)
    for model in (UserProfile, ExternalItem):
        ids.update(t for (t,) in db.session.query(model.tts_task_id).filter(
            scope.rows(model),
            model.tts_task_status.in_(IN_FLIGHT_STATUSES),
            model.tts_task_id.isnot(None)))
    ids.update(t for (t,) in db.session.query(PollResponse.draft_task_id)
               .filter(scope.rows(PollResponse),
                       PollResponse.status == "drafting",
                       PollResponse.draft_task_id.isnot(None)))
    user = db.session.get(User, user_id)
    if user is not None and user.profile_generation_task_id:
        ids.add(user.profile_generation_task_id)
    return sorted(ids)


def _revoke_tasks(task_ids):
    """Revoke queued tasks so they never start. Without terminate: Celery
    documents that terminate may kill the process after it has moved on
    to another task, which here would be another user's."""
    if not task_ids:
        return
    from backend.celery_app import celery
    celery.control.revoke(list(task_ids))


def _running_tasks(task_ids):
    """The task ids a worker is executing now (state STARTED; the app
    sets task_track_started)."""
    if not task_ids:
        return []
    from backend.celery_app import celery
    return [t for t in task_ids if celery.AsyncResult(t).state == "STARTED"]


def _batch_keys(key_type="chat"):
    from flask import current_app
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.llm_batch import apply_batch_key_override
    return apply_batch_key_override(
        get_api_keys_for_usage(current_app.config, key_type),
        current_app.config)


def _cancel_provider_batch(provider_key, batch_id, key_type="chat"):
    """Best-effort provider cancel of a batch that carries only this
    user's requests. Returns None, or the error class name."""
    from backend.utils import llm_batch
    try:
        keys = _batch_keys(key_type)
        if provider_key == "anthropic":
            llm_batch.anthropic_batch_cancel_one(keys.get("anthropic"), batch_id)
        elif provider_key.startswith("openai"):
            llm_batch.openai_batch_cancel_one(keys.get("openai"), batch_id)
        else:
            return "UnknownProvider"
        return None
    except Exception as e:  # noqa: BLE001 - a cancel never blocks the purge
        logger.warning("user purge: provider cancel of batch %s failed (%s)",
                       batch_id, type(e).__name__)
        return type(e).__name__


@contextmanager
def _profile_batch_lock():
    """The profile batch pipeline's lock, so the poller cannot apply this
    user's result from a job it read before the purge stripped it."""
    from backend.tasks.profile_batch import batch_pipeline_lock
    with batch_pipeline_lock() as ok:
        yield ok


@contextmanager
def _recent_context_batch_lock():
    """The recent-context collector's lock (held from its claim of an
    ended job until the summaries are saved), so it cannot save this
    user's summary from a job it read before the purge stripped it."""
    from backend.tasks.recent_context import recent_context_batch_lock
    with recent_context_batch_lock() as ok:
        yield ok


def _node_batch_entries(plan):
    """Live provider batches of the user's Reads ("_batch" entries in
    tool_calls_meta, one request each)."""
    out = []
    for chunk in _chunks(plan.parents):
        for nid, meta in db.session.query(Node.id, Node.tool_calls_meta).filter(
                Node.id.in_(chunk), Node.llm_task_status.in_(IN_FLIGHT_STATUSES),
                Node.tool_calls_meta.like('%"_batch"%')):
            try:
                entries = json.loads(meta) or []
            except (TypeError, ValueError):
                continue
            for m in entries:
                if (isinstance(m, dict) and m.get("name") == "_batch"
                        and m.get("status") in _LIVE_BATCH_STATUSES
                        and m.get("batch_id")):
                    out.append(m)
    return out


def _strip_batch_jobs(model, belongs, dry_run, now, counts, key):
    """Take the user's items out of a batch job table. A pending job that
    carried only theirs is cancelled at the provider and marked
    cancelled; a finished job that carried only theirs is deleted; any
    other job keeps the other users' items."""
    for job in model.query.order_by(model.id).all():
        items = list(job.items or [])
        mine = [i for i in items if isinstance(i, dict) and belongs(i)]
        if not mine:
            continue
        counts[key] += 1
        others = [i for i in items if not (isinstance(i, dict) and belongs(i))]
        if job.status == "pending" and not others:
            counts["provider_batches_cancelled"] += 1
            if not dry_run:
                _cancel_provider_batch(job.provider_key, job.batch_id)
                job.status = "cancelled"
                job.collected_at = now
                job.items = []
        elif dry_run:
            continue
        elif job.status != "pending" and not others:
            db.session.delete(job)
        else:
            job.items = others


PIPELINE_FLAG_RESETS = {
    "profile_batch_pending": False,
    "profile_needs_full_regen": False,
    "profile_batch_attempts": 0,
    "profile_force_batch": False,
    "profile_generation_task_id": None,
    "profile_generation_task_dispatched_at": None,
    "profile_seed_error": None,
    "profile_token_ratio": None,
    "profile_token_ratio_family": None,
}


def stop_in_flight(user_id, *, dry_run=False, plan=None, scope=None):
    """Stop everything that could write the user's data again. Returns
    InFlight: counts, the task ids still executing (the runner waits for
    them) and whether a batch pipeline's lock was busy."""
    with including_hidden_rows():
        return _stop_in_flight(user_id, dry_run, plan, scope)


def _stop_in_flight(user_id, dry_run, plan, scope):
    plan = plan or plan_nodes(user_id, scope)
    scope = plan._scope(user_id)
    counts = Counter()
    now = _now()
    task_ids = _task_ids(user_id, plan)
    counts["celery_tasks_revoked"] = len(task_ids)
    node_batches = _node_batch_entries(plan)
    counts["provider_batches_cancelled"] += len(node_batches)
    response_ids = {r for (r,) in db.session.query(PollResponse.id).filter(
        scope.rows(PollResponse))}

    if dry_run:
        for model, belongs, key in _batch_tables(user_id, response_ids):
            _strip_batch_jobs(model, belongs, True, now, counts, key)
        return InFlight(dict(counts), [], False)

    # The profile task's own guard stays until the purge has run: it is
    # how the next look finds the task still running (the task clears
    # it when it ends; the purge clears it at the end).
    user = db.session.get(User, user_id)
    for attr, value in PIPELINE_FLAG_RESETS.items():
        if attr not in ("profile_generation_task_id",
                        "profile_generation_task_dispatched_at"):
            setattr(user, attr, value)
    db.session.commit()

    revoke_failed = False
    try:
        _revoke_tasks(task_ids)
    except Exception as e:  # noqa: BLE001 - recorded below: the runner waits
        logger.warning("user purge: revoke failed (%s); waiting to revoke "
                       "again", type(e).__name__)
        revoke_failed = True
    for entry in node_batches:
        _cancel_provider_batch(entry.get("provider") or "anthropic",
                               entry["batch_id"],
                               entry.get("key_type") or "chat")

    lock_busy = False
    # The pipelines whose collectors save per-user results under a lock.
    locks = {ProfileBatchJob: _profile_batch_lock,
             RecentContextBatchJob: _recent_context_batch_lock}
    for model, belongs, key in _batch_tables(user_id, response_ids):
        lock = locks.get(model)
        if lock is None:
            _strip_batch_jobs(model, belongs, False, now, counts, key)
            db.session.commit()
            continue
        with lock() as ok:
            if not ok:
                lock_busy = True
                continue
            _strip_batch_jobs(model, belongs, False, now, counts, key)
            db.session.commit()

    try:
        running = _running_tasks(task_ids)
    except Exception as e:  # noqa: BLE001
        # Unknown is not "none running": wait and look again.
        logger.warning("user purge: task state check failed (%s); treating "
                       "the user's tasks as running", type(e).__name__)
        running = list(task_ids)
    if revoke_failed:
        # A queued task may still start and write: wait, then revoke again.
        running = list(task_ids)
    return InFlight(dict(counts), running, lock_busy)


def _batch_tables(user_id, response_ids):
    return (
        (ProfileBatchJob, lambda i: i.get("user_id") == user_id,
         "profile_batch_job"),
        (PollDraftBatchJob, lambda i: i.get("response_id") in response_ids,
         "poll_draft_batch_job"),
        (ExternalDigestBatchJob, lambda i: i.get("user_id") == user_id,
         "external_digest_batch_job"),
        (RecentContextBatchJob, lambda i: i.get("user_id") == user_id,
         "recent_context_batch_job"),
    )


# ── Counting (the dry run) ──────────────────────────────────────────────

# Tables deleted by user_id alone.
_USER_TABLES = (
    (ExternalAccount, "external_account"),
    (UserRecentContext, "user_recent_context"),
    (UserProfile, "user_profile"),
    (UserTodo, "user_todo"),
    (UserArtifact, "user_artifact"),
    (ArtifactView, "artifact_view"),
    (UserPrompt, "user_prompt"),
    (UserFeedback, "user_feedback"),
    (UserNotification, "user_notification"),
    (PollResponse, "poll_response"),
)

# Columns of other users' rows that point at a deleted node and are set
# to null (the foreign key requires it).
_REF_COLUMNS = (
    (Draft, Draft.node_id, "draft.node_id"),
    (Draft, Draft.parent_id, "draft.parent_id"),
    (Draft, Draft.llm_node_id, "draft.llm_node_id"),
    (ShareDraft, ShareDraft.source_node_id, "share_draft.source_node_id"),
    (ShareDraft, ShareDraft.public_node_id, "share_draft.public_node_id"),
)


def _count(model, *conds):
    return db.session.query(func.count()).select_from(model).filter(
        *conds).scalar() or 0


def count_user_data(user_id, plan=None, scope=None):
    """What a purge of *user_id* would delete, change or keep, per table.
    Changes nothing."""
    with including_hidden_rows():
        return _count_user_data(user_id, plan, scope)


def _by_user(scope, model, col):
    """Rows of a derived table found by the user's id: all of them when
    the purge takes everything, none otherwise (they are found through
    the rows they hang off)."""
    return col == scope.user_id if scope.everything else false()


def _count_user_data(user_id, plan, scope):
    plan = plan or plan_nodes(user_id, scope)
    scope = plan._scope(user_id)
    U = user_id
    owned_ids = plan.owned_select(U)
    deleted_ids = plan.deleted_select(U)
    item_ids = select(ExternalItem.id).where(scope.rows(ExternalItem))
    profile_ids = select(UserProfile.id).where(scope.rows(UserProfile))
    sessions = _session_ids(U, plan)

    c = {
        "node": len(plan.deleted),
        "node_tombstoned": len(plan.keep),
        "others_replies_kept": plan.others_under,
        "node_version": _count(NodeVersion, NodeVersion.node_id.in_(owned_ids)),
        "node_transcript_chunk": _count(NodeTranscriptChunk, or_(
            NodeTranscriptChunk.node_id.in_(owned_ids),
            _session_chunk_filter(sessions, owned_ids))),
        "tts_chunk": _count(TTSChunk, or_(
            TTSChunk.node_id.in_(owned_ids),
            TTSChunk.profile_id.in_(profile_ids),
            TTSChunk.item_id.in_(item_ids))),
        "node_context_artifact": _count(
            NodeContextArtifact, NodeContextArtifact.node_id.in_(owned_ids)),
        "node_embedding": _count(NodeEmbedding, or_(
            NodeEmbedding.node_id.in_(owned_ids),
            _by_user(scope, NodeEmbedding, NodeEmbedding.user_id))),
        "thread": _count(Thread, Thread.root_node_id.in_(owned_ids)),
        "feed_pick": _count(FeedPick, or_(
            FeedPick.node_id.in_(owned_ids), scope.rows(FeedPick),
            FeedPick.external_item_id.in_(item_ids))),
        "feed_render": _count(FeedRender, FeedRender.node_id.in_(owned_ids)),
        "reference_action": _count(ReferenceAction, or_(
            ReferenceAction.node_id.in_(owned_ids),
            scope.rows(ReferenceAction),
            ReferenceAction.item_id.in_(item_ids))),
        "draft": _count(Draft, scope.rows(Draft)),
        "share_draft": _count(ShareDraft, scope.rows(ShareDraft)),
        "external_item": _count(ExternalItem, scope.rows(ExternalItem)),
        "external_item_embedding": _count(ExternalItemEmbedding, or_(
            ExternalItemEmbedding.item_id.in_(item_ids),
            _by_user(scope, ExternalItemEmbedding,
                     ExternalItemEmbedding.user_id))),
        "api_cost_log": _count(APICostLog, scope.cost_rows()),
    }
    for model, col, key in _REF_COLUMNS:
        c[key] = _count(model, col.in_(deleted_ids), scope.other_rows(model))
    for col, key in ((Node.linked_node_id, "node.linked_node_id"),
                     (Node.continuation_node_id, "node.continuation_node_id")):
        c[key] = _count(Node, col.in_(deleted_ids), plan.not_owned(U))
    for model, key in _USER_TABLES:
        c[key] = _count(model, scope.rows(model))

    user = db.session.get(User, U)
    dirs, extra_files = _scope_files(scope, user)
    files = _files_in(_node_dirs(plan, plan.parents) + dirs)
    files |= {f for f in extra_files if os.path.lexists(f)}
    c["files"] = len(files)
    return c


def leftovers(counts):
    """The part of *counts* the purge should have brought to zero."""
    return {k: v for k, v in counts.items() if v and k not in INFO_KEYS}


def batch_items_left(user_id):
    """Batch jobs that still carry the user's items, per table (the dry
    run of the strip). Poll-draft items are keyed by the user's poll
    responses, so they are found only while those rows exist."""
    with including_hidden_rows():
        response_ids = {r for (r,) in db.session.query(
            PollResponse.id).filter(PollResponse.user_id == user_id)}
    counts = Counter()
    for model, belongs, key in _batch_tables(user_id, response_ids):
        _strip_batch_jobs(model, belongs, True, _now(), counts, key)
    counts.pop("provider_batches_cancelled", None)
    return {k: v for k, v in counts.items() if v}


# ── The purge ───────────────────────────────────────────────────────────

def _delete(model, *conds):
    return model.query.filter(*conds).delete(synchronize_session=False)


def _delete_node_rows_dependents(ids, counts):
    """Rows that hang off the nodes *ids* (all of them the user's)."""
    for model, col, key in (
            (NodeVersion, NodeVersion.node_id, "node_version"),
            (NodeTranscriptChunk, NodeTranscriptChunk.node_id,
             "node_transcript_chunk"),
            (TTSChunk, TTSChunk.node_id, "tts_chunk"),
            (NodeContextArtifact, NodeContextArtifact.node_id,
             "node_context_artifact"),
            (NodeEmbedding, NodeEmbedding.node_id, "node_embedding"),
            (Thread, Thread.root_node_id, "thread"),
            (FeedPick, FeedPick.node_id, "feed_pick"),
            (FeedRender, FeedRender.node_id, "feed_render"),
            (ReferenceAction, ReferenceAction.node_id, "reference_action"),
    ):
        counts[key] += _delete(model, col.in_(ids))


def _clear_references_to(ids, plan, counts):
    """Before the nodes *ids* are deleted: set to null every column that
    points at them. Only that column changes on another user's row (or a
    row of the user's the purge keeps), and its updated_at is kept (a
    bumped timestamp would look like an edit, and would send a reply back
    through the embedding sweep)."""
    scope = plan.scope
    for model, col, key in _REF_COLUMNS:
        counts[key] += model.query.filter(
            col.in_(ids), scope.other_rows(model),
        ).update({col: None, model.updated_at: model.updated_at},
                 synchronize_session=False)
        # The user's own rows written since the first step of the round.
        table_key = "draft" if model is Draft else "share_draft"
        counts[table_key] += _delete(model, col.in_(ids), scope.rows(model))
    for col, key in ((Node.linked_node_id, "node.linked_node_id"),
                     (Node.continuation_node_id, "node.continuation_node_id")):
        counts[key] += Node.query.filter(
            col.in_(ids), scope.not_nodes(plan.extras),
        ).update({col: None, Node.updated_at: Node.updated_at},
                 synchronize_session=False)
        Node.query.filter(col.in_(ids), scope.nodes(plan.extras)).update(
            {col: None, Node.updated_at: Node.updated_at},
            synchronize_session=False)


def _tombstone(ids, now):
    """Keep the rows (another user's reply hangs below) but nothing of
    what the user wrote or recorded."""
    return Node.query.filter(Node.id.in_(ids)).update({
        Node.content: None,
        Node.streaming_content: None,
        Node.tool_calls_meta: None,
        Node.public_slug: None,
        Node.source_key: None,
        Node.audio_original_url: None,
        Node.audio_tts_url: None,
        Node.audio_duration_sec: None,
        Node.audio_mime_type: None,
        Node.transcription_error: None,
        Node.llm_task_error: None,
        Node.llm_task_warnings: None,
        Node.streaming_session_id: None,
        Node.pinned_at: None,
        Node.pinned_by: None,
        Node.token_count: 0,
        Node.deleted_at: func.coalesce(Node.deleted_at, now),
    }, synchronize_session=False)


def _revoke_x_access(user_id):
    """Revoke the user's stored X tokens at X, before the purge deletes
    them, so X stops listing Loore as an app with access to the account.

    Never raises and never holds the purge up: a call X refuses, one that
    times out (X_REVOKE_TIMEOUT_SECONDS) or a token that cannot be read is
    logged without the token, and the purge deletes the connection
    anyway. The user was told how to remove Loore on X themselves if it
    is still listed. Logged as an error (Sentry), except for a connection
    X had already refused (``revoked_at``), where a refusal is expected.

    The tokens are decrypted here, one connection at a time, to send them
    to X: they are credentials Loore holds, not the user's writing.
    An access token past its expiry grants nothing and is not sent.
    Returns how many tokens X confirmed revoked."""
    from flask import current_app
    from backend.utils.external_content import x_revoke_token

    accounts = ExternalAccount.query.filter(
        ExternalAccount.user_id == user_id).order_by(ExternalAccount.id).all()
    if not accounts:
        return 0
    client_id = current_app.config.get("X_CLIENT_ID")
    if not client_id:
        logger.warning("user purge of user %s: X is not configured here, "
                       "so the stored X connection is deleted without "
                       "revoking it at X", user_id)
        return 0
    client_secret = current_app.config.get("X_CLIENT_SECRET")
    now = _now()
    revoked = 0
    for account in accounts:
        if account.provider != "twitter":
            continue
        expected = account.revoked_at is not None
        kinds = []
        if account.access_token and not (
                account.token_expires_at and account.token_expires_at <= now):
            kinds.append(("access", account.get_access_token))
        if account.refresh_token:
            kinds.append(("refresh", account.get_refresh_token))
        for kind, read in kinds:
            try:
                token = read()
                if not token:
                    continue
                x_revoke_token(token, client_id, client_secret,
                               timeout=X_REVOKE_TIMEOUT_SECONDS)
                revoked += 1
            except Exception as e:  # noqa: BLE001 - never stops the purge
                response = getattr(e, "response", None)
                status = getattr(response, "status_code", None)
                (logger.warning if expected else logger.error)(
                    "user purge of user %s: X did not revoke the stored X "
                    "%s token (%s%s); the connection is deleted anyway",
                    user_id, kind, type(e).__name__,
                    f", HTTP {status}" if status else "")
    if revoked:
        logger.info("user purge of user %s: %d stored X token(s) revoked "
                    "at X", user_id, revoked)
    return revoked


def _beat(heartbeat):
    if heartbeat is not None:
        heartbeat()


def _purge_round(user, plan, counts, heartbeat):
    U = user.id
    scope = plan.scope
    owned_ids = plan.owned_select(U)

    # 1. Rows of the user's that point at nodes, so the nodes can go.
    # Sessions are read before the drafts that name them are deleted.
    sessions = _session_ids(U, plan)
    for chunk in _chunks(sessions):
        counts["node_transcript_chunk"] += _delete(
            NodeTranscriptChunk, _session_chunk_filter(chunk, owned_ids))
    _revoke_x_access(U)
    counts["external_account"] += _delete(ExternalAccount,
                                          ExternalAccount.user_id == U)
    counts["draft"] += _delete(Draft, scope.rows(Draft))
    counts["share_draft"] += _delete(ShareDraft, scope.rows(ShareDraft))
    counts["feed_pick"] += _delete(FeedPick, scope.rows(FeedPick))
    counts["reference_action"] += _delete(ReferenceAction,
                                          scope.rows(ReferenceAction))
    if scope.everything:
        counts["node_embedding"] += _delete(NodeEmbedding,
                                            NodeEmbedding.user_id == U)
        counts["external_item_embedding"] += _delete(
            ExternalItemEmbedding, ExternalItemEmbedding.user_id == U)
    db.session.commit()
    _beat(heartbeat)

    # 2. Nodes, deepest first. Files first: a crash between the two
    # leaves rows whose folders the next run finds again.
    for chunk in _chunks(plan.deleted):
        counts["files"] += _delete_files(_node_dirs(plan, chunk))
        _delete_node_rows_dependents(chunk, counts)
        _clear_references_to(chunk, plan, counts)
        counts["node"] += _delete(Node, Node.id.in_(chunk))
        db.session.commit()
        _beat(heartbeat)

    # 3. Tombstones.
    now = _now()
    tombstoned = 0
    for chunk in _chunks(sorted(plan.keep)):
        counts["files"] += _delete_files(_node_dirs(plan, chunk))
        _delete_node_rows_dependents(chunk, counts)
        tombstoned += _tombstone(chunk, now)
        db.session.commit()
        _beat(heartbeat)
    counts["node_tombstoned"] = tombstoned
    counts["others_replies_kept"] = plan.others_under

    # 4. Saved references.
    item_ids = [i for (i,) in db.session.query(ExternalItem.id).filter(
        scope.rows(ExternalItem)).order_by(ExternalItem.id)]
    for chunk in _chunks(item_ids):
        counts["tts_chunk"] += _delete(TTSChunk, TTSChunk.item_id.in_(chunk))
        counts["feed_pick"] += _delete(FeedPick,
                                       FeedPick.external_item_id.in_(chunk))
        counts["reference_action"] += _delete(
            ReferenceAction, ReferenceAction.item_id.in_(chunk))
        counts["external_item_embedding"] += _delete(
            ExternalItemEmbedding, ExternalItemEmbedding.item_id.in_(chunk))
        counts["external_item"] += _delete(ExternalItem,
                                           ExternalItem.id.in_(chunk))
        db.session.commit()
        _beat(heartbeat)

    # 5. Profiles and the rest of the user's own tables.
    profile_ids = [p for (p,) in db.session.query(UserProfile.id).filter(
        scope.rows(UserProfile))]
    for chunk in _chunks(profile_ids):
        counts["tts_chunk"] += _delete(TTSChunk, TTSChunk.profile_id.in_(chunk))
        UserProfile.query.filter(UserProfile.id.in_(chunk)).update(
            {UserProfile.parent_profile_id: None}, synchronize_session=False)
        # A version the purge keeps (written after the request) may name
        # a deleted one as its base.
        UserProfile.query.filter(
            UserProfile.parent_profile_id.in_(chunk)).update(
            {UserProfile.parent_profile_id: None}, synchronize_session=False)
        UserRecentContext.query.filter(
            UserRecentContext.profile_id.in_(chunk),
            scope.other_rows(UserRecentContext)).update(
            {UserRecentContext.profile_id: None}, synchronize_session=False)
    for model, key in _USER_TABLES:
        if model is ExternalAccount:
            continue
        counts[key] += _delete(model, scope.rows(model))
    db.session.commit()
    _beat(heartbeat)

    # 6. Cost rows stay, detached from the person.
    from backend.utils.system_accounts import get_erased_system_user
    counts["api_cost_log"] += 0   # reported even when there is none
    if db.session.query(APICostLog.id).filter(
            scope.cost_rows()).first() is not None:
        erased = get_erased_system_user()
        counts["api_cost_log"] += APICostLog.query.filter(
            scope.cost_rows(),
        ).update({
            APICostLog.user_id: erased.id,
            APICostLog.provider_response_id: None,
            APICostLog.request_ref: None,
            APICostLog.system_prefix_hash: None,
        }, synchronize_session=False)
        db.session.commit()

    # 7. The user's folders and pre-fill dumps (for a "Delete all my
    # writing" request: those that existed when it was made).
    dirs, files = _scope_files(scope, user)
    counts["files"] += _delete_files(dirs, files)
    _beat(heartbeat)


def _drop_public_pages(user):
    """Drop the cached public pages the user's writing appears on (their
    profile, feed, permalinks and the threads they replied in). Before
    the purge, while the slugs still name the pages; after it, for a page
    rendered meanwhile. A cache failure never stops the purge (the TTL
    bounds it)."""
    try:
        from backend.utils.public_cache import invalidate_for_user
        invalidate_for_user(user)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        logger.warning("user purge of user %s: public page cache not "
                       "dropped (%s)", user.id, type(e).__name__)


def purge_user_content(user_id, *, dry_run=False, heartbeat=None,
                       scope=None):
    """Delete all of *user_id*'s data (see the module docstring), or with
    *dry_run* count it; with a job's *scope*, what that "Delete all my
    writing" request hid. Returns counts per table. Raises PurgeRefused
    for an AI or system account. *heartbeat* is called after every commit
    and may raise PurgeSuperseded to stop."""
    with including_hidden_rows():
        return _purge_user_content(user_id, dry_run, heartbeat,
                                   scope or Scope(user_id))


def _purge_user_content(user_id, dry_run, heartbeat, scope):
    user = db.session.get(User, user_id)
    reason = purge_refusal(user)
    if reason:
        raise PurgeRefused(reason)
    if dry_run:
        plan = plan_nodes(user_id, scope)
        counts = count_user_data(user_id, plan)
        counts.update(stop_in_flight(user_id, dry_run=True, plan=plan).counts)
        return counts

    _drop_public_pages(user)
    counts = Counter()
    for _ in range(PURGE_MAX_ROUNDS):
        plan = plan_nodes(user_id, scope)
        _purge_round(user, plan, counts, heartbeat)
        left = leftovers(count_user_data(user_id, scope=scope))
        if not left:
            break
        logger.warning("user purge of user %s: rows written meanwhile (%s); "
                       "another round", user_id, sorted(left))
    else:
        raise PurgeIncomplete(
            f"still left after {PURGE_MAX_ROUNDS} rounds: "
            + ", ".join(f"{k}={v}" for k, v in sorted(left.items())))

    # The account stays; what described the writing does not. A
    # description the user wrote after a "Delete all my writing" request
    # is theirs and stays.
    user = db.session.get(User, user_id)
    for attr, value in PIPELINE_FLAG_RESETS.items():
        setattr(user, attr, value)
    user.prefilled_handle = None
    if scope.everything or db.session.query(UserDataPurgeHidden.id).filter(
            UserDataPurgeHidden.job_id == scope.job_id,
            UserDataPurgeHidden.kind == "user_description").first():
        user.description = ""
    db.session.commit()
    _drop_public_pages(user)
    return dict(counts)


# ── Jobs: schedule, cancel, claim, run ──────────────────────────────────

def active_job(user_id):
    return UserDataPurge.query.filter(
        UserDataPurge.user_id == user_id,
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES),
    ).order_by(UserDataPurge.id.desc()).first()


def schedule_purge(user, *, requested_by_id, source, at=None):
    """Create the user's purge job, due at *at* (default: after the grace
    period). Returns (job, created); an active job is returned as it is.
    Raises PurgeRefused for an AI or system account.

    The user's own request ("self") hides at once everything it will
    delete (hide_writing), in the same transaction as the job: either
    both are there or neither."""
    reason = purge_refusal(user)
    if reason:
        raise PurgeRefused(reason)
    # One active job per user: the user row lock serialises two requests.
    db.session.query(User).filter(User.id == user.id).with_for_update().one()
    job = active_job(user.id)
    if job is not None:
        db.session.commit()
        return job, False
    now = _now()
    hides = source == "self"
    job = UserDataPurge(
        user_id=user.id, source=source, requested_by_id=requested_by_id,
        status="scheduled", requested_at=now,
        scope="hidden" if hides else "all",
        scheduled_for=at or now + timedelta(days=PURGE_GRACE_DAYS))
    db.session.add(job)
    if hides:
        db.session.flush()
        hide_writing(user, job, now)
    db.session.commit()
    if hides:
        _after_hiding(user, job)
    return job, True


def _record(job_id, kind, model, *conds):
    """Record the ids of *model*'s rows matching *conds* as hidden by the
    job (one INSERT ... SELECT)."""
    from sqlalchemy import insert, literal
    sel = select(literal(job_id), literal(kind), model.id).where(*conds)
    db.session.execute(insert(UserDataPurgeHidden).from_select(
        ["job_id", "kind", "row_id"], sel))


def hide_writing(user, job, now):
    """Hide at once everything the purge of *job* will delete (Peter,
    2026-10-09), and record it (UserDataPurgeHidden), so a restore brings
    back exactly that and the purge deletes exactly that:

    * the user's live nodes (their entries, recordings, imports and the AI
      replies they asked for, legacy ones included) are soft-deleted, so
      the user and everyone else see them as deleted, and other people's
      replies below them stay as under any deleted entry; nodes the user
      had deleted before are recorded apart and never restored;
    * the rows of the per-user tables (HIDDEN_ROW_TABLES) are left out of
      every query from now on;
    * the short profile description is shown empty;
    * the folders and files in the user's storage are recorded, so the
      purge deletes those and not what the user records afterwards.

    Runs in the caller's transaction and commits nothing. Ids, paths and
    timestamps only: nothing is decrypted."""
    U = user.id
    with including_hidden_rows():
        owned = _owned(U, _legacy_ai_reply_ids(U))
        _record(job.id, "node", Node, owned, Node.deleted_at.is_(None))
        _record(job.id, "node_deleted", Node, owned,
                Node.deleted_at.isnot(None))
        Node.query.filter(Node.id.in_(hidden_ids("node", job.id))).update(
            {Node.deleted_at: now, Node.updated_at: Node.updated_at},
            synchronize_session=False)
        for model in HIDDEN_ROW_TABLES:
            _record(job.id, model.__tablename__, model, model.user_id == U)
        if user.description:
            db.session.add(UserDataPurgeHidden(
                job_id=job.id, kind="user_description", row_id=U))
        for path in paths_at_request(user):
            db.session.add(UserDataPurgeHidden(
                job_id=job.id, kind="file", path=path))
        db.session.flush()


def _after_hiding(user, job):
    """After the hide has committed: drop the cached public pages.

    Nothing in flight is stopped here, so a restore finds everything as
    it was. What is running finishes against hidden rows: an AI reply
    whose node was soft-deleted discards its text (the completion task's
    own check), and every job that would save something new from the
    writing skips a user whose writing is on hold (profile, recent
    context, digest, intentions, embeddings, TTS, bookmark sync;
    hidden_rows.writing_on_hold). The purge stops what is still in
    flight when it starts, as before."""
    _drop_public_pages(user)


def hidden_counts(job_id):
    """{kind: count} of what a job hid (ids only), for the logs."""
    return dict(db.session.query(
        UserDataPurgeHidden.kind, func.count(UserDataPurgeHidden.id)
    ).filter(UserDataPurgeHidden.job_id == job_id).group_by(
        UserDataPurgeHidden.kind).all())


def unhide_writing(job_ids):
    """Show again what the jobs hid: the nodes they soft-deleted get
    ``deleted_at`` cleared (never the ones the user had deleted before),
    and the records go, so the hidden rows and the description show
    again. In the caller's transaction (nothing commits here), so a
    restore that fails half way changes nothing."""
    job_ids = list(job_ids)
    if not job_ids:
        return
    node_ids = select(UserDataPurgeHidden.row_id).where(
        UserDataPurgeHidden.job_id.in_(job_ids),
        UserDataPurgeHidden.kind == "node")
    Node.query.filter(Node.id.in_(node_ids),
                      Node.deleted_at.isnot(None)).update(
        {Node.deleted_at: None, Node.updated_at: Node.updated_at},
        synchronize_session=False)
    # A poll answer whose AI draft was in flight when it was hidden got no
    # draft (the collector and the submit skip a hidden answer): it comes
    # back where the user can write it or ask for a draft again.
    poll_ids = select(UserDataPurgeHidden.row_id).where(
        UserDataPurgeHidden.job_id.in_(job_ids),
        UserDataPurgeHidden.kind == "poll_response")
    PollResponse.query.filter(
        PollResponse.id.in_(poll_ids), PollResponse.status == "drafting",
        PollResponse.content.is_(None),
    ).update({PollResponse.status: "draft_failed"},
             synchronize_session=False)
    UserDataPurgeHidden.query.filter(
        UserDataPurgeHidden.job_id.in_(job_ids)).delete(
        synchronize_session=False)


def forget_hidden(job_id):
    """After the purge: the records of what the job hid (the rows are
    gone)."""
    UserDataPurgeHidden.query.filter(
        UserDataPurgeHidden.job_id == job_id).delete(
        synchronize_session=False)


def cancel_purge(user_id, cancelled_by_id):
    """Cancel the user's own "Delete all my writing" while it waits, and
    restore what it hid, in one transaction ("Restore my writing").
    Returns True when a job was cancelled (False: none waiting, it has
    started, or it is an admin's purge, which is never undone). The
    cancel is a conditional update on the job's status, so a restore and
    the beat's claim have one winner."""
    cancelled = cancel_jobs(UserDataPurge.user_id == user_id,
                            UserDataPurge.scope == "hidden",
                            cancelled_by_id=cancelled_by_id)
    db.session.commit()
    if cancelled:
        user = db.session.get(User, user_id)
        if user is not None:
            _drop_public_pages(user)
    return bool(cancelled)


def cancel_jobs(*conds, cancelled_by_id):
    """Cancel the scheduled jobs matching *conds* and restore what each
    hid, in the caller's transaction (nothing commits here). Each job is
    a conditional update on its status, so a cancel and the beat's claim
    have one winner, and only a job this call cancelled is restored.
    Returns the cancelled job ids."""
    now = _now()
    cancelled = []
    for (job_id,) in db.session.query(UserDataPurge.id).filter(
            UserDataPurge.status == "scheduled", *conds).all():
        n = UserDataPurge.query.filter(
            UserDataPurge.id == job_id,
            UserDataPurge.status == "scheduled",
        ).update({UserDataPurge.status: "cancelled",
                  UserDataPurge.cancelled_at: now,
                  UserDataPurge.cancelled_by_id: cancelled_by_id},
                 synchronize_session=False)
        if n:
            cancelled.append(job_id)
    unhide_writing(cancelled)
    return cancelled


def _claimable(now):
    stale = now - PURGE_STALE_AFTER
    return or_(
        and_(UserDataPurge.status == "scheduled",
             UserDataPurge.scheduled_for <= now),
        and_(UserDataPurge.status == "running",
             or_(UserDataPurge.heartbeat_at.is_(None),
                 UserDataPurge.heartbeat_at < stale)),
    )


def claim_job(job_id, token, now=None):
    """Take the job for the runner *token*, if it is due or its last
    runner went quiet. Atomic: of two claims, one wins.

    A claim is not an attempt: the runner may wait in the Celery queue
    behind long jobs, and a claim whose runner never started is simply
    claimed again by a later beat. The attempt is counted when the
    runner starts (start_runner)."""
    now = now or _now()
    n = UserDataPurge.query.filter(
        UserDataPurge.id == job_id, _claimable(now),
    ).update({
        UserDataPurge.status: "running",
        UserDataPurge.task_id: token,
        UserDataPurge.heartbeat_at: now,
        UserDataPurge.runner_started_at: None,
    }, synchronize_session=False)
    db.session.commit()
    return n == 1


def start_runner(job_id, token, now=None):
    """The runner *token* starts (or resumes after a wait): True when
    it still holds the job. The first start under a claim counts one
    attempt; a resume after waiting for the user's tasks does not."""
    now = now or _now()
    n = UserDataPurge.query.filter(
        UserDataPurge.id == job_id, UserDataPurge.task_id == token,
        UserDataPurge.status == "running",
    ).update({
        UserDataPurge.attempts: UserDataPurge.attempts + case(
            (UserDataPurge.runner_started_at.is_(None), 1), else_=0),
        UserDataPurge.runner_started_at: func.coalesce(
            UserDataPurge.runner_started_at, now),
        UserDataPurge.started_at: func.coalesce(UserDataPurge.started_at, now),
        UserDataPurge.heartbeat_at: now,
    }, synchronize_session=False)
    db.session.commit()
    return n == 1


def _fail_job(job, error):
    job.status = "failed"
    job.finished_at = _now()
    job.error = (error or "")[:255]
    db.session.commit()
    logger.error("user data purge job %s (user %s) failed: %s",
                 job.id, job.user_id, job.error)


def dispatch_due_jobs(dispatch, now=None):
    """Beat: claim every due job (end of grace, admin purge, a runner that
    went quiet) and hand it to *dispatch(job_id, token)*. A job that has
    used up PURGE_MAX_ATTEMPTS is marked failed instead. Returns the ids
    dispatched."""
    now = now or _now()
    dispatched = []
    for job in UserDataPurge.query.filter(_claimable(now)).order_by(
            UserDataPurge.id).all():
        if job.status == "running" and (job.attempts or 0) >= PURGE_MAX_ATTEMPTS:
            _fail_job(job, job.error or "runner stopped without finishing")
            continue
        if job.status == "running" and job.runner_started_at is None:
            # Claimed, but no runner has started: the queue is backed up
            # or the message was lost. Claim again; not an attempt.
            overdue = now - job.scheduled_for
            log = logger.error if overdue > PURGE_START_OVERDUE else logger.warning
            log("user purge job %s (user %s): no runner started since the "
                "last claim; due %s ago; dispatching again", job.id,
                job.user_id, overdue)
        token = str(uuid.uuid4())
        if not claim_job(job.id, token, now):
            continue
        try:
            dispatch(job.id, token)
            dispatched.append(job.id)
        except Exception as e:  # noqa: BLE001 - the next beat claims it again
            logger.warning("user purge job %s: dispatch failed (%s)",
                           job.id, type(e).__name__)
    return dispatched


def start_job_now(job, dispatch):
    """Claim and dispatch *job* at once (admin purge). False when another
    runner holds it."""
    token = str(uuid.uuid4())
    if not claim_job(job.id, token):
        return False
    dispatch(job.id, token)
    return True


def _heartbeat_for(job_id, token):
    def beat():
        n = UserDataPurge.query.filter(
            UserDataPurge.id == job_id, UserDataPurge.task_id == token,
            UserDataPurge.status == "running",
        ).update({UserDataPurge.heartbeat_at: _now()},
                 synchronize_session=False)
        db.session.commit()
        if not n:
            raise PurgeSuperseded()
    return beat


def run_purge_job(job_id, token):
    """Run the claimed job. Returns "done", "wait" (call again after
    PURGE_WAIT_RETRY_SECONDS: the user's tasks are still running),
    "superseded", "refused" or "error"."""
    if not start_runner(job_id, token):
        return "superseded"
    job = db.session.get(UserDataPurge, job_id)
    user = db.session.get(User, job.user_id)
    if user is None:
        job.status = "done"
        job.finished_at = _now()
        job.counts = {}
        job.error = "account no longer exists; nothing to purge"
        db.session.commit()
        return "done"
    reason = purge_refusal(user)
    if reason:
        _fail_job(job, f"refused: {reason}")
        return "refused"

    heartbeat = _heartbeat_for(job_id, token)
    scope = scope_of(job)
    try:
        inflight = stop_in_flight(user.id, scope=scope)
        job = db.session.get(UserDataPurge, job_id)
        now = _now()
        if inflight.must_wait:
            job.waiting_since = job.waiting_since or now
            if now - job.waiting_since < PURGE_MAX_WAIT:
                # Ahead of now, so the beat does not take it for stale.
                job.heartbeat_at = now + timedelta(
                    seconds=PURGE_WAIT_RETRY_SECONDS)
                db.session.commit()
                logger.info("user purge job %s: waiting for %d running "
                            "task(s)%s", job_id, len(inflight.running),
                            ", a batch pipeline lock" if inflight.lock_busy else "")
                return "wait"
            logger.warning("user purge job %s: tasks still running after "
                           "%s; purging anyway", job_id, PURGE_MAX_WAIT)
        heartbeat()
        counts = purge_user_content(user.id, heartbeat=heartbeat,
                                    scope=scope)
        # A batch job that still carries the user's item (its pipeline's
        # lock stayed busy past the longest wait) would save a result
        # from the purged writing when it is collected.
        left = batch_items_left(user.id)
        if left:
            raise PurgeIncomplete(
                "batch items still left: "
                + ", ".join(f"{k}={v}" for k, v in sorted(left.items())))
        for key, value in inflight.counts.items():
            counts[key] = counts.get(key, 0) + value
        job = db.session.get(UserDataPurge, job_id)
        job.status = "done"
        job.finished_at = _now()
        job.counts = counts
        job.error = None
        job.waiting_since = None
        forget_hidden(job_id)
        db.session.commit()
        logger.info("user data purge job %s (user %s) done", job_id, user.id)
        return "done"
    except PurgeSuperseded:
        db.session.rollback()
        logger.warning("user purge job %s: claimed by another runner; "
                       "stopping", job_id)
        return "superseded"
    except Exception as e:  # noqa: BLE001 - recorded; the beat retries
        db.session.rollback()
        logger.exception("user purge job %s failed", job_id)
        job = db.session.get(UserDataPurge, job_id)
        if job is not None and job.task_id == token and job.status == "running":
            job.error = f"{type(e).__name__}: {e}"[:255]
            # Stale at once: the next beat claims it (bounded by attempts).
            job.heartbeat_at = None
            db.session.commit()
        return "error"


def deletion_status(user_id):
    """The user's latest purge, for the Account page and the banner."""
    job = UserDataPurge.query.filter(
        UserDataPurge.user_id == user_id,
        UserDataPurge.status != "cancelled",
    ).order_by(UserDataPurge.id.desc()).first()
    out = {"status": None, "grace_days": PURGE_GRACE_DAYS,
           "purge_at": None, "requested_at": None, "finished_at": None,
           # The writing is hidden now and "Restore my writing" brings
           # it back (the user's own request, until it starts).
           "restorable": False,
           # X is connected for bookmarks: the purge revokes Loore's
           # access at X and deletes the stored connection.
           "x_connected": db.session.query(ExternalAccount.id).filter(
               ExternalAccount.user_id == user_id).first() is not None,
           "x_connection_removed": False}
    if job is None:
        return out
    from backend.utils.timefmt import iso_utc
    out.update({
        "status": job.status,
        "source": job.source,
        "purge_at": iso_utc(job.scheduled_for),
        "requested_at": iso_utc(job.requested_at),
        "finished_at": iso_utc(job.finished_at),
        "x_connection_removed": bool((job.counts or {}).get("external_account")),
        "restorable": job.status == "scheduled" and job.scope == "hidden",
    })
    return out
