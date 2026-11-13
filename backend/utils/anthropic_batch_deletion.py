"""Anthropic Message Batches are deleted once Loore no longer needs them
(#693; Peter, 2026-11-13: "they should, as a followup" of #667).

An ended batch and its results stay retrievable at Anthropic for 29 days
after the batch was created, and the batch stays listed after that,
unless it is deleted (DELETE /v1/messages/batches/{id}; Message Batches
API docs, checked 2026-11-13). The API deletes only a batch that has
finished processing: one still running is cancelled first. The docs say
nothing more about what Anthropic keeps after a delete; its data
retention policy applies. #667 deletes an OpenAI batch's files at the
same points (utils/llm_batch).

Bookkeeping (``AnthropicBatchDeletion``, one row per batch):

- **Registered with its owner.** The row is added in the same commit as
  the record that owns the batch: the batch job row (profile, intentions,
  poll draft, external digest, recent context) or the Glean reply's
  "_batch" entry. ``register`` does it; the caller commits.
- **Deleted right after the save.** Once a collector has committed what
  it saved from an ended batch, it deletes the batch
  (``delete_collected_batches``, ``delete_now``) and marks the row.
- **Swept.** A crash between the save and the delete, a failed delete,
  and a batch Loore gave up on (failed, expired, cancelled, abandoned,
  results not used) are left to ``sweep``: hourly, bounded, idempotent.
  It deletes the batch of a row whose owner no longer needs it
  (``still_needed``): a job that is no longer pending (or no longer
  exists), a reply that is no longer at work (or no longer exists). A
  batch still running is cancelled first and deleted by a later sweep.
  One exception: a job with a result that came back but could not be
  saved (``UNSAVED_RESULT``, an intentions save that raised) keeps its
  batch while Anthropic keeps results, for
  ``backfill_intentions.py --collect``.

A 404 counts as deleted. A delete that fails is counted on the row
(``attempts``, ``last_error``: the error's type) and tried again by the
next sweep; it never fails the job. After ``ERROR_AFTER_ATTEMPTS``
failures in a row it is reported once at ERROR (Sentry) and then tried
once a day (``BACKOFF_AFTER_ERROR``); the admin script tries it at once.
Batch ids and error types only are logged: nothing of a request or a
result. The task's soft time limit is let through (utils/soft_limit).

Batches created before this change have no row: the admin script
``backend/scripts/delete_anthropic_batches.py`` finds the ones Loore has
recorded (dry run by default) and registers them for deletion.
"""
import logging
from datetime import datetime, timedelta

from backend.extensions import db
from backend.utils import soft_limit

log = logging.getLogger(__name__)

# The batch job tables. A batch one of them owns is needed while its job
# is pending.
JOB_SOURCES = ("profile_batch_job", "poll_draft_batch_job",
               "external_digest_batch_job", "recent_context_batch_job")
# A Glean reply's one-item batch, owned by its reply node.
READ_SOURCE = "read"
# Batches registered only once nothing needs them: a Glean reply's batch
# an account purge cancelled, and the admin backfill script's.
PURGE_SOURCE = "purge"
SCRIPT_SOURCE = "backfill_intentions"
# The batch id a profile job records for a submission that never went
# out (tasks/profile_batch): no batch.
NOT_A_BATCH = ("unsubmitted",)
# A reply node that may still poll its batch.
NODE_IN_FLIGHT = ("pending", "processing")
# HEURISTIC: rows one sweep looks at. Most batches are deleted right
# after their collect, so the sweep sees the leftovers; each row costs at
# most one status read, one delete or cancel call and a database lookup.
# 100 rows are at most 200 calls in a run, against the Message Batches
# API's own limit of 1,000 requests per minute at the lowest tier, which
# the collectors' polls share.
SWEEP_LIMIT = 100
# HEURISTIC: failed deletes in a row after which the batch is reported at
# ERROR (once) and no longer tried every hour. 5: the delete right after
# the save and four hourly sweeps; a passing outage or rate limit is over
# by then, and what is left (a revoked key, a refused request) does not
# go away by itself.
ERROR_AFTER_ATTEMPTS = 5
# HEURISTIC: after that, the row is tried again once a day, so a fixed
# key deletes it without anyone running the script.
BACKOFF_AFTER_ERROR = timedelta(days=1)
# An item flag on a batch job: a result came back but its save raised
# (tasks/profile_batch, an intentions item). Its batch stays while
# Anthropic keeps results: 29 days after the batch was created (the job
# is submitted after that, so its submission time is a safe stand-in).
UNSAVED_RESULT = "result_unsaved"
RESULTS_AVAILABLE = timedelta(days=29)


def holds_unsaved_result(items):
    """Whether a job's items flag a result that was not saved."""
    return any(isinstance(i, dict) and i.get(UNSAVED_RESULT)
               for i in items or ())


