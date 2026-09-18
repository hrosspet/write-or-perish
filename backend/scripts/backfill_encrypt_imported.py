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
expunged between chunks so memory stays flat. Rows with empty content are
not pending — there is nothing to encrypt — and a chunk where nothing
could be encrypted stops the run rather than re-selecting forever.

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
from backend.utils.encryption import (  # noqa: E402
    is_encrypted, is_encryption_enabled)


def pending_query():
    """Imported, non-public rows whose stored content is still plaintext.

    Empty content is excluded: there is nothing to encrypt, and both
    ``set_privacy_level`` and ``encrypt_content`` return early on a falsy
    value, so such a row could never leave this set — it would be
    re-selected every chunk and an unbounded run would never finish.
    """
    return Node.query.filter(
        Node.source_key.isnot(None),
        Node.content.isnot(None),
        Node.content != "",
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
        encrypted = [node for node in rows if is_encrypted(node.content)]
        if not encrypted:
            # Nothing here could be encrypted, so re-querying returns the
            # same rows forever: bounded runs report every pass as
            # progress, unbounded ones never finish. Stop and name them.
            db.session.rollback()
            log(f"  no progress: {len(rows)} row(s) from node {rows[0].id} "
                f"have nothing to encrypt — stopping, none written")
            break
        db.session.commit()
        done += len(encrypted)
        log(f"  encrypted {done} (through node {encrypted[-1].id})")
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
