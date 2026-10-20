"""One todo writer at a time per user (#477).

Every write of a user's todo list takes this lock first: a tick, the row
"+" and quick-add (PATCH), an editor Save (PUT), a revert, and the save of
a todo merge (tasks/voice_todo_merge.py). Under the lock the writer reads
the newest version, checks it and writes; the commit releases the lock. So
no write lands between another writer's read and its write: a tick can't
go into a version that a Save or a merge has just replaced, and a merge
saves on top of the list as it is when it saves.

It is a PostgreSQL advisory lock held for the rest of the transaction
(pg_advisory_xact_lock), keyed on the user: the commit or rollback
releases it, also when a worker dies mid-write. The merge's model call is
not inside it; only its save is, so a tick never waits for the model.

Other databases (SQLite in the tests) take no lock here: SQLite runs one
write transaction at a time on its own.
"""
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from backend.extensions import db

# Heuristic: how long a todo write waits for another write of the same
# user's todo to finish. Each holds the lock for one short transaction
# (read the newest version, check it, encrypt, write, commit), well under a
# second; waiting this long means a writer is stuck, and the request gives
# up (503, nothing written) rather than hang.
TODO_LOCK_WAIT_SECONDS = 10

# The first key of the two-key advisory lock: a fixed number naming "a
# user's todo list", so this lock can't collide with another advisory lock
# keyed on a user id. The second key is the user id.
_TODO_LOCK_NAMESPACE = 477001

# PostgreSQL's SQLSTATE for "lock_timeout reached" (lock_not_available).
_LOCK_NOT_AVAILABLE = "55P03"

TODO_BUSY_CODE = "todo_busy"
TODO_BUSY_MESSAGE = (
    "Your todo list is busy saving another change, so this change wasn't "
    "saved. Try again in a moment.")


class TodoBusy(Exception):
    """The lock wasn't free within TODO_LOCK_WAIT_SECONDS. The session was
    rolled back; nothing was written."""


def lock_user_todo(user_id):
    """Take the user's todo lock for the rest of the current transaction.

    Call it before reading the version you are going to check or build on,
    and commit (or roll back) promptly: that releases it. Raises TodoBusy
    after waiting TODO_LOCK_WAIT_SECONDS.
    """
    if db.session.get_bind().dialect.name != "postgresql":
        return
    set_lock_timeout = text("SELECT set_config('lock_timeout', :value, true)")
    previous = db.session.execute(
        text("SELECT current_setting('lock_timeout')")).scalar()
    try:
        # Bounds only this wait: the setting is put back right after, so
        # the writer's other statements wait as they always did.
        db.session.execute(set_lock_timeout,
                           {"value": f"{int(TODO_LOCK_WAIT_SECONDS * 1000)}ms"})
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :user_id)"),
            {"namespace": _TODO_LOCK_NAMESPACE, "user_id": int(user_id)})
    except OperationalError as e:
        if getattr(e.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
            raise
        db.session.rollback()
        raise TodoBusy() from e
    db.session.execute(set_lock_timeout, {"value": previous})
