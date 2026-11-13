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
     ``APICostLog`` row of request_type 'x_prefill') and that nobody
     has ever signed into (utils/activity.sign_in_status "never"):
     provenance -> 'prefill_x', Train -> Chat. Such an account can't
     have uploaded its own archive, so these tweets came from a pre-fill:
     the X API's, or a Community Archive pre-fill's, which the record
     can't tell apart; they are read the safe way.
``updated_at`` is kept. Nothing else.

What it reports, never changes on its own: the same unlabelled tweets
in accounts someone has signed into (or whose sign-in record is
"unknown"), per account. Such a tweet may also come from the user's own
archive upload, which may be Train. ``--label-user ID`` (repeatable)
labels one such account, for an owner known not to have uploaded their
own archive before 2026-09-22.

Metadata only: selects ids and issues UPDATEs in batches of
--batch-size (default 500). Never loads or decrypts content. Each UPDATE
re-checks its condition, so a row changed while this runs is left alone.
Rerunnable; a second run finds nothing to change.

Runbook (prod, from the repo root, in the app's virtualenv):

    python backend/scripts/cap_x_api_train.py            # dry run: counts only
    python backend/scripts/cap_x_api_train.py --apply

Verify after: the dry run reports 0 entries at Train.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import distinct, func, select  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import APICostLog, Node  # noqa: E402
from backend.utils.privacy import X_API_PROVENANCES  # noqa: E402

X_API = X_API_PROVENANCES[0]
X_PREFILL_REQUEST = "x_prefill"


def _x_prefilled_owners():
    """Ids of the accounts that had an X API pre-fill (its cost row)."""
    return select(APICostLog.user_id).where(
        APICostLog.request_type == X_PREFILL_REQUEST,
        APICostLog.user_id.isnot(None)).distinct()


def survey():
    """Counts, by ai_usage: the X API entries, and the unlabelled tweets
    of accounts with an X API pre-fill ({ai_usage: (rows, accounts)})."""
    owner = func.coalesce(Node.human_owner_id, Node.user_id)
    labelled = dict(
        db.session.query(Node.ai_usage, func.count(Node.id))
        .filter(Node.provenance == X_API)
        .group_by(Node.ai_usage).all())
    unlabelled = {
        usage: (n_rows, n_owners) for usage, n_rows, n_owners in
        db.session.query(Node.ai_usage, func.count(Node.id),
                         func.count(distinct(owner)))
        .filter(Node.origin == "twitter", Node.provenance.is_(None),
                owner.in_(_x_prefilled_owners()))
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
    owner = func.coalesce(Node.human_owner_id, Node.user_id)
    counts = {}
    for user_id, usage, n in (
            db.session.query(owner, Node.ai_usage, func.count(Node.id))
            .filter(Node.origin == "twitter", Node.provenance.is_(None),
                    owner.in_(_x_prefilled_owners()))
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


def label_user(user_id, apply=False, batch_size=500):
    """Stamp provenance 'prefill_x' on *user_id*'s unlabelled tweets and
    lower their Train to Chat. Returns a Counter by the old ai_usage."""
    counts = Counter()
    owner = func.coalesce(Node.human_owner_id, Node.user_id)
    unlabelled = (Node.origin == "twitter", Node.provenance.is_(None),
                  owner == user_id)
    for usage in ("none", "chat", "train"):
        query = db.session.query(Node.id).filter(
            *unlabelled, Node.ai_usage == usage)
        for ids in _batches(query, batch_size):
            if apply:
                values = {Node.provenance: X_API,
                          Node.updated_at: Node.updated_at}
                if usage == "train":
                    values[Node.ai_usage] = "chat"
                counts[usage] += (
                    Node.query.filter(Node.id.in_(ids), *unlabelled,
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
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        run(apply=args.apply, label_users=args.label_user,
            batch_size=args.batch_size)
        return 0


def run(apply=False, label_users=(), batch_size=500, out=print):
    """The whole command inside an app context: report, then lower and
    label (each only with *apply*)."""
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
        out(f"  account {user_id} (sign-in: {status}): "
            + ", ".join(f"{u!r} {n}" for u, n in sorted(counts.items())))

    verb = "lowered" if apply else "would lower"
    n = cap(apply=apply, batch_size=batch_size)
    out(f"{verb} Train -> Chat on {n} X API entr{'y' if n == 1 else 'ies'}")
    never = [user_id for user_id, status, _ in accounts if status == "never"]
    for user_id in list(never) + [u for u in label_users if u not in never]:
        counts = label_user(user_id, apply=apply, batch_size=batch_size)
        why = ("nobody has signed in" if user_id in never
               else "named with --label-user")
        out(f"account {user_id} ({why}): "
            f"{'labelled' if apply else 'would label'} "
            f"{sum(counts.values())} tweet(s) as fetched through the X API "
            f"({counts['train']} of them from Train to Chat)")
    left = [user_id for user_id, status, _ in accounts
            if status != "never" and user_id not in label_users]
    if left:
        out(f"left as they are (signed in, or no sign-in record, so an "
            f"own upload is possible): account(s) "
            f"{', '.join(map(str, left))}")
    if not apply:
        out("dry run — nothing written; rerun with --apply")


if __name__ == "__main__":
    sys.exit(main())
