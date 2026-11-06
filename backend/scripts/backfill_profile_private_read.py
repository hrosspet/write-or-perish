"""Mark the accounts from before User.profile_private_read whose profile
builds may have read private writing (Peter, 2026-11-05: a user's writing
never reaches the other provider unless public-only or consented; when in
doubt, private).

The mark is set by the profile passes from its deploy on; an account that
existed before carries no record of what earlier passes read. This sets
it for every account that ``utils/profile_providers.needs_private_mark``
cannot vouch for: someone signed into it (an account that ever uploaded
its own archive is private), it has an entry that is not an unchanged
pre-filled tweet (deleted ones included), or it has a profile row not
proven to have read pre-filled tweets alone. Metadata only: no content is
read. Idempotent: a marked account is skipped, and nothing is ever
unmarked. The public-only check does not depend on this run (it is
already limited to accounts nobody signed into); the mark makes the
record complete for the rest.

Run after the deploy that adds the column, before the first profile
window pass:

    cd /path/to/write-or-perish
    python backend/scripts/backfill_profile_private_read.py            # dry run
    python backend/scripts/backfill_profile_private_read.py --apply

Verify after: the dry run reports 0 to mark.
"""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())


def plan(users):
    """(to_mark, already_marked, left) user ids."""
    from backend.utils.profile_providers import needs_private_mark
    to_mark, marked, left = [], [], []
    for user in users:
        if user.profile_private_read:
            marked.append(user.id)
        elif needs_private_mark(user):
            to_mark.append(user.id)
        else:
            left.append(user.id)
    return to_mark, marked, left


def run(apply=False, out=print):
    from backend.extensions import db
    from backend.models import User
    users = User.query.order_by(User.id).all()
    to_mark, marked, left = plan(users)
    out(f"{len(users)} accounts: {len(marked)} already marked, "
        f"{len(to_mark)} to mark, {len(left)} left unmarked "
        f"(nobody signed in, pre-filled tweets only, or nothing yet)")
    if apply and to_mark:
        User.query.filter(User.id.in_(to_mark)).update(
            {"profile_private_read": True}, synchronize_session=False)
        db.session.commit()
        out(f"marked {len(to_mark)} accounts")
    elif to_mark:
        out("dry run: nothing changed (--apply to mark)")
    return to_mark


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill User.profile_private_read (metadata only).")
    parser.add_argument("--apply", action="store_true",
                        help="mark the accounts (default: dry run)")
    args = parser.parse_args(argv)
    from backend import create_app
    app = create_app()
    with app.app_context():
        run(apply=args.apply)


if __name__ == "__main__":
    main()
