"""Store the tool records of earlier replies the way new ones are stored.

A reply's tool record (Node.tool_calls_meta: which tools the model
called, with what input, and the server's notes on the reply) is stored
encrypted from the deploy that brought backend/utils/tool_record.py:
each entry's name and status as they are, the whole record encrypted
beside them. Replies written before that deploy hold the record as a
plain JSON list. The app reads both, so nothing depends on this script
having run; it brings the earlier rows to the same stored form.

What it does: walks the node rows whose record is in the earlier form,
in id order, --batch-size at a time (default 200), and rewrites each
record in the stored form. One new data key per batch (one key-service
call per batch, like an import), not one per row. Each UPDATE checks
that the record is still the value that was read, so a reply the app
changes while this runs keeps the app's write (and is picked up on the
next pass, already in the new form). ``updated_at`` is not touched.

It reads the tool record column only: no entry text is loaded or
decrypted. It prints counts and ids, never a record.

Memory: one batch of records at a time. A pause between batches
(--pause, default 0.2 s) keeps the load on the database low.

Runbook (production, from the repo root, in the app's virtualenv, after
the deploy that contains this file):

    python backend/scripts/store_tool_records_encrypted.py            # dry run: counts only
    python backend/scripts/store_tool_records_encrypted.py --apply
    python backend/scripts/store_tool_records_encrypted.py --apply --after-id 120000   # continue after a stop

Stopping it (Ctrl-C) is safe: finished batches stay, and a rerun
continues with what is left (--after-id only saves the scan of the
converted part). Verify after: the dry run reports 0 records in the
earlier form.

To go back (before running an older version of the app, which reads the
earlier form only):

    python backend/scripts/store_tool_records_encrypted.py --undo            # dry run
    python backend/scripts/store_tool_records_encrypted.py --undo --apply

That one decrypts each record (one key-service call per data key, so
per batch converted here and per reply written since).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from backend.extensions import db  # noqa: E402
from backend.models import Node  # noqa: E402
from backend.utils import encryption, tool_record  # noqa: E402

# Rows per batch and per data key. Small on purpose: the production
# machine has 4 GB, and a batch is one transaction.
BATCH_SIZE = 200
PAUSE_SECONDS = 0.2

# The stored form is a JSON object, the earlier form a JSON list.
_STORED_FORM = '{%'


def _has_record():
    return [Node.tool_calls_meta.isnot(None), Node.tool_calls_meta != ""]


def survey():
    """(records in the earlier form, records in the stored form)."""
    earlier = db.session.query(Node.id).filter(
        *_has_record(), ~Node.tool_calls_meta.like(_STORED_FORM)).count()
    stored = db.session.query(Node.id).filter(
        *_has_record(), Node.tool_calls_meta.like(_STORED_FORM)).count()
    return earlier, stored


def _batches(condition, after_id, batch_size, limit):
    last_id, seen = after_id, 0
    while limit is None or seen < limit:
        size = batch_size if limit is None else min(batch_size, limit - seen)
        rows = (db.session.query(Node.id, Node.tool_calls_meta)
                .filter(*_has_record(), condition, Node.id > last_id)
                .order_by(Node.id.asc()).limit(size).all())
        if not rows:
            return
        last_id = rows[-1][0]
        seen += len(rows)
        yield rows


def _write(node_id, old, new):
    """Replace the record when it is still *old*. True when it was."""
    return bool(
        Node.query.filter(Node.id == node_id, Node.tool_calls_meta == old)
        .update({Node.tool_calls_meta: new,
                 Node.updated_at: Node.updated_at},
                synchronize_session=False))


def convert(apply=False, batch_size=BATCH_SIZE, after_id=0, limit=None,
            pause=PAUSE_SECONDS, report=None):
    """Bring earlier records to the stored form. Returns counts:
    {"found", "converted", "changed_meanwhile", "last_id"}."""
    counts = {"found": 0, "converted": 0, "changed_meanwhile": 0,
              "last_id": after_id}
    condition = ~Node.tool_calls_meta.like(_STORED_FORM)
    for rows in _batches(condition, after_id, batch_size, limit):
        counts["found"] += len(rows)
        if apply:
            key = encryption.new_dek()   # one key-service call per batch
            for node_id, old in rows:
                new = tool_record.store(old, dek=key)
                if new != old and _write(node_id, old, new):
                    counts["converted"] += 1
                else:
                    counts["changed_meanwhile"] += 1
            db.session.commit()
        counts["last_id"] = rows[-1][0]
        if report:
            report(counts)
        if apply and pause:
            time.sleep(pause)
    return counts


def undo(apply=False, batch_size=BATCH_SIZE, after_id=0, limit=None,
         pause=PAUSE_SECONDS, report=None):
    """Bring stored-form records back to the earlier form. Same counts."""
    counts = {"found": 0, "converted": 0, "changed_meanwhile": 0,
              "last_id": after_id}
    condition = Node.tool_calls_meta.like(_STORED_FORM)
    for rows in _batches(condition, after_id, batch_size, limit):
        rows = [r for r in rows if tool_record.is_stored_form(r[1])]
        counts["found"] += len(rows)
        if apply:
            encryption.prefetch_deks(
                tool_record.sealed_part(old) for _, old in rows)
            for node_id, old in rows:
                if _write(node_id, old, tool_record.read(old)):
                    counts["converted"] += 1
                else:
                    counts["changed_meanwhile"] += 1
            db.session.commit()
        if rows:
            counts["last_id"] = rows[-1][0]
        if report:
            report(counts)
        if apply and pause:
            time.sleep(pause)
    return counts


def _print_progress(counts):
    print(f"  through node id {counts['last_id']}: "
          f"{counts['found']} found, {counts['converted']} written, "
          f"{counts['changed_meanwhile']} left (changed meanwhile)",
          flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Store earlier replies' tool records in the "
                    "encrypted form new replies use")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only counts")
    parser.add_argument("--undo", action="store_true",
                        help="the other direction: back to the earlier "
                             "form")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--after-id", type=int, default=0,
                        help="continue after this node id")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many records")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                        help="seconds between batches")
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        earlier, stored = survey()
        print(f"tool records in the earlier form: {earlier}")
        print(f"tool records in the stored form:  {stored}")
        if not encryption.is_encryption_enabled() and not (
                args.undo and encryption.reads_decrypt()):
            # Off: nothing is stored encrypted, so there is nothing to
            # convert. --undo still runs where a key name is set: what
            # was stored encrypted earlier is read with it.
            print("Encryption is off here: records are stored as they "
                  "are. Nothing to do.")
            return 0
        run = undo if args.undo else convert
        counts = run(apply=args.apply, batch_size=args.batch_size,
                     after_id=args.after_id, limit=args.limit,
                     pause=args.pause, report=_print_progress)
        verb = "written" if args.apply else "would be written"
        print(f"{counts['converted'] if args.apply else counts['found']} "
              f"{verb}; {counts['changed_meanwhile']} left because the "
              f"app changed them meanwhile; last node id "
              f"{counts['last_id']}")
        if not args.apply:
            print("Dry run. Add --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