def job_needs_batch(status, unsaved, submitted_at, now=None):
    """Whether a batch job still needs its batch: it is pending, or it
    holds a result that was not saved and Anthropic still keeps it."""
    if status == "pending":
        return True
    now = now or datetime.utcnow()
    return bool(unsaved) and submitted_at is not None and (
        now - submitted_at < RESULTS_AVAILABLE)


def unsaved_result_flagged(model):
    """SQL: the job's items carry UNSAVED_RESULT (a text match on the
    JSON, so no job's items are loaded)."""
    from sqlalchemy import Text, cast
    return cast(model.items, Text).like(f'%"{UNSAVED_RESULT}"%')


def job_models():
    """{source: model} of the batch job tables."""
    from backend.models import (
        ExternalDigestBatchJob, PollDraftBatchJob, ProfileBatchJob,
        RecentContextBatchJob,
    )
    return {m.__tablename__: m for m in (
        ProfileBatchJob, PollDraftBatchJob, ExternalDigestBatchJob,
        RecentContextBatchJob)}


def register(provider_key, batch_id, source, key_type="chat",
             node_id=None):
    """Record an Anthropic batch in the caller's transaction (the caller
    commits). Nothing for another provider or without a batch id.
    Idempotent: an existing row is kept as it is. Returns the row or
    None."""
    if (provider_key != "anthropic" or not batch_id
            or batch_id in NOT_A_BATCH):
        return None
    from backend.models import AnthropicBatchDeletion
    row = AnthropicBatchDeletion.query.filter_by(batch_id=batch_id).first()
    if row is None:
        row = AnthropicBatchDeletion(
            batch_id=batch_id, source=source, key_type=key_type or "chat",
            node_id=node_id)
        db.session.add(row)
    return row


def still_needed(row, now=None):
    """Whether Loore may still read the batch of *row*: the job that owns
    it is pending or holds a result that was not saved
    (``job_needs_batch``), or the reply that owns it is still at work (a
    reply keeps polling while it is pending or processing). Ids and
    statuses only. A row of another source (a script's batch, a purge's)
    is registered only once nothing needs the batch."""
    if row.source in JOB_SOURCES:
        model = job_models()[row.source]
        return any(
            job_needs_batch(status, unsaved, submitted_at, now)
            for status, submitted_at, unsaved in db.session.query(
                model.status, model.submitted_at,
                unsaved_result_flagged(model)).filter(
                model.batch_id == row.batch_id))
    if row.source == READ_SOURCE and row.node_id is not None:
        from backend.models import Node
        status = db.session.query(Node.llm_task_status).filter(
            Node.id == row.node_id).scalar()
        return status in NODE_IN_FLIGHT
    return False


def _anthropic_client(key_type):
    """The Anthropic client for the key the batch was created under, or
    None when no key is configured."""
    from flask import current_app
    from backend.utils.api_keys import get_api_keys_for_usage
    key = get_api_keys_for_usage(current_app.config,
                                 key_type or "chat").get("anthropic")
    if not key:
        return None
    from anthropic import Anthropic
    return Anthropic(api_key=key)


def _gone(row, now, outcome):
    row.deleted_at = now
    row.checked_at = now
    row.last_error = None
    return outcome


def _failed(row, now, error_type):
    """Count a failed call on the row. The ERROR_AFTER_ATTEMPTS-th failure
    in a row is reported once at ERROR (it reaches Sentry); the row is
    then tried once a day (due_rows)."""
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error_type[:64]
    row.checked_at = now
    if row.attempts == ERROR_AFTER_ATTEMPTS:
        log.error("Deleting Anthropic batch %s failed %s times in a row "
                  "(%s); tried once a day from now on, or at once with "
                  "backend/scripts/delete_anthropic_batches.py --apply",
                  row.batch_id, row.attempts, row.last_error)
    else:
        log.warning("Deleting Anthropic batch %s failed (%s, %s in a row); "
                    "tried again later", row.batch_id, row.last_error,
                    row.attempts)
    return "failed"


