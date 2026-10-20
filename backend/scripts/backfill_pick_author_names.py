"""Fill the display name of tweets Glean picked before #435. One-off,
after deploy.

A data backfill, not a schema migration: ExternalItem.author_name comes
from the migration deploy.sh generates (`flask db migrate`), so run this
only once that deploy has finished. Picks saved since then carry the name
already (ca_feed.save_feed_picks).

Every tweet row a Glean picked that has a handle and no name gets the
display name the cached Community Archive snapshot has for that handle
(profiles.parquet). Handles the archive has no name for stay without one,
and the tweet card then shows the handle alone. Reads and writes
metadata only: handles and public display names. No content is
decrypted. One query over the picked rows and one scan of
profiles.parquet; light enough to run on the production VM.

    cd /path/to/write-or-perish
    python backend/scripts/backfill_pick_author_names.py            # dry run
    python backend/scripts/backfill_pick_author_names.py --apply
    python backend/scripts/backfill_pick_author_names.py --user-id 6 --apply

Idempotent: a second run finds only the rows the archive has no name for.
"""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

from backend import create_app, db  # noqa: E402
from backend.tasks.imports import snapshot_dir_for  # noqa: E402
from backend.utils.ca_feed import fill_pick_author_names  # noqa: E402
from backend.utils.community_archive import snapshot_export_id  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the names (default: dry run)")
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        snapshot_dir = snapshot_dir_for(app.config)
        if not snapshot_export_id(snapshot_dir):
            print(f"No Community Archive snapshot cached at {snapshot_dir}; "
                  "nothing to read the names from.")
            return 1
        print("APPLY" if args.apply else "DRY RUN (nothing is written)")
        missing, filled = fill_pick_author_names(
            snapshot_dir, user_id=args.user_id, apply=args.apply)
        print(f"{missing} picked tweets without a display name; "
              f"{filled} have one in the archive")
        if args.apply:
            db.session.commit()
            print("committed")
        else:
            db.session.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
