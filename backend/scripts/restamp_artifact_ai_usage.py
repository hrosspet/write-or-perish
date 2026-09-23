"""Restamp artifact rows that were marked 'chat' without anyone choosing
it (#326).

Until #326 two artifact writers stamped ai_usage='chat' whatever the
owner's default: the agentic update_artifact tool (memory, scratchpad,
intentions and custom kinds the model writes) and the Artifacts page
(PUT /api/artifacts/<kind>). Now every writer stamps the owner's
default_ai_usage, and the training key reads each artifact row that
reaches a payload. Left alone, a 'train' user's older memory — still
'chat' — would take every one of their agentic threads off the training
key until the model rewrote it. This sets those rows to the owner's
current default.

What it changes: UserArtifact rows with ai_usage='chat' whose owner's
default_ai_usage is 'train' → 'train'. With --include-none, also rows
whose owner's default is 'none' → 'none'; that hides those artifacts
from the owner's own agentic threads (a 'none' artifact never reaches a
prompt), which is what a 'none' default means for every new write, but
it is a visible change for anyone who runs threads with a per-thread
'chat' override, so it is opt-in. Owners whose default is 'chat' have
nothing to change. Every version of an artifact is restamped, not just
the latest: sessions pin older versions (#191) and the export
references them.

What it cannot tell apart: no page or API sets an artifact's ai_usage
by hand, so no 'chat' row records a per-artifact choice. But the
intentions and digest tasks already followed the owner's default, so a
'chat' row written by them while the owner's default WAS 'chat' is
restamped to the owner's current default too. The owner's current
default is the setting this applies.

Metadata only: selects ids and issues UPDATEs in batches of --batch-size
(default 500). Never loads or decrypts content (no KMS calls) and holds
at most one batch of ids in memory, so it is safe on the 4 GB prod VM.
Each UPDATE re-checks ai_usage='chat', so a row written or changed
while this runs is left alone. Rerunnable; a second run finds nothing.

Side effect for 24 h: system prompts rendered before the run are cached
(#192) with the verdict they had then, so a thread whose prompt was
rendered with a 'chat' memory stays on the chat key until that entry
expires (the conservative direction).

Runbook (prod, from the repo root, in the app's virtualenv):

    python backend/scripts/restamp_artifact_ai_usage.py                    # dry run: counts only
    python backend/scripts/restamp_artifact_ai_usage.py --apply
    python backend/scripts/restamp_artifact_ai_usage.py --include-none     # dry run incl. 'none' owners

Verify after: the dry run reports 0 rows to restamp for the targets you
applied.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from sqlalchemy import distinct, func  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import User, UserArtifact  # noqa: E402

UNCHOSEN = "chat"


def survey():
    """{owner default: (rows, owners)} over artifact rows still 'chat'."""
    rows = (
        db.session.query(
            User.default_ai_usage,
            func.count(UserArtifact.id),
            func.count(distinct(UserArtifact.user_id)),
        )
        .join(User, User.id == UserArtifact.user_id)
        .filter(UserArtifact.ai_usage == UNCHOSEN)
        .group_by(User.default_ai_usage)
        .all()
    )
    return {usage: (n_rows, n_owners) for usage, n_rows, n_owners in rows}


def restamp(targets, apply=False, batch_size=500):
    """Walk the 'chat' rows of owners whose default is in *targets*, in
    id order, one batch of ids at a time. Returns a Counter keyed by
    (old, new): rows restamped with *apply*, rows that would be without.
    """
    counts = Counter()
    for target in targets:
        last_id = 0
        while True:
            ids = [
                row_id for (row_id,) in
                db.session.query(UserArtifact.id)
                .join(User, User.id == UserArtifact.user_id)
                .filter(
                    UserArtifact.ai_usage == UNCHOSEN,
                    User.default_ai_usage == target,
                    UserArtifact.id > last_id,
                )
                .order_by(UserArtifact.id.asc())
                .limit(batch_size)
            ]
            if not ids:
                break
            last_id = ids[-1]
            if apply:
                changed = (
                    UserArtifact.query
                    .filter(UserArtifact.id.in_(ids),
                            UserArtifact.ai_usage == UNCHOSEN)
                    .update({UserArtifact.ai_usage: target},
                            synchronize_session=False)
                )
                db.session.commit()
            else:
                changed = len(ids)
            counts[(UNCHOSEN, target)] += changed
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Restamp 'chat' artifact rows to the owner's default "
                    "(#326)")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it this only reports")
    parser.add_argument("--include-none", action="store_true",
                        help="also restamp rows of owners whose default "
                             "is 'none' (hides them from AI)")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        found = survey()
        print("artifact rows still 'chat', by the owner's default:")
        for usage in sorted(found):
            n_rows, n_owners = found[usage]
            print(f"  owner default {usage!r}: {n_rows} row(s), "
                  f"{n_owners} owner(s)")
        targets = ["train"] + (["none"] if args.include_none else [])
        skipped = found.get("none")
        if skipped and not args.include_none:
            print(f"  (owners defaulting to 'none': {skipped[0]} row(s) "
                  "left as they are; --include-none to restamp them)")
        counts = restamp(targets, apply=args.apply,
                         batch_size=args.batch_size)
        verb = "restamped" if args.apply else "would restamp"
        for (old, new), n in sorted(counts.items()):
            print(f"{verb} {old!r} -> {new!r}: {n} row(s)")
        if not counts:
            print("nothing to restamp")
        if not args.apply:
            print("dry run — nothing written; rerun with --apply")
        return 0


if __name__ == "__main__":
    sys.exit(main())
