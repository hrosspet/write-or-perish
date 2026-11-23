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

What it does, one entry at a time, each batch in one transaction:

- A living prompt entry whose level is not private (public or circles),
  last changed before --before, is set to private through
  Node.set_privacy_level, which also encrypts the text an entry holds
  itself (one edited inside its thread). An entry from before the
  ``prompt_key`` column gets the key of the prompt it links.
  ``updated_at`` is not touched.
- When the entry is the root of a thread and was PUBLIC, the first
  public message of its owner directly under it gets a permalink slug
  in the same transaction, made from the message's own first line,
  unless a living public message under it has one already: the public
  pages list a thread with a private prompt root from the message that
  has the slug (utils/public_threads). The root's pin on the public
  profile moves to that message. A slug the root had is dropped, never
  carried over: it was made from the prompt's text, and the root's own
  address stops answering once it is private in any case. Only public
  roots this run sets private get a slug: a root that was private
  before (every Voice and Glean root, a Text root whose message alone
  was made public) is not looked at, and a root stored at the circles
  level is set private without one, so nothing that was read by its
  link only becomes listed.

Left as they are, and counted:

- an entry with another person's public reply directly under it: with
  the entry private that reply would no longer show in the thread;
- entries of an account that is being deleted;
- entries last changed at or after --before (their ids are printed).

It cannot tell an entry its owner has shared on purpose from one that
became public with its thread: before the deploy there was no such
choice to make. After it there is. --before <time> is therefore
required with --apply: give the time of the deploy, and entries their
owners changed since are left alone. The time is UTC unless it carries
an offset or Z; with --apply it may not lie in the future. The dry run
takes "now" when it is not given, and says so.

What people notice: on a public Text-mode thread the prompt entry is no
longer shown to anyone but its owner; the thread starts at its first
message, in the listings too. A visitor who opens the root's own
addresses (/node/<root id>, a permalink the root had) gets "not found";
the message's own address and its permalink work.

It prints ids and counts only: no text, no slug. Dry run by default (it
takes no row locks). A second run finds nothing to do. Stopping it
(Ctrl-C) is safe: finished batches stay, each complete.

Memory: one batch of rows at a time (--batch-size, default 200), a
pause between batches (--pause, default 0.2 s). With encryption on, one
key-service call per entry that holds text of its own.

Runbook (production, from the repo root, in the app's virtualenv, after
the deploy that contains this file):

    python backend/scripts/set_prompt_entries_private.py --before 2026-11-24T10:00:00            # dry run: ids and counts
    python backend/scripts/set_prompt_entries_private.py --before 2026-11-24T10:00:00 --apply

There is no undo pass: which entries were public is printed by the dry
run and by --apply (keep the output); an owner sets an entry public
again in the entry's own edit form.
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.getcwd())

from sqlalchemy import func, or_  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    Node, NodeContextArtifact, User, UserPrompt,
)

# Rows per batch. Small on purpose: the production machine has 4 GB, and
# a batch is one transaction.
BATCH_SIZE = 200
PAUSE_SECONDS = 0.2
PRIVATE = "private"
PUBLIC = "public"


def _owner_id():
    return func.coalesce(Node.human_owner_id, Node.user_id)


def _prompt_link():
    """EXISTS: the node links a prompt version (how an entry from before
    the prompt_key column is recognised)."""
    return db.session.query(NodeContextArtifact.id).filter(
        NodeContextArtifact.node_id == Node.id,
        NodeContextArtifact.artifact_type == "prompt").exists()


def _shared_entries(before):
    """Conditions: a living prompt entry whose level is not private,
    last changed before *before*."""
    return [
        Node.deleted_at.is_(None),
        Node.privacy_level != PRIVATE,
        or_(Node.prompt_key.isnot(None), _prompt_link()),
        func.coalesce(Node.updated_at, Node.created_at) < before,
    ]


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


def _owners_being_deleted(node_ids):
    """Ids of the entries whose owner's account is being deleted."""
    return {i for (i,) in db.session.query(Node.id).join(
        User, User.id == _owner_id()).filter(
            Node.id.in_(node_ids), User.deleted_at.isnot(None)).all()}


