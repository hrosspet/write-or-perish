"""Encrypt pre-existing plaintext imported nodes on prod (#265).

Before #262 the ChatGPT/Claude/Markdown/Twitter importers wrote
``Node(content=plaintext)`` and bypassed ``Node.set_content()``, so their
rows sit in plaintext at rest. New imports are correct; this one-time
backfill re-encrypts the rows that predate the fix. Only NON-public rows
are touched: public nodes are meant to be plaintext (#257).

Each row goes through the model (``set_privacy_level`` on its current
level, which encrypts plaintext content and leaves encrypted content
alone), so the #257 invariant and the persist-time guard apply. One KMS
wrap per node — run it OFF the request path and off-box or nice'd
(per the 4 GB prod VM rule), with the KMS retry (#264) deployed.

Resumable: the ``content NOT LIKE 'ENC:%'`` filter means a rerun skips
rows an earlier run already encrypted. Rows are streamed in id order in
chunks of --chunk (default 200), committed per chunk, and the session is
expunged between chunks so memory stays flat.

    cd /path/to/write-or-perish
    python backend/scripts/backfill_encrypt_imported.py --dry-run
    python backend/scripts/backfill_encrypt_imported.py --limit 500   # staged
    python backend/scripts/backfill_encrypt_imported.py               # the rest

Verify after:  the --dry-run count reads 0.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from sqlalchemy import func  # noqa: E402

from backend import create_app, db  # noqa: E402
from backend.models import Node  # noqa: E402
from backend.utils.encryption import is_encryption_enabled  # noqa: E402


def pending_query():
    """Imported, non-public rows whose stored content is still plaintext."""
    return Node.query.filter(
        Node.source_key.isnot(None),
        Node.content.isnot(None),
        Node.content.notlike("ENC:%"),
        Node.privacy_level != "public",
    )


def count_pending():
    q = pending_query()
    total = q.with_entities(func.count(Node.id)).scalar() or 0
    users = q.with_entities(
        func.count(func.distinct(Node.human_owner_id))).scalar() or 0
    return total, users


def encrypt_pending(limit=None, chunk=200, log=print):
    """Encrypt pending rows in id order; returns the number encrypted.

    Re-queries after every chunk instead of paginating: each committed
    chunk drops out of the pending set on its own, so ``first chunk`` is
    always the next unencrypted rows — no offsets, no drift on rerun.
    """
    done = 0
    while limit is None or done < limit:
        size = chunk if limit is None else min(chunk, limit - done)
        rows = pending_query().order_by(Node.id.asc()).limit(size).all()
        if not rows:
            break
        for node in rows:
            # Same level, same content: the only effect on a plaintext
            # non-public row is the encryption (set_privacy_level's
            # "leaving public encrypts" branch).
            node.set_privacy_level(node.privacy_level)
        db.session.commit()
        done += len(rows)
        log(f"  encrypted {done} (through node {rows[-1].id})")
        db.session.expunge_all()
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Encrypt pre-existing plaintext imported nodes (#265)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count only, no writes")
    parser.add_argument("--limit", type=int, default=None,
                        help="encrypt at most N rows this run (staged runs)")
    parser.add_argument("--chunk", type=int, default=200,
                        help="rows per commit (default 200)")
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        total, users = count_pending()
        print(f"plaintext non-public imported nodes: {total} "
              f"across {users} user(s)")
        if args.dry_run or not total:
            return 0
        if not is_encryption_enabled():
            print("ENCRYPTION is disabled in this environment — refusing to "
                  "run (set_content would store plaintext again).")
            return 2
        started = time.time()
        done = encrypt_pending(limit=args.limit, chunk=args.chunk)
        left, _ = count_pending()
        print(f"encrypted {done} node(s) in {time.time() - started:.0f}s; "
              f"{left} still pending")
        return 0


if __name__ == "__main__":
    sys.exit(main())
