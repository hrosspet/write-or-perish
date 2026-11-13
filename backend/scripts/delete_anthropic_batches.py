#!/usr/bin/env python3
"""Delete the Anthropic Message Batches from before #693 that Loore no
longer needs.

Since #693 every Anthropic batch Loore creates is registered
(AnthropicBatchDeletion) and deleted at Anthropic once its results are
saved, or once Loore gave up on it (utils/anthropic_batch_deletion.py).
The batches created before have no registration: their results stay
retrievable for 29 days after creation, and the batches stay listed after
that. This script finds the ones Loore has recorded and no longer needs,
and deletes them.

Only batch ids recorded in Loore's database are touched, because the key
can be shared with other uses (another environment, the lab, a script
run): a batch Loore has no record of is never deleted. What it finds
(batch ids and statuses only; no content is read or decrypted):

- batch job rows on Anthropic that are no longer pending (collected,
  failed, abandoned or cancelled): profile builds and intentions, poll
  drafts, external digests, recent context; a job holding a result whose
  save failed keeps its batch while Anthropic keeps results;
- Glean replies' "_batch" entries on Anthropic whose reply is no longer
  at work (completed, failed or cancelled);
- registrations not yet deleted (the hourly sweep's work).

Not found, so left at Anthropic: batches of other environments on the
same key, of `backfill_intentions.py --batch` runs before #693 (their
ids were printed, never stored), of the prompt RCT experiment (ids in its
local results file), of a job row a purge deleted before #693, and of a
submission whose job row was never committed.

Dry run by default: counts only, and (unless --no-list) a listing of the
key's batches at Anthropic to say how many of the recorded ones still
exist and how many listed batches Loore has no record of. --apply
registers the batches found and deletes them, one at a time with a pause
between them; a batch still running is left to the hourly sweep, which
cancels it first. A registration whose deletes kept failing (tried once a
day by the sweep) is tried at once. --limit N registers and deletes at
most N of the batches found, and registers none of the others, so the
sweep does not take them either: a way to try a few first. Idempotent:
run it again and it finds only what is left. Prints counts only: no ids,
no keys, no content.

    python backend/scripts/delete_anthropic_batches.py            # dry run
    python backend/scripts/delete_anthropic_batches.py --apply
    python backend/scripts/delete_anthropic_batches.py --apply --limit 500
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# HEURISTIC: seconds to wait after each batch --apply sends to Anthropic
# (a status read and a delete). At most about 400 calls a minute, under
# the Message Batches API's 1,000 requests a minute at the lowest tier,
# which the collectors' polls share.
PAUSE_SECONDS = 0.3
# Rows per commit while registering.
COMMIT_EVERY = 500


def find_recorded():
    """({batch_id: (source, key_type, node_id)}, in_use): the Anthropic
    batches Loore has recorded and no longer needs that are not
    registered yet, and the ids of the recorded ones still in use (a
    pending job, a reply at work)."""
    from backend.extensions import db
    from backend.models import AnthropicBatchDeletion
    registered = {b for (b,) in db.session.query(
        AnthropicBatchDeletion.batch_id)}
    found, in_use = {}, set()
    for batch_id, owner, needed in _job_batches() + _read_batches():
        if needed:
            in_use.add(batch_id)
        elif batch_id not in registered:
            found.setdefault(batch_id, owner)
    for batch_id in in_use:
        found.pop(batch_id, None)
    return found, in_use


def _job_batches():
    """[(batch_id, (source, key_type, None), needed)] of the batch job
    rows on Anthropic (needed: pending, or holding a result whose save
    failed while Anthropic keeps it)."""
    from datetime import datetime
    from backend.extensions import db
    from backend.utils.anthropic_batch_deletion import (
        NOT_A_BATCH, job_models, job_needs_batch, unsaved_result_flagged,
    )
    now = datetime.utcnow()
    out = []
    for source, model in job_models().items():
        for batch_id, status, submitted_at, unsaved in db.session.query(
                model.batch_id, model.status, model.submitted_at,
                unsaved_result_flagged(model)).filter(
                model.provider_key == "anthropic"):
            if batch_id and batch_id not in NOT_A_BATCH:
                out.append((batch_id, (source, "chat", None),
                            job_needs_batch(status, unsaved, submitted_at,
                                            now)))
    return out


def _read_batches():
    """[(batch_id, ("read", key_type, node_id), needed)] of the Glean
    replies' Anthropic batch entries (ids and statuses only)."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.anthropic_batch_deletion import (
        NODE_IN_FLIGHT, READ_SOURCE,
    )
    out = []
    rows = db.session.query(
        Node.id, Node.llm_task_status, Node.tool_calls_meta).filter(
        Node.node_type == "llm",
        Node.tool_calls_meta.like('%"_batch"%')).yield_per(500)
    for node_id, status, meta in rows:
        for entry in _batch_entries(meta):
            batch_id = entry.get("batch_id")
            if ((entry.get("provider") or "anthropic") == "anthropic"
                    and batch_id):
                out.append((batch_id, (READ_SOURCE,
                                       entry.get("key_type") or "chat",
                                       node_id),
                            status in NODE_IN_FLIGHT))
    return out


def _batch_entries(meta):
    try:
        entries = json.loads(meta or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict) and e.get("name") == "_batch"]


def registration_counts():
    """(deleted, waiting) registrations."""
    from backend.extensions import db
    from backend.models import AnthropicBatchDeletion as R
    deleted = db.session.query(R.id).filter(R.deleted_at.isnot(None)).count()
    waiting = db.session.query(R.id).filter(R.deleted_at.is_(None)).count()
    return deleted, waiting


