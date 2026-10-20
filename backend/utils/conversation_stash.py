"""Server-side stash of an analyzed ChatGPT or Claude export, between the
analyze and confirm steps of the import, and the files the background
import job keeps next to it.

Why: the analyze step used to send every analyzed conversation back to
the browser, which posted all of it again on confirm (65-100 MB for a
large export), and the confirm request created every node itself. For
43k messages that ran far past the request timeouts. Now analyze writes
the conversations here and returns a token; confirm sends the token and
a Celery task (backend/tasks/chat_imports.py) imports from the stash.

Everything here lives in the user's folder under the Twitter import
stash root (``twitter_archive.STASH_ROOT/<user_id>/``): the one path the
web and worker processes share in every deployment, and a folder the
user data purge already deletes.

Files:

- ``<token>.conv``: the stash. JSON lines: a header with counts only
  (no content: conversations, messages, and messages and tokens per
  author), the dedup keys of every message, then one line per
  conversation in the order the import creates them. Every line after
  the header is sealed with one data key (encryption.new_dek, one KMS
  call per stash) in the same ENC:v2 envelope the node content uses, so
  conversation text is never on disk in plaintext while encryption is
  on. With encryption off (local development) the lines are plain JSON,
  as node content is then.
- ``<token>.state``: where the job is (the next conversation, the
  counts so far, and the result once it finished). Counts and
  timestamps only. A run of the job that stopped part-way (a deploy, a
  crash, the time limit) continues from here.
- ``<token>.run``: held with a non-blocking flock for the whole run of
  the job (run_guard), so a second copy of the same task running at the
  same time (Redis delivers an unacked message again after its
  visibility timeout) stops at once instead of importing alongside.
- ``conversation-import.lock``: the one running conversation import of
  this user: its task id and stash token. The job touches it after every
  committed chunk; a lock nobody touched for LOCK_STALE_SECONDS no
  longer counts as running. Changes to it happen under an flock on
  ``conversation-import.mutex`` (an empty file).

A stash is deleted when its job finishes, when the user analyzes
another export (one pending export per user; a running job's stash
stays), and by sweep_expired when nothing touched it for
STASH_TTL_SECONDS (never confirmed, or a failed job nobody resumed).
"""
import contextlib
import fcntl
import json
import os
import pathlib
import secrets
import time

from backend.utils import encryption
from backend.utils import twitter_archive

# HEURISTIC: how long an analyzed export waits for its confirm, and how
# long a failed job's stash stays for a resume. Same as the Twitter stash.
STASH_TTL_SECONDS = 24 * 3600
# HEURISTIC: a running job touches its lock after every committed chunk
# (seconds apart). A lock untouched this long belongs to a job that died
# or never started; a new confirm may then start the import again.
LOCK_STALE_SECONDS = 30 * 60

FORMAT = 1
SOURCES = ("chatgpt", "claude")
_SUFFIX = ".conv"
_STATE_SUFFIX = ".state"
_RUN_SUFFIX = ".run"
_LOCK_NAME = "conversation-import.lock"
_MUTEX_NAME = "conversation-import.mutex"


class StashError(ValueError):
    """A stash file that cannot be read (damaged, or not a stash)."""


class RunInProgress(RuntimeError):
    """Another run of the same import holds its run guard."""


def _root():
    return pathlib.Path(twitter_archive.STASH_ROOT)


def _user_dir(user_id):
    return _root() / str(int(user_id))


def _valid_token(token):
    return isinstance(token, str) and bool(
        twitter_archive._TOKEN_RE.match(token))


def stash_path(user_id, token):
    """Path of a stash, or None for a malformed token (never trust it
    into a filesystem path)."""
    if not _valid_token(token):
        return None
    return _user_dir(user_id) / f"{token}{_SUFFIX}"


def _state_path(user_id, token):
    if not _valid_token(token):
        return None
    return _user_dir(user_id) / f"{token}{_STATE_SUFFIX}"


def _lock_path(user_id):
    return _user_dir(user_id) / _LOCK_NAME


