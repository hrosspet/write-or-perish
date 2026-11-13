"""Lower stored Train to Chat on entries fetched through the X API (#689).

X's Developer Agreement forbids using the X API or X Content to train a
foundation or frontier model. From #689 on, an entry Loore fetched
through the X API (``Node.provenance = 'prefill_x'``) is never at Train:
imports create it at Chat at most, no route raises it, and the training
licence reads it as Chat whatever is stored. Entries stored at Train
before that rule are therefore already kept off the training key; this
command lowers the stored value too, so the editor and the counts show
what the licence does.

What it changes (with --apply):
  1. Node rows with provenance 'prefill_x' and ai_usage 'train' ->
     'chat'.
  2. Older imported tweets with no provenance at all (``origin =
     'twitter'``, ``provenance IS NULL``; the column exists since
     2026-09-21) in accounts that had an X API pre-fill (an
     ``APICostLog`` row of request_type 'x_prefill'): provenance ->
     'prefill_x', Train -> Chat, for
     - every tweet listed in the account's X API dump: each pre-fill
       wrote ``<data>/x-api/<handle>-<stamp>.jsonl`` (tasks/imports
       x_api_dump_path) naming the account (``for_user_id``) and every
       post it fetched (``id``), so the dump settles which tweets came
       through the X API. The one case it can't rule out: the person
       uploaded their own archive before the pre-fill, which then
       skipped the tweet as already imported; the counts say how many
       listed tweets are in accounts someone has signed into;
     - in an account with no dump that nobody has ever signed into
       (utils/activity.sign_in_status "never"), every unlabelled tweet:
       it can't hold an own upload, so they came from a pre-fill, the X
       API's or the Community Archive's, which the record can't tell
       apart; they are read the safe way.
``updated_at`` is kept. Nothing else.

What it reports, never changes on its own: unlabelled tweets in accounts
with no dump that someone has signed into (or whose sign-in record is
"unknown"), per account. Such a tweet may be the person's own upload,
which may be Train. ``--label-user ID`` (repeatable) labels one such
account, for an owner known not to have uploaded their own archive
before 2026-09-22; it refuses an account with no X API pre-fill on
record.

Reads the dumps' ids only and the database's metadata only: never loads
or decrypts Loore content. UPDATEs go in batches of --batch-size
(default 500), and each re-checks its condition, so a row changed while
this runs is left alone. Rerunnable; a second run finds nothing to
change.

Runbook (prod, from the repo root, in the app's virtualenv):

    python backend/scripts/cap_x_api_train.py            # dry run: counts only
    python backend/scripts/cap_x_api_train.py --apply

Verify after: the dry run reports 0 entries at Train and nothing to
label.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.getcwd())

from sqlalchemy import distinct, func, select  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import APICostLog, Node  # noqa: E402
from backend.utils.privacy import (  # noqa: E402
    X_API_PREFILL_REQUEST, X_API_PROVENANCES,
)

X_API = X_API_PROVENANCES[0]


def _x_prefilled_owners():
    """Ids of the accounts that had an X API pre-fill (its cost row)."""
    return select(APICostLog.user_id).where(
        APICostLog.request_type == X_API_PREFILL_REQUEST,
        APICostLog.user_id.isnot(None)).distinct()


def x_prefilled(user_id):
    """Whether *user_id* has an X API pre-fill on record."""
    return db.session.query(APICostLog.id).filter(
        APICostLog.user_id == user_id,
        APICostLog.request_type == X_API_PREFILL_REQUEST).first() is not None


def default_dump_dir():
    """Where the X API pre-fill wrote its dumps (tasks/imports)."""
    from backend.utils.twitter_archive import STASH_ROOT
    return STASH_ROOT.parent / "x-api"


def read_dumps(folder):
    """``{user_id: {tweet id}}`` from the X API dumps in *folder*: each
    file's header names the account (``for_user_id``), every other line
    is one fetched post; only its ``id`` is kept. A missing folder reads
    as no dumps."""
    found = {}
    folder = Path(folder)
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            try:
                header = json.loads(f.readline() or "{}")
            except ValueError:
                continue
            user_id = header.get("for_user_id")
            if user_id is None:
                continue
            ids = found.setdefault(int(user_id), set())
            for line in f:
                try:
                    post = json.loads(line)
                except ValueError:
                    continue
                if isinstance(post, dict) and post.get("id"):
                    ids.add(str(post["id"]))
    return found


def _owner():
    return func.coalesce(Node.human_owner_id, Node.user_id)


def _unlabelled(user_id=None):
    clauses = [Node.origin == "twitter", Node.provenance.is_(None)]
    if user_id is not None:
        clauses.append(_owner() == user_id)
    return clauses


def survey():
    """Counts, by ai_usage: the X API entries, and the unlabelled tweets
    of accounts with an X API pre-fill ({ai_usage: (rows, accounts)})."""
    owner = _owner()
    labelled = dict(
        db.session.query(Node.ai_usage, func.count(Node.id))
        .filter(Node.provenance == X_API)
        .group_by(Node.ai_usage).all())
    unlabelled = {
        usage: (n_rows, n_owners) for usage, n_rows, n_owners in
        db.session.query(Node.ai_usage, func.count(Node.id),
                         func.count(distinct(owner)))
        .filter(*_unlabelled(), owner.in_(_x_prefilled_owners()))
        .group_by(Node.ai_usage).all()}
    return labelled, unlabelled


def unlabelled_by_account():
    """Per account with an X API pre-fill and unlabelled tweets:
    ``(user_id, sign-in status, {ai_usage: rows})``. Metadata only. An
    account nobody has signed into ("never", utils/activity) can't have
    uploaded its own archive, so its unlabelled tweets all came from a
    pre-fill (the X API's, or the Community Archive's)."""
    from backend.models import User
    from backend.utils.activity import sign_in_status
    owner = _owner()
    counts = {}
    for user_id, usage, n in (
            db.session.query(owner, Node.ai_usage, func.count(Node.id))
            .filter(*_unlabelled(), owner.in_(_x_prefilled_owners()))
            .group_by(owner, Node.ai_usage).all()):
        counts.setdefault(user_id, {})[usage] = n
    return [(user_id, sign_in_status(db.session.get(User, user_id)),
             counts[user_id]) for user_id in sorted(counts)]


def _batches(query, batch_size):
    """Ids of *query* (a Node.id query), in id order, a batch at a time."""
    last_id = 0
    while True:
        ids = [row_id for (row_id,) in
               query.filter(Node.id > last_id)
               .order_by(Node.id.asc()).limit(batch_size)]
        if not ids:
            return
        last_id = ids[-1]
        yield ids


def cap(apply=False, batch_size=500):
    """Lower stored Train to Chat on the X API entries. Returns the count
    (changed with *apply*, to change without)."""
    n = 0
    query = db.session.query(Node.id).filter(
        Node.provenance == X_API, Node.ai_usage == "train")
    for ids in _batches(query, batch_size):
        if apply:
            n += (Node.query
                  .filter(Node.id.in_(ids), Node.provenance == X_API,
                          Node.ai_usage == "train")
                  .update({Node.ai_usage: "chat",
                           Node.updated_at: Node.updated_at},
                          synchronize_session=False))
            db.session.commit()
        else:
            n += len(ids)
    return n


def label_user(user_id, apply=False, batch_size=500, tweet_ids=None):
    """Stamp provenance 'prefill_x' on *user_id*'s unlabelled tweets (only
    those whose id is in *tweet_ids*, when given: the account's X API
    dump) and lower their Train to Chat. Returns a Counter by the old
    ai_usage."""
    counts = Counter()
    keys = (None if tweet_ids is None
            else sorted(f"twitter:{i}" for i in tweet_ids))
    key_chunks = [None] if keys is None else [
        keys[i:i + 500] for i in range(0, len(keys), 500)]
    for chunk in key_chunks:
        clauses = list(_unlabelled(user_id))
        if chunk is not None:
            clauses.append(Node.source_key.in_(chunk))
        for usage in ("none", "chat", "train"):
            query = db.session.query(Node.id).filter(
                *clauses, Node.ai_usage == usage)
            for ids in _batches(query, batch_size):
                if apply:
                    values = {Node.provenance: X_API,
                              Node.updated_at: Node.updated_at}
                    if usage == "train":
                        values[Node.ai_usage] = "chat"
                    counts[usage] += (
                        Node.query.filter(Node.id.in_(ids), *clauses,
                                          Node.ai_usage == usage)
                        .update(values, synchronize_session=False))
                    db.session.commit()
                else:
                    counts[usage] += len(ids)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Lower stored Train to Chat on entries fetched through "
                    "the X API (#689)")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only reports")
    parser.add_argument("--label-user", type=int, action="append",
                        default=[], metavar="USER_ID",
                        help="also label this account's unlabelled tweets "
                             "as fetched through the X API (repeatable)")
    parser.add_argument("--dumps", metavar="DIR",
                        help="the X API dumps folder (default: data/x-api "
                             "next to the import stashes)")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        ok = run(apply=args.apply, label_users=args.label_user,
                 batch_size=args.batch_size,
                 dumps=read_dumps(args.dumps or default_dump_dir()))
        return 0 if ok else 1


def _labelled_line(user_id, why, counts, apply):
    return (f"account {user_id} ({why}): "
            f"{'labelled' if apply else 'would label'} "
            f"{sum(counts.values())} tweet(s) as fetched through the X API "
            f"({counts['train']} of them from Train to Chat)")


def run(apply=False, label_users=(), batch_size=500, out=print, dumps=None):
    """The whole command inside an app context: report, then lower and
    label (each only with *apply*). *dumps* is ``read_dumps``'s answer.
    Returns False when a --label-user account was refused."""
    dumps = dumps or {}
    labelled, unlabelled = survey()
    out("entries fetched through the X API, by AI usage:")
    for usage in sorted(labelled):
        out(f"  {usage!r}: {labelled[usage]}")
    if not labelled:
        out("  none")
    out("unlabelled tweets (no provenance) of accounts with an X API "
        "pre-fill, by AI usage:")
    for usage in sorted(unlabelled):
        n_rows, n_owners = unlabelled[usage]
        out(f"  {usage!r}: {n_rows} row(s) in {n_owners} account(s)")
    if not unlabelled:
        out("  none")
    accounts = unlabelled_by_account()
    for user_id, status, counts in accounts:
        out(f"  account {user_id} (sign-in: {status}; X API dump: "
            f"{'yes' if user_id in dumps else 'none'}): "
            + ", ".join(f"{u!r} {n}" for u, n in sorted(counts.items())))

    verb = "lowered" if apply else "would lower"
    n = cap(apply=apply, batch_size=batch_size)
    out(f"{verb} Train -> Chat on {n} X API entr{'y' if n == 1 else 'ies'}")

    ok = True
    for user_id in label_users:
        if not x_prefilled(user_id):
            out(f"account {user_id}: no X API pre-fill on record; "
                "--label-user refused, nothing labelled")
            ok = False
    listed_signed_in = 0
    left = []
    for user_id, status, _ in accounts:
        if user_id in dumps:
            counts = label_user(user_id, apply=apply, batch_size=batch_size,
                                tweet_ids=dumps[user_id])
            out(_labelled_line(user_id, "listed in its X API dump",
                               counts, apply))
            if status != "never":
                listed_signed_in += sum(counts.values())
        elif status == "never":
            counts = label_user(user_id, apply=apply, batch_size=batch_size)
            out(_labelled_line(user_id, "no dump; nobody has signed in",
                               counts, apply))
        elif user_id in label_users:
            counts = label_user(user_id, apply=apply, batch_size=batch_size)
            out(_labelled_line(user_id, "named with --label-user", counts,
                               apply))
        else:
            left.append(user_id)
    if listed_signed_in:
        out(f"of the tweets listed in dumps, {listed_signed_in} are in "
            "accounts someone has signed into: one the person had uploaded "
            "before the pre-fill would be among them (the record can't "
            "tell)")
    if left:
        out(f"left as they are (no X API dump, and signed in or no sign-in "
            f"record, so an own upload is possible): account(s) "
            f"{', '.join(map(str, left))}")
    if not apply:
        out("dry run — nothing written; rerun with --apply")
    return ok


if __name__ == "__main__":
    sys.exit(main())