def delete_row(row, client, now=None, ended=False, cancel_running=True):
    """Delete the batch of *row* at Anthropic. *ended*: the caller read
    it as ended just now, so it is deleted without a status read;
    otherwise its status is read first, and a batch still running is
    cancelled (*cancel_running*) or left, for a later sweep. Returns the
    outcome: "deleted", "gone" (404), "cancelled" (cancel requested),
    "running" (still running or cancelling) or "failed" (counted on the
    row). Changes the row; the caller commits. Never raises but the soft
    limit."""
    from backend.utils.llm_batch import anthropic_not_found
    now = now or datetime.utcnow()
    not_found = anthropic_not_found()
    try:
        if not ended:
            batch = client.messages.batches.retrieve(row.batch_id)
            status = getattr(batch, "processing_status", None)
            if status != "ended":
                row.checked_at = now
                row.attempts = 0   # the calls work: no failure in a row
                if (status == "in_progress" and cancel_running
                        and row.cancel_requested_at is None):
                    client.messages.batches.cancel(row.batch_id)
                    row.cancel_requested_at = now
                    log.info("Anthropic batch %s no longer needed and "
                             "still running: cancel requested",
                             row.batch_id)
                    return "cancelled"
                return "running"
        client.messages.batches.delete(row.batch_id)
    except not_found:
        return _gone(row, now, "gone")
    except Exception as e:  # noqa: BLE001 - counted, swept again
        soft_limit.reraise(e)
        return _failed(row, now, type(e).__name__)
    log.info("Anthropic batch %s deleted", row.batch_id)
    return _gone(row, now, "deleted")


def delete_now(batch_ids, source, key_type="chat", node_id=None):
    """Delete ended Anthropic batches whose outcome the caller has just
    committed, and record it. A batch without a row (created before
    #693) gets one first. A failed delete stays on its row for the sweep.
    Returns {outcome: count}. Never raises but the soft limit."""
    counts = {}
    ids = [b for b in dict.fromkeys(batch_ids or ())
           if b and b not in NOT_A_BATCH]
    if not ids:
        return counts
    try:
        client = _anthropic_client(key_type)
        now = datetime.utcnow()
        for batch_id in ids:
            row = register("anthropic", batch_id, source, key_type, node_id)
            if row.deleted_at is not None:
                outcome = "gone"
            elif client is None:
                outcome = _failed(row, now, "NoKey")
            else:
                outcome = delete_row(row, client, now, ended=True)
            counts[outcome] = counts.get(outcome, 0) + 1
            db.session.commit()
    except Exception as e:  # noqa: BLE001 - never fails the job
        soft_limit.reraise(e)
        db.session.rollback()
        log.warning("Deleting Anthropic batches %s failed (%s); the hourly "
                    "sweep tries again", ids, type(e).__name__)
    return counts


def delete_collected_batches(results, api_keys, source):
    """After a collector has committed what it saved from a
    batch_check_and_collect: the ended batches' files at OpenAI (#667)
    and the ended Anthropic batches (#693) are deleted. Never raises but
    the soft limit."""
    from backend.utils.llm_batch import delete_collected_batch_files
    delete_collected_batch_files(results, api_keys)
    delete_now(getattr(results, "anthropic_batches", None) or (), source)


def due_rows(limit=SWEEP_LIMIT, now=None, backed_off_too=False):
    """The rows not yet deleted, the ones looked at longest ago (or never)
    first. A row whose delete failed ERROR_AFTER_ATTEMPTS times in a row
    is due once a day (BACKOFF_AFTER_ERROR), unless *backed_off_too*
    (the admin script)."""
    from sqlalchemy import func, or_
    from backend.models import AnthropicBatchDeletion as R
    q = R.query.filter(R.deleted_at.is_(None))
    if not backed_off_too:
        now = now or datetime.utcnow()
        q = q.filter(or_(R.attempts < ERROR_AFTER_ATTEMPTS,
                         R.checked_at.is_(None),
                         R.checked_at <= now - BACKOFF_AFTER_ERROR))
    q = q.order_by(func.coalesce(R.checked_at, R.created_at), R.id)
    return q.limit(limit).all() if limit else q.all()


def process(rows, now=None, cancel_running=True, pause=None):
    """Delete the batches of *rows* that Loore no longer needs (see the
    module docstring). *pause*: seconds to wait after each provider call
    (the admin script's pace). Commits after each row. Returns
    {outcome: count}, with "needed" for the rows skipped."""
    import time
    now = now or datetime.utcnow()
    counts = {}
    clients = {}
    for row in rows:
        if still_needed(row, now):
            row.checked_at = now
            outcome = "needed"
        else:
            if row.key_type not in clients:
                clients[row.key_type] = _anthropic_client(row.key_type)
            client = clients[row.key_type]
            if client is None:
                outcome = _failed(row, now, "NoKey")
            else:
                outcome = delete_row(row, client, now,
                                     cancel_running=cancel_running)
                if pause:
                    time.sleep(pause)
        counts[outcome] = counts.get(outcome, 0) + 1
        db.session.commit()
    return counts


def sweep(limit=SWEEP_LIMIT, now=None):
    """One bounded pass over the rows due (``due_rows``). Returns
    {outcome: count}."""
    now = now or datetime.utcnow()
    rows = due_rows(limit, now)
    if not rows:
        return {}
    counts = process(rows, now)
    done = counts.get("deleted", 0) + counts.get("gone", 0)
    if done or counts.get("failed"):
        log.info("Anthropic batch sweep: %s", counts)
    return counts
