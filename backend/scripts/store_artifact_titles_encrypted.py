"""Store the titles and descriptions of earlier artifact versions the way
new ones are stored.

An artifact version's title and description (UserArtifact) are stored
encrypted, in the columns ``title_enc`` and ``description_enc``, from
the deploy that added those columns. Versions written before hold them
in the ``title`` and ``description`` columns as plain text. The app
reads both, so nothing depends on this script having run; it moves the
earlier rows' values to the encrypted columns and empties the plain
ones.

What it does: walks the artifact rows whose ``title_enc`` is empty, in
id order, --batch-size at a time (default 200). One new data key per
account for the run (one key-service call per account that has such
rows), not one per row: the artifacts list of an account then needs one
more key than its texts need, once per process. Each UPDATE checks that
``title_enc`` is still empty. No artifact text is loaded or decrypted.
It prints counts and ids, never a title.

Runbook (production, from the repo root, in the app's virtualenv, after
the deploy that contains this file and its migration):

    python backend/scripts/store_artifact_titles_encrypted.py            # dry run: counts only
    python backend/scripts/store_artifact_titles_encrypted.py --apply
    python backend/scripts/store_artifact_titles_encrypted.py --apply --after-id 5000   # continue after a stop

Stopping it (Ctrl-C) is safe: finished batches stay, and a rerun
continues with what is left. Verify after: the dry run reports 0 rows
with a plain title.

To go back (before running an older version of the app, which reads the
plain columns only):

    python backend/scripts/store_artifact_titles_encrypted.py --undo            # dry run
    python backend/scripts/store_artifact_titles_encrypted.py --undo --apply

That one decrypts each title and description (one key-service call per
data key) and writes them to the plain columns, cut to the columns'
lengths (128 and 255 characters, the lengths the app's writers keep to).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from backend.extensions import db  # noqa: E402
from backend.models import UserArtifact  # noqa: E402
from backend.utils import encryption  # noqa: E402

# Rows per batch. Small on purpose: the production machine has 4 GB, and
# a batch is one transaction.
BATCH_SIZE = 200
PAUSE_SECONDS = 0.2
# The plain columns' lengths.
TITLE_LENGTH = 128
DESCRIPTION_LENGTH = 255

A = UserArtifact


def survey():
    """(rows with a plain title, rows with an encrypted title)."""
    plain = db.session.query(A.id).filter(A.title_enc.is_(None)).count()
    stored = db.session.query(A.id).filter(A.title_enc.isnot(None)).count()
    return plain, stored


def _batches(columns, condition, after_id, batch_size, limit):
    last_id, seen = after_id, 0
    while limit is None or seen < limit:
        size = batch_size if limit is None else min(batch_size, limit - seen)
        rows = (db.session.query(*columns)
                .filter(condition, A.id > last_id)
                .order_by(A.id.asc()).limit(size).all())
        if not rows:
            return
        last_id = rows[-1][0]
        seen += len(rows)
        yield rows


def _counts(after_id):
    return {"found": 0, "converted": 0, "changed_meanwhile": 0,
            "accounts": 0, "last_id": after_id}


def convert(apply=False, batch_size=BATCH_SIZE, after_id=0, limit=None,
            pause=PAUSE_SECONDS, report=None):
    """Move plain titles and descriptions to the encrypted columns.
    Returns counts: {"found", "converted", "changed_meanwhile",
    "accounts", "last_id"}."""
    counts = _counts(after_id)
    keys = {}   # account id -> data key, for this run
    columns = (A.id, A.user_id, A.title_plain, A.description_plain)
    for rows in _batches(columns, A.title_enc.is_(None), after_id,
                         batch_size, limit):
        counts["found"] += len(rows)
        for row_id, user_id, title, description in rows:
            if user_id not in keys:
                # One key-service call per account.
                keys[user_id] = encryption.new_dek() if apply else None
                counts["accounts"] += 1
            if not apply:
                continue
            key = keys[user_id]
            values = {
                A.title_enc: encryption.encrypt_with_dek(title or "", key),
                A.title_plain: "",
            }
            if description is not None:
                values[A.description_enc] = encryption.encrypt_with_dek(
                    description, key)
                values[A.description_plain] = None
            written = (A.query.filter(A.id == row_id, A.title_enc.is_(None))
                       .update(values, synchronize_session=False))
            counts["converted" if written else "changed_meanwhile"] += 1
        if apply:
            db.session.commit()
        counts["last_id"] = rows[-1][0]
        if report:
            report(counts)
        if apply and pause:
            time.sleep(pause)
    return counts


def undo(apply=False, batch_size=BATCH_SIZE, after_id=0, limit=None,
         pause=PAUSE_SECONDS, report=None):
    """Move encrypted titles and descriptions back to the plain columns.
    Same counts."""
    counts = _counts(after_id)
    columns = (A.id, A.user_id, A.title_enc, A.description_enc)
    for rows in _batches(columns, A.title_enc.isnot(None), after_id,
                         batch_size, limit):
        counts["found"] += len(rows)
        if apply:
            encryption.prefetch_deks(
                [r[2] for r in rows] + [r[3] for r in rows])
            for row_id, _user_id, title_enc, description_enc in rows:
                values = {
                    A.title_plain: (encryption.decrypt_content(title_enc)
                                    or "")[:TITLE_LENGTH],
                    A.title_enc: None,
                }
                if description_enc is not None:
                    values[A.description_plain] = (
                        encryption.decrypt_content(description_enc)
                        or "")[:DESCRIPTION_LENGTH]
                    values[A.description_enc] = None
                written = (A.query.filter(A.id == row_id,
                                          A.title_enc == title_enc)
                           .update(values, synchronize_session=False))
                counts["converted" if written else "changed_meanwhile"] += 1
            db.session.commit()
        counts["last_id"] = rows[-1][0]
        if report:
            report(counts)
        if apply and pause:
            time.sleep(pause)
    return counts


def _print_progress(counts):
    print(f"  through artifact row id {counts['last_id']}: "
          f"{counts['found']} found, {counts['converted']} written, "
          f"{counts['changed_meanwhile']} left (changed meanwhile)",
          flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Store earlier artifact versions' titles and "
                    "descriptions in the encrypted columns")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only counts")
    parser.add_argument("--undo", action="store_true",
                        help="the other direction: back to the plain "
                             "columns")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--after-id", type=int, default=0,
                        help="continue after this artifact row id")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many rows")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                        help="seconds between batches")
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        plain, stored = survey()
        print(f"artifact rows with a plain title:      {plain}")
        print(f"artifact rows with an encrypted title: {stored}")
        if not encryption.is_encryption_enabled():
            print("Encryption is off here: titles are stored as they "
                  "are. Nothing to do.")
            return 0
        run = undo if args.undo else convert
        counts = run(apply=args.apply, batch_size=args.batch_size,
                     after_id=args.after_id, limit=args.limit,
                     pause=args.pause, report=_print_progress)
        verb = "written" if args.apply else "would be written"
        print(f"{counts['converted'] if args.apply else counts['found']} "
              f"rows {verb}; {counts['changed_meanwhile']} left because "
              f"the app changed them meanwhile; last row id "
              f"{counts['last_id']}")
        if not args.apply:
            print("Dry run. Add --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