def _children(node_ids):
    """{entry id: the nodes directly under it, oldest first}, the
    columns the decisions read (no text)."""
    under = {}
    for row in db.session.query(
            Node.id, Node.parent_id, Node.human_owner_id, Node.user_id,
            Node.public_slug, Node.privacy_level, Node.deleted_at,
            Node.pinned_at,
    ).filter(Node.parent_id.in_(node_ids)).order_by(Node.id.asc()).all():
        under.setdefault(row.parent_id, []).append(row)
    return under


def _owner(row):
    return row.human_owner_id or row.user_id


def _new_counts():
    return {"found": 0, "set_private": 0, "by_level": Counter(),
            "stamped": 0, "changed_meanwhile": 0,
            "others_reply": [], "account_being_deleted": 0,
            "changed_since": [], "slugs_given": 0, "root_slugs_dropped": 0,
            "pins_moved": 0, "circles_roots_without_slug": 0,
            "ids": [], "slug_ids": []}


def run(before, apply=False, batch_size=BATCH_SIZE, pause=PAUSE_SECONDS,
        report=None):
    """Set the entries private and give their threads' first public
    messages a slug (see the module text). Returns the counts; without
    *apply* nothing is written and they say what would be."""
    from backend.utils.slugs import generate_unique_public_slug
    counts = _new_counts()
    # Entries that would be taken but for their time: their owners'.
    since = _shared_entries(before)
    since[-1] = func.coalesce(Node.updated_at, Node.created_at) >= before
    for ids in _id_batches(since, batch_size):
        counts["changed_since"].extend(ids)
    for ids in _id_batches(_shared_entries(before), batch_size):
        counts["found"] += len(ids)
        keys = _linked_keys(ids)
        leaving = _owners_being_deleted(ids)
        under = _children(ids)
        query = Node.query.filter(Node.id.in_(ids)).order_by(Node.id.asc())
        if apply:
            query = query.with_for_update().populate_existing()
        done, slugged = [], []
        for node in query.all():
            if (node.privacy_level == PRIVATE or node.deleted_at is not None
                    or (node.updated_at or node.created_at) >= before):
                counts["changed_meanwhile"] += 1
                continue
            if node.id in leaving:
                counts["account_being_deleted"] += 1
                continue
            children = under.get(node.id, [])
            owner = _owner(node)
            if any(c.privacy_level == PUBLIC and c.deleted_at is None
                   and _owner(c) != owner for c in children):
                counts["others_reply"].append(node.id)
                continue
            done.append(node.id)
            counts["by_level"][node.privacy_level] += 1
            stamp = not node.prompt_key and keys.get(node.id)
            counts["stamped"] += 1 if stamp else 0
            # The thread's first public message, where the root was
            # public and no living public message under it has a slug.
            readable = [c for c in children if c.privacy_level == PUBLIC
                        and c.deleted_at is None]
            first = None
            if (node.parent_id is None
                    and not any(c.public_slug for c in readable)):
                first = next((c for c in readable if _owner(c) == owner),
                             None)
            if first is not None and node.privacy_level != PUBLIC:
                counts["circles_roots_without_slug"] += 1
                first = None
            move_pin = (first is not None and node.pinned_at is not None
                        and first.pinned_at is None)
            if first is not None:
                slugged.append(first.id)
                counts["slugs_given"] += 1
                counts["pins_moved"] += 1 if move_pin else 0
            if node.public_slug:
                counts["root_slugs_dropped"] += 1
            if not apply:
                continue
            if stamp:
                node.prompt_key = keys[node.id]
            pin = (node.pinned_at, node.pinned_by)
            node.set_privacy_level(PRIVATE)
            # A private node is not pinned (as the edit route does).
            node.pinned_at = None
            node.pinned_by = None
            # Its slug was made from the prompt's text: dropped.
            node.public_slug = None
            # Named in the UPDATE with its own value, so the column's
            # onupdate does not move it.
            flag_modified(node, "updated_at")
            counts["set_private"] += 1
            if first is not None:
                db.session.flush()
                message = db.session.get(Node, first.id)
                message.public_slug = generate_unique_public_slug(
                    owner, message.get_content() or "")
                if move_pin:
                    message.pinned_at, message.pinned_by = pin
                flag_modified(message, "updated_at")
                db.session.flush()
        if apply:
            db.session.commit()
            _drop_cached_pages(done + slugged)
        else:
            db.session.rollback()
        counts["ids"].extend(done)
        counts["slug_ids"].extend(slugged)
        if report:
            report("set private" if apply else "would be set private", done)
            report("first public message given the thread's permalink"
                   if apply else
                   "first public message that would get the permalink",
                   slugged)
        if apply and pause:
            time.sleep(pause)
    return counts


