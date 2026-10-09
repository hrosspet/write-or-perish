"""Rows hidden by a waiting "Delete all my writing" (#268).

Peter, 2026-10-09: "I was expecting a soft-delete of everything
immediately + real deletion after 30 days. The Cancel deletion button
undoing the soft deletion."

The request records what it hides in ``user_data_purge_hidden``
(backend/models.py, UserDataPurgeHidden). Nodes are soft-deleted
(``deleted_at``), so every existing check of a deleted node applies,
including the placeholders that keep other people's replies in their
threads. The rows of the per-user tables (``HIDDEN_ROW_TABLES``: profile,
recent context, todo list, documents, drafts, shares, saved references,
prompts, poll answers, Read picks, reference marks, feedback) have no
such column, so this module leaves them out of every ORM SELECT instead:
a ``do_orm_execute`` hook adds "not recorded as hidden" to each query of
those tables, relationship loads included. A restore deletes the records
and the rows show again; the purge deletes the rows.

The purge, the restore and the counts read the hidden rows through
``including_hidden_rows()``. Writes (UPDATE, DELETE) are never filtered.

A table with a unique key on the user's rows needs the hidden row out of
the way when the user saves the same thing again during the grace
period: ``reclaim_*`` below.
"""
import contextvars
from contextlib import contextmanager

from sqlalchemy import event, select
from sqlalchemy.orm import Session, with_loader_criteria

_include_hidden = contextvars.ContextVar("include_hidden_rows",
                                         default=False)
_options = {}   # mapper -> option
_by_table = {}  # table name -> option
_installed = False


@contextmanager
def including_hidden_rows():
    """Queries inside see the hidden rows too (the purge, the restore,
    the counts)."""
    token = _include_hidden.set(True)
    try:
        yield
    finally:
        _include_hidden.reset(token)


def hidden_ids(kind, job_id=None):
    """SELECT of the row ids recorded as hidden for *kind* (a table
    name), by one job or by any."""
    from backend.models import UserDataPurgeHidden as H
    q = select(H.row_id).where(H.kind == kind)
    if job_id is not None:
        q = q.where(H.job_id == job_id)
    return q


def _not_hidden(model):
    return with_loader_criteria(
        model, ~model.id.in_(hidden_ids(model.__tablename__)),
        include_aliases=True)


def _hide_recorded_rows(state):
    """Add "not hidden" for each hidden-rows table the query selects from
    (its entities and columns: every read of a row's content or id).
    The criteria then apply wherever that table occurs in the statement.
    A query that selects none of them gets nothing added, so the rest of
    Loore pays almost nothing for this."""
    if (not state.is_select or _include_hidden.get()
            or state.execution_options.get("include_hidden_rows")):
        return
    mappers = state.all_mappers
    if mappers:
        opts = [_options[m] for m in mappers if m in _options]
    else:
        # No entity at the top (Query.count(), func.count(), select_from):
        # look for the tables anywhere in the statement.
        opts = [_by_table[t] for t in _tables_in(state.statement)
                if t in _by_table]
    if opts:
        state.statement = state.statement.options(*opts)


def _tables_in(statement):
    from sqlalchemy.sql import visitors
    return {el.name for el in visitors.iterate(statement)
            if getattr(el, "__visit_name__", None) == "table"}


def install():
    """Called once from backend.models, after the models exist."""
    global _options, _by_table, _installed
    from sqlalchemy import inspect
    from backend.models import HIDDEN_ROW_TABLES
    _options = {inspect(m): _not_hidden(m) for m in HIDDEN_ROW_TABLES}
    _by_table = {m.__tablename__: _options[inspect(m)]
                 for m in HIDDEN_ROW_TABLES}
    if not _installed:
        event.listen(Session, "do_orm_execute", _hide_recorded_rows)
        _installed = True


# ── Background jobs ─────────────────────────────────────────────────────

def on_hold_user_ids():
    """SELECT of the users whose writing is on hold: a "Delete all my
    writing" is waiting (the writing is hidden) or a purge is running.
    No background job builds anything from their writing meanwhile
    (profile, recent context, digests, Reads, embeddings, TTS, bookmark
    sync): it would read writing the user asked to delete, or save
    something new from it that outlives the purge."""
    from backend.models import UserDataPurge
    return select(UserDataPurge.user_id).where(
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES))


class WritingOnHold(Exception):
    """A job that read the user's writing before a "Delete all my
    writing" hid it tries to save something from it: nothing is saved."""


def writing_on_hold(user_id):
    """True when *user_id*'s writing is on hold (see on_hold_user_ids)."""
    from backend.extensions import db
    from backend.models import UserDataPurge
    if user_id is None:
        return False
    return db.session.query(UserDataPurge.id).filter(
        UserDataPurge.user_id == user_id,
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES),
    ).first() is not None


# ── The user saves the same thing again during the grace period ─────────

def _unhide(kind, ids):
    """Take rows out of the hidden set: they are the user's again and stay
    after the purge. Returns how many records went."""
    from backend.models import UserDataPurgeHidden as H
    ids = [i for i in ids if i is not None]
    if not ids:
        return 0
    return H.query.filter(H.kind == kind, H.row_id.in_(ids)).delete(
        synchronize_session=False)


def reclaim_external_items(user_id, external_ids, sources=None):
    """Saved references (#208) have a unique key per user, source and
    external id. Saving or importing a reference that a waiting "Delete
    all my writing" hid makes it the user's again (as re-importing an
    entry brings back a deleted node), instead of failing on the hidden
    row. Call before looking the rows up."""
    from backend.models import ExternalItem
    external_ids = [str(e) for e in external_ids if e is not None]
    if not external_ids:
        return 0
    with including_hidden_rows():
        q = ExternalItem.query.filter(
            ExternalItem.user_id == user_id,
            ExternalItem.external_id.in_(external_ids),
            ExternalItem.id.in_(hidden_ids("external_item")))
        if sources:
            q = q.filter(ExternalItem.source.in_(list(sources)))
        ids = [i for (i,) in q.with_entities(ExternalItem.id)]
    return _unhide("external_item", ids)


def drop_hidden_poll_response(poll_id, user_id):
    """A poll answer has a unique key per poll and user. Answering a poll
    again after "Delete all my writing" hid the old answer replaces it:
    the hidden answer is deleted now (the purge would delete it anyway),
    so the old text never comes back over the new one. Returns True when
    one went."""
    from backend.extensions import db
    from backend.models import PollResponse, UserDataPurgeHidden as H
    with including_hidden_rows():
        row = PollResponse.query.filter(
            PollResponse.poll_id == poll_id, PollResponse.user_id == user_id,
            PollResponse.id.in_(hidden_ids("poll_response"))).first()
        if row is None:
            return False
        H.query.filter(H.kind == "poll_response",
                       H.row_id == row.id).delete(synchronize_session=False)
        db.session.delete(row)
        db.session.flush()
    return True


# ── The short profile description ───────────────────────────────────────

def description_hidden(user_id):
    """The user's description is hidden by a waiting "Delete all my
    writing"."""
    from backend.extensions import db
    from backend.models import UserDataPurgeHidden as H
    return db.session.query(H.id).filter(
        H.kind == "user_description", H.row_id == user_id).first() is not None


def shown_description(user):
    """The description as others and the user see it: empty while a
    waiting "Delete all my writing" hides it."""
    if user is None or not user.description:
        return user.description if user is not None else None
    return "" if description_hidden(user.id) else user.description


def reclaim_description(user_id):
    """The user wrote a new description during the grace period: it is
    theirs and stays after the purge."""
    return _unhide("user_description", [user_id])
