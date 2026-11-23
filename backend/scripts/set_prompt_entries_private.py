"""Set the prompt entries stored before the rule to private.

A prompt entry (the node a Text / Voice / Glean session starts with: its
owner's prompt text, with pinned versions of their profile and other
documents) is private from the deploy that brought
backend/utils/public_threads.py, whatever the level of the message
under it; its owner can share it by setting its level on the entry
itself. Entries created before that deploy took their message's level,
and a change of a thread's level "with all my replies" carried them
along. The deploy changes no stored row, and the app works with both, so
nothing depends on this script having run; it brings the earlier
entries to the same state. The owner decides whether to run it.

What it does, in two passes:

1. Levels. Every living prompt entry whose level is not private (public
   or circles) is set to private through Node.set_privacy_level, which
   also encrypts the text an entry holds itself (one edited inside its
   thread). An entry from before the ``prompt_key`` column gets the key
   of the prompt it links, so the public pages recognise it
   (utils/public_threads). ``updated_at`` is not touched. Each write
   checks that the level is still the one that was read.

2. Permalinks. In a thread whose root is a prompt entry that is not
   public, the public pages list the first public message as the top of
   the thread. Where no message under such a root has a permalink slug,
   the first public one gets it (the root's own slug when it had one, so
   an address that worked keeps working; a new one otherwise).

It cannot tell an entry its owner has shared on purpose from one that
became public with its thread: before the deploy there was no such
choice to make. After it there is, so run this once, soon after the
deploy; --before <UTC time> limits pass 1 to entries last changed before
that time (give the time of the deploy) when it runs later.

What people notice: on a public Text-mode thread the prompt entry is no
longer shown to anyone but its owner; the thread starts at its first
message, in the listings too.

It prints ids and counts only: no text, no slug. Dry run by default.
Safe to rerun: the second run finds nothing to do. Stopping it (Ctrl-C)
is safe: finished batches stay.

Memory: one batch of rows at a time (--batch-size, default 200), a
pause between batches (--pause, default 0.2 s). With encryption on, one
key-service call per entry that holds text of its own.

Runbook (production, from the repo root, in the app's virtualenv, after
the deploy that contains this file):

    python backend/scripts/set_prompt_entries_private.py            # dry run: ids and counts
    python backend/scripts/set_prompt_entries_private.py --apply
    python backend/scripts/set_prompt_entries_private.py --apply --before 2026-11-24T10:00:00

There is no undo pass: which entries were public is printed by the dry
run and by --apply (keep the output); an owner sets an entry public
again in the entry's own edit form.
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

from sqlalchemy import func, or_  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import Node, NodeContextArtifact, UserPrompt  # noqa: E402

# Rows per batch. Small on purpose: the production machine has 4 GB, and
# a batch is one transaction.
BATCH_SIZE = 200
PAUSE_SECONDS = 0.2
PRIVATE = "private"
PUBLIC = "public"


def _prompt_link():
    """EXISTS: the node links a prompt version (how an entry from before
    the prompt_key column is recognised)."""
    return db.session.query(NodeContextArtifact.id).filter(
        NodeContextArtifact.node_id == Node.id,
        NodeContextArtifact.artifact_type == "prompt").exists()


def _shared_entries(before=None):
    """Condition: a living prompt entry whose level is not private."""
    conditions = [
        Node.deleted_at.is_(None),
        Node.privacy_level != PRIVATE,
        or_(Node.prompt_key.isnot(None), _prompt_link()),
    ]
    if before is not None:
        conditions.append(
            func.coalesce(Node.updated_at, Node.created_at) < before)
    return conditions


def _id_batches(conditions, batch_size):
    last_id = 0
    while True:
        ids = [i for (i,) in db.session.query(Node.id).filter(
            *conditions, Node.id > last_id).order_by(
                Node.id.asc()).limit(batch_size).all()]
        if not ids:
            return
        last_id = ids[-1]
        yield ids


def _linked_keys(node_ids):
    """{node id: the prompt key of the prompt version it links}."""
    return dict(db.session.query(
        NodeContextArtifact.node_id, UserPrompt.prompt_key).join(
            UserPrompt, UserPrompt.id == NodeContextArtifact.artifact_id
        ).filter(NodeContextArtifact.artifact_type == "prompt",
                 NodeContextArtifact.node_id.in_(node_ids)).all())


def set_levels(apply=False, batch_size=BATCH_SIZE, pause=PAUSE_SECONDS,
               before=None, report=None):
    """Pass 1. Returns {"found", "set_private", "stamped",
    "changed_meanwhile", "ids", "root_ids"}; *ids* are the entries found
    (set, with apply), *root_ids* those of them that start a thread."""
    counts = {"found": 0, "set_private": 0, "stamped": 0,
              "changed_meanwhile": 0, "ids": [], "root_ids": []}
    for ids in _id_batches(_shared_entries(before), batch_size):
        counts["found"] += len(ids)
        keys = _linked_keys(ids)
        done = []
        for node in Node.query.filter(Node.id.in_(ids)).order_by(
                Node.id.asc()).with_for_update().populate_existing().all():
            if node.privacy_level == PRIVATE or node.deleted_at is not None:
                counts["changed_meanwhile"] += 1
                continue
            done.append(node.id)
            if node.parent_id is None:
                counts["root_ids"].append(node.id)
            if not node.prompt_key and keys.get(node.id):
                counts["stamped"] += 1
                if apply:
                    node.prompt_key = keys[node.id]
            if apply:
                node.set_privacy_level(PRIVATE)
                # A private node is not pinned (as the edit route does).
                node.pinned_at = None
                node.pinned_by = None
                # Named in the UPDATE with its own value, so the
                # column's onupdate does not move it.
                flag_modified(node, "updated_at")
                counts["set_private"] += 1
        if apply:
            db.session.commit()
            _drop_cached_pages(done)
        else:
            db.session.rollback()
        counts["ids"].extend(done)
        if report:
            report("levels", done)
        if apply and pause:
            time.sleep(pause)
    return counts


def _owner(row):
    return row.human_owner_id or row.user_id


def set_slugs(apply=False, batch_size=BATCH_SIZE, pause=PAUSE_SECONDS,
              also_root_ids=(), report=None):
    """Pass 2. *also_root_ids*: roots pass 1 would set private (a dry
    run has not written them). Returns {"threads", "slugged", "moved",
    "ids"}: threads looked at, messages given a slug, how many of those
    took the root's own slug, and the messages' ids."""
    from backend.utils.slugs import generate_unique_public_slug
    counts = {"threads": 0, "slugged": 0, "moved": 0, "ids": []}
    also = sorted(set(also_root_ids))
    roots = [Node.deleted_at.is_(None), Node.parent_id.is_(None),
             or_(Node.prompt_key.isnot(None), Node.id.in_(also))
             if also else Node.prompt_key.isnot(None),
             or_(Node.privacy_level != PUBLIC, Node.id.in_(also))
             if also else Node.privacy_level != PUBLIC]
    for root_ids in _id_batches(roots, batch_size):
        counts["threads"] += len(root_ids)
        root_rows = {r.id: r for r in db.session.query(
            Node.id, Node.human_owner_id, Node.user_id, Node.public_slug
        ).filter(Node.id.in_(root_ids)).all()}
        under = {}
        for row in db.session.query(
                Node.id, Node.parent_id, Node.human_owner_id, Node.user_id,
                Node.public_slug, Node.privacy_level, Node.deleted_at,
        ).filter(Node.parent_id.in_(root_ids)).order_by(Node.id.asc()).all():
            under.setdefault(row.parent_id, []).append(row)
        done = []
        for root_id, children in under.items():
            root = root_rows[root_id]
            if any(c.public_slug for c in children):
                continue
            first = next((c for c in children
                          if c.privacy_level == PUBLIC
                          and c.deleted_at is None
                          and _owner(c) == _owner(root)), None)
            if first is None:
                continue
            done.append(first.id)
            counts["slugged"] += 1
            counts["moved"] += 1 if root.public_slug else 0
            if not apply:
                continue
            message = db.session.get(Node, first.id)
            if root.public_slug:
                # The thread's address stays: the slug moves from the
                # prompt root to the message the public pages now show
                # at the top (unique per owner, so the root gives it up
                # first).
                slug = root.public_slug
                top = db.session.get(Node, root_id)
                top.public_slug = None
                flag_modified(top, "updated_at")
                db.session.flush()
            else:
                slug = generate_unique_public_slug(
                    _owner(first), message.get_content() or "")
            message.public_slug = slug
            flag_modified(message, "updated_at")
            db.session.flush()
        if apply:
            db.session.commit()
            _drop_cached_pages(done)
        counts["ids"].extend(done)
        if report and done:
            report("permalinks", done)
        if apply and pause:
            time.sleep(pause)
    return counts


def _drop_cached_pages(node_ids):
    """The public pages these nodes are shown on leave the page cache
    (never raises: the change is committed)."""
    if node_ids:
        from backend.utils.public_cache import invalidate_for_nodes
        invalidate_for_nodes(node_ids)


def run(apply=False, batch_size=BATCH_SIZE, pause=PAUSE_SECONDS,
        before=None, report=None):
    """Both passes. Returns (levels counts, permalink counts)."""
    levels = set_levels(apply=apply, batch_size=batch_size, pause=pause,
                        before=before, report=report)
    slugs = set_slugs(apply=apply, batch_size=batch_size, pause=pause,
                      also_root_ids=() if apply else levels["root_ids"],
                      report=report)
    return levels, slugs


def _print_ids(what, ids):
    print(f"  {what}: node ids {', '.join(str(i) for i in ids)}",
          flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Set stored prompt entries that are public or shared "
                    "with circles to private, and give the first public "
                    "message under a private prompt root a permalink")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only reports")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                        help="seconds between batches")
    parser.add_argument("--before", type=datetime.fromisoformat,
                        default=None,
                        help="only entries last changed before this UTC "
                             "time (e.g. 2026-11-24T10:00:00)")
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        levels, slugs = run(apply=args.apply, batch_size=args.batch_size,
                            pause=args.pause, before=args.before,
                            report=_print_ids)
        if args.apply:
            print(f"prompt entries set to private: {levels['set_private']} "
                  f"({levels['stamped']} of them got their prompt key; "
                  f"{levels['changed_meanwhile']} left because they "
                  "changed meanwhile)")
            print(f"first public messages given a permalink: "
                  f"{slugs['slugged']} ({slugs['moved']} took the prompt "
                  f"root's own); threads looked at: {slugs['threads']}")
        else:
            print(f"prompt entries that are public or shared with "
                  f"circles: {levels['found']} ({levels['stamped']} of "
                  "them without a prompt key)")
            print(f"first public messages that would get a permalink: "
                  f"{slugs['slugged']} ({slugs['moved']} would take the "
                  f"prompt root's own); threads looked at: "
                  f"{slugs['threads']}")
            print("Dry run. Add --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