def _drop_cached_pages(node_ids):
    """The public pages these nodes are shown on leave the page cache
    (never raises: the change is committed)."""
    if node_ids:
        from backend.utils.public_cache import invalidate_for_nodes
        invalidate_for_nodes(node_ids)


def _print_ids(what, ids):
    if ids:
        print(f"  {what}: node ids {', '.join(str(i) for i in ids)}",
              flush=True)


def _utc_time(value):
    """An ISO time as a naive UTC datetime (the form the rows hold): Z
    or an offset is converted, a time without one is taken as UTC."""
    from datetime import timezone
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a time: {value!r} (e.g. 2026-11-24T10:00:00)")
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Set stored prompt entries that are public or shared "
                    "with circles to private, and give the first public "
                    "message of their threads the permalink")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only reports")
    parser.add_argument("--before", type=_utc_time, default=None,
                        help="only entries last changed before this "
                             "time, the time of the deploy (e.g. "
                             "2026-11-24T10:00:00; UTC unless it has an "
                             "offset or Z); required with --apply")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                        help="seconds between batches")
    args = parser.parse_args(argv)
    if args.apply and args.before is None:
        parser.error("--apply needs --before <UTC time of the deploy>: "
                     "entries changed since then are their owners' choice")
    before = args.before
    if args.apply and before > datetime.utcnow():
        parser.error("--before lies in the future: give the time of the "
                     "deploy, in UTC")
    if before is None:
        before = datetime.utcnow()
        print(f"No --before given: counting entries last changed before "
              f"now ({before.isoformat(timespec='seconds')} UTC).")

    from backend import create_app
    app = create_app()
    with app.app_context():
        counts = run(before, apply=args.apply, batch_size=args.batch_size,
                     pause=args.pause, report=_print_ids)
        will = "" if args.apply else "would be "
        levels = ", ".join(f"{level}: {n}" for level, n
                           in sorted(counts["by_level"].items())) or "none"
        print(f"prompt entries {will}set to private: "
              f"{len(counts['ids'])} ({levels}); "
              f"{counts['stamped']} of them {will}given their prompt key")
        print(f"permalinks {will}given to a thread's first public "
              f"message, made from the message: {counts['slugs_given']}")
        print(f"permalinks of prompt roots {will}dropped (their "
              f"addresses stop answering): {counts['root_slugs_dropped']}")
        print(f"pins {will}moved from the root to that message: "
              f"{counts['pins_moved']}")
        print(f"roots at the circles level {will}set private without a "
              f"permalink for their public message (read by its link, "
              f"as before): {counts['circles_roots_without_slug']}")
        print(f"left public because another person's public reply hangs "
              f"directly under them: {len(counts['others_reply'])}")
        _print_ids("left public", counts["others_reply"])
        print(f"left as they are because they changed at or after "
              f"--before: {len(counts['changed_since'])}")
        _print_ids("changed at or after --before", counts["changed_since"])
        print(f"left as they are: {counts['account_being_deleted']} of "
              f"accounts being deleted; {counts['changed_meanwhile']} "
              "changed while this ran")
        if not args.apply:
            print("Dry run. Add --apply (with --before) to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