def _run_path(user_id, token):
    if not _valid_token(token):
        return None
    return _user_dir(user_id) / f"{token}{_RUN_SUFFIX}"


def _seal(text, dek):
    # encrypt_with_dek returns the text as it is when dek is None
    # (encryption off): a plain JSON line.
    return encryption.encrypt_with_dek(text, dek)


def _unseal(line):
    try:
        return json.loads(encryption.decrypt_content(line))
    except (ValueError, TypeError) as e:
        raise StashError(f"unreadable stash line ({type(e).__name__})")


def _write_json_atomically(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ---- stash -----------------------------------------------------------------

def write_stash(user_id, source, conversations, keys, total_messages,
                roles=None):
    """Store analyzed *conversations* (already in import order) and the
    dedup *keys* of their messages; returns the token. One KMS call (the
    data key) when encryption is on. *roles*: messages and tokens per
    author (counts only), kept in the header for the job's final log."""
    if source not in SOURCES:
        raise ValueError(f"Unknown source {source!r}")
    sweep_expired()
    folder = _user_dir(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path = stash_path(user_id, token)
    dek = encryption.new_dek()
    header = {
        "format": FORMAT,
        "source": source,
        "conversations": len(conversations),
        "messages": int(total_messages),
        "roles": {k: int(v) for k, v in (roles or {}).items()},
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            f.write(_seal(json.dumps(list(keys)), dek) + "\n")
            for conv in conversations:
                f.write(_seal(json.dumps(conv, ensure_ascii=False), dek))
                f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        _remove(tmp)
        raise
    _drop_other_stashes(user_id, keep=token)
    return token


def _drop_other_stashes(user_id, keep):
    """One pending export per user: a new analyze removes the user's
    other stashes (a cancelled dialog, a reload, a lost response, a
    failed job) and their job files, except the stash of a running job.
    A stash of 100 MB or more would otherwise stay for the TTL."""
    folder = _user_dir(user_id)
    with _lock_mutex(user_id):
        running = running_job(user_id)
        keep_tokens = {keep}
        if running and running.get("token"):
            keep_tokens.add(running["token"])
        for suffix in (_SUFFIX, _STATE_SUFFIX, _RUN_SUFFIX):
            for f in folder.glob(f"*{suffix}"):
                if f.name[:-len(suffix)] not in keep_tokens:
                    _remove(f)


def is_fresh(path, now=None):
    """True when the stash exists and something touched it (written,
    or a job working on it) within STASH_TTL_SECONDS."""
    try:
        age = (now or time.time()) - path.stat().st_mtime
    except (OSError, AttributeError):
        return False
    return age <= STASH_TTL_SECONDS


def touch(path):
    try:
        os.utime(path, None)
    except OSError:
        pass


def read_header(path):
    with open(path, encoding="utf-8") as f:
        line = f.readline()
    try:
        header = json.loads(line)
    except ValueError:
        raise StashError("unreadable stash header")
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise StashError("unknown stash format")
    return header


def read_keys(path):
    """The dedup keys of every message in the stash (for the confirm
    step's deleted-content check)."""
    with open(path, encoding="utf-8") as f:
        f.readline()
        return _unseal(f.readline().rstrip("\n"))


def iter_conversations(path, start=0):
    """Yield (index, conversation) from *start* on, one line at a time:
    memory holds one conversation, not the export."""
    with open(path, encoding="utf-8") as f:
        f.readline()  # header
        f.readline()  # keys
        for index, line in enumerate(f):
            if index < start:
                continue
            line = line.rstrip("\n")
            if line:
                yield index, _unseal(line)


def delete_stash(user_id, token):
    """Remove the stash (the conversation text) and its run file; the
    state file stays until the sweep, so a repeated run of a finished
    job still finds its result."""
    for path in (stash_path(user_id, token), _run_path(user_id, token)):
        if path is not None:
            _remove(path)


@contextlib.contextmanager
def run_guard(user_id, token):
    """Hold for the whole run of one import. A second run of the same
    import while this one runs (the same task id delivered again) cannot
    take it and gets RunInProgress before it reads or writes anything.
    The kernel drops the flock when the process dies, so a run after a
    crash takes it at once."""
    path = _run_path(user_id, token)
    if path is None:  # malformed token: the run reports it as expired
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # EWOULDBLOCK: another run holds it
            raise RunInProgress()
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---- job state ---------------------------------------------------------------

def read_state(user_id, token):
    path = _state_path(user_id, token)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def write_state(user_id, token, state):
    path = _state_path(user_id, token)
    if path is None:
        raise ValueError("malformed token")
    _write_json_atomically(path, state)


# ---- the user's one running conversation import ------------------------------

def _lock_age(path, now=None):
    try:
        return (now or time.time()) - path.stat().st_mtime
    except OSError:
        return None


def read_lock(user_id, now=None):
    """The user's running conversation import as {"task_id", "token",
    "source", "stale"}, or None when there is none."""
    path = _lock_path(user_id)
    try:
        with open(path, encoding="utf-8") as f:
            lock = json.load(f)
    except (OSError, ValueError):
        return None
    age = _lock_age(path, now)
    if not isinstance(lock, dict) or age is None:
        return None
    lock["stale"] = age > LOCK_STALE_SECONDS
    return lock


def running_job(user_id, now=None):
    """The lock of a conversation import that counts as running, or None."""
    lock = read_lock(user_id, now)
    if lock is None or lock["stale"]:
        return None
    return lock


@contextlib.contextmanager
def _lock_mutex(user_id):
    """Serialize every read-check-write of the user's lock across the web
    and worker processes (an flock on a small file next to it), so two
    confirms that arrive together cannot both start a job, and replacing
    a stale lock cannot remove a fresh one."""
    folder = _user_dir(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / _MUTEX_NAME, "a") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(mutex, fcntl.LOCK_UN)


def _write_lock(user_id, task_id, token, source):
    lock = {"task_id": task_id, "token": token, "source": source}
    _write_json_atomically(_lock_path(user_id), lock)
    return lock


def acquire_lock(user_id, task_id, token, source):
    """Take the user's import lock for a new job. Returns (True, lock)
    or (False, the running job's lock). A stale lock is replaced."""
    with _lock_mutex(user_id):
        held = read_lock(user_id)
        if held is not None and not held["stale"]:
            return False, held
        return True, _write_lock(user_id, task_id, token, source)


def claim_lock(user_id, task_id, token, source):
    """Called by the job when it starts: True when the lock is this
    task's (written again if it went missing or went stale), False when
    another task holds a fresh one (this run was superseded)."""
    with _lock_mutex(user_id):
        held = read_lock(user_id)
        if held is not None and held.get("task_id") != task_id \
                and not held["stale"]:
            return False
        _write_lock(user_id, task_id, token, source)
        return True


def heartbeat(user_id, task_id):
    """Touch the lock if this task holds it."""
    held = read_lock(user_id)
    if held is not None and held.get("task_id") == task_id:
        touch(_lock_path(user_id))
        return True
    return False


def release_lock(user_id, task_id):
    """Remove the lock if this task holds it."""
    with _lock_mutex(user_id):
        held = read_lock(user_id)
        if held is not None and held.get("task_id") == task_id:
            _remove(_lock_path(user_id))


# ---- sweep -------------------------------------------------------------------

def sweep_expired(now=None):
    """Remove stashes, job states and locks nothing touched within
    STASH_TTL_SECONDS. Returns the number of files removed."""
    root = _root()
    if not root.exists():
        return 0
    now = now or time.time()
    removed = 0
    for user_dir in root.iterdir():
        if not user_dir.is_dir():
            continue
        patterns = (f"*{_SUFFIX}", f"*{_STATE_SUFFIX}", f"*{_RUN_SUFFIX}",
                    "*.tmp", _LOCK_NAME)
        for pattern in patterns:
            for f in user_dir.glob(pattern):
                try:
                    if now - f.stat().st_mtime > STASH_TTL_SECONDS:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
    return removed