def list_remote(found, in_use, out=print):
    """Count the batches the keys list at Anthropic against *found*,
    *in_use* and the waiting registrations. Prints counts per key
    type."""
    from anthropic import Anthropic
    from flask import current_app
    from backend.models import AnthropicBatchDeletion as R
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.llm_batch import LIST_PAGE_SIZE
    waiting = {b for (b,) in R.query.filter(
        R.deleted_at.is_(None)).with_entities(R.batch_id)}
    by_key = {}
    for key_type in ("chat", "train"):
        key = get_api_keys_for_usage(current_app.config,
                                     key_type).get("anthropic")
        if key:
            by_key.setdefault(key, []).append(key_type)
    for key, key_types in by_key.items():
        label = " and ".join(key_types)
        counts = Counter()
        client = Anthropic(api_key=key)
        cursor = None
        while True:
            page = client.messages.batches.list(
                limit=LIST_PAGE_SIZE["anthropic"],
                **({"after_id": cursor} if cursor else {}))
            data = list(getattr(page, "data", None) or [])
            for batch in data:
                ended = getattr(batch, "processing_status", None) == "ended"
                counts["listed"] += 1
                counts["ended" if ended else "processing"] += 1
                if batch.id in found:
                    counts["found_ended" if ended else "found_running"] += 1
                elif batch.id in waiting:
                    counts["registered"] += 1
                elif batch.id in in_use:
                    counts["in_use"] += 1
                else:
                    counts["unrecorded"] += 1
            if not data or not getattr(page, "has_more", False):
                break
            cursor = getattr(page, "last_id", None) or data[-1].id
        out(f"Listed at Anthropic ({label} key): {counts['listed']} "
            f"batch(es), {counts['ended']} ended and {counts['processing']} "
            f"still processing. Of them: {counts['found_ended']} found here "
            f"and ended (to delete), {counts['found_running']} found here "
            f"and still running (left to the hourly sweep), "
            f"{counts['in_use']} still in use here, "
            f"{counts['registered']} already registered, "
            f"{counts['unrecorded']} not recorded here (never touched).")
    return by_key


def register_found(found):
    from backend.extensions import db
    from backend.utils.anthropic_batch_deletion import register
    for n, (batch_id, (source, key_type, node_id)) in enumerate(
            found.items(), 1):
        register("anthropic", batch_id, source, key_type, node_id)
        if n % COMMIT_EVERY == 0:
            db.session.commit()
    db.session.commit()


def run(apply=False, list_at_anthropic=True, limit=None, pause=PAUSE_SECONDS,
        out=print):
    """The script's work; returns the outcome counts of --apply (or {})."""
    found, in_use = find_recorded()
    by_source = Counter(source for source, _k, _n in found.values())
    deleted, waiting = registration_counts()
    out(f"Recorded here, no longer needed and not registered: "
        f"{len(found)} batch(es)"
        + (" (" + ", ".join(f"{s}: {n}" for s, n in sorted(
            by_source.items())) + ")" if found else ""))
    out(f"Registered: {deleted} deleted, {waiting} waiting")
    if list_at_anthropic:
        list_remote(found, in_use, out=out)
    if not apply:
        out("Dry run: nothing registered or deleted. --apply registers "
            "these and deletes them.")
        return {}
    from backend.utils.anthropic_batch_deletion import process
    # --limit N: only N are registered, so the hourly sweep does not
    # delete the others either.
    picked = dict(list(found.items())[:limit]) if limit else found
    register_found(picked)
    if len(picked) < len(found):
        out(f"Registered {len(picked)} of the {len(found)} found; the other "
            f"{len(found) - len(picked)} are left for a later run")
    counts = process(_rows_to_process(list(picked), limit),
                     cancel_running=False, pause=pause)
    out("Applied: " + ", ".join(f"{k}: {v}" for k, v in sorted(
        counts.items())) if counts else "Applied: nothing to delete")
    return counts


def _rows_to_process(picked_ids, limit):
    """The registrations this run works on: without a limit, every one
    not yet deleted (those whose deletes kept failing included); with
    one, the batches just registered first, then the waiting ones, at
    most *limit* in all."""
    from backend.models import AnthropicBatchDeletion as R
    from backend.utils.anthropic_batch_deletion import due_rows
    if not limit:
        return due_rows(limit=None, backed_off_too=True)
    rows = []
    for start in range(0, len(picked_ids), COMMIT_EVERY):
        chunk = picked_ids[start:start + COMMIT_EVERY]
        rows += R.query.filter(R.batch_id.in_(chunk),
                               R.deleted_at.is_(None)).all()
    taken = {r.batch_id for r in rows}
    rows += [r for r in due_rows(limit=limit, backed_off_too=True)
             if r.batch_id not in taken]
    return rows[:limit]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="register and delete (default: dry run)")
    ap.add_argument("--no-list", action="store_true",
                    help="skip listing the key's batches at Anthropic")
    ap.add_argument("--limit", type=int, default=None,
                    help="register and delete at most this many batches "
                         "in this run (the others are not registered, so "
                         "the hourly sweep does not take them either)")
    ap.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                    help=f"seconds between batches (default "
                         f"{PAUSE_SECONDS})")
    args = ap.parse_args()

    from backend import create_app
    app = create_app()
    with app.app_context():
        run(apply=args.apply, list_at_anthropic=not args.no_list,
            limit=args.limit, pause=args.pause)


if __name__ == "__main__":
    main()
