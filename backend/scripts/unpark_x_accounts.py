"""Un-park X accounts that #313 disconnected for Loore's own bug.

Until the fix, the nightly bookmark sync refreshed its access token
WITHOUT the client secret. X answers a confidential client that does that
with 401 invalid_client, and the sync read any 400/401 as "the user
revoked us": it set ``revoked_at``, notified the user that X had
disconnected, and stopped syncing. The user could only reconnect — which
bought exactly one sync, until the next night killed it again.

Those accounts are still parked after the fix, and their stored refresh
token is untouched (no refresh ever succeeded), so clearing the flag is
enough to resume nightly syncing. This clears it, and marks the stale
"X disconnected" notices read so nobody is told to reconnect for nothing.

Safe by construction: an account whose grant really IS dead gets parked
again on its next sync — now with the reason ``invalid_grant``, which
means the user themselves revoked access.

Dry run by default; --apply writes. Run it AFTER the fix is deployed,
otherwise the next night parks everyone again.

    python -m backend.scripts.unpark_x_accounts            # dry run
    python -m backend.scripts.unpark_x_accounts --apply
"""
import argparse
import sys
from datetime import datetime


def _run(apply, out=sys.stdout):
    from backend.extensions import db
    from backend.models import ExternalAccount, UserNotification

    parked = ExternalAccount.query.filter(
        ExternalAccount.provider == "twitter",
        ExternalAccount.revoked_at.isnot(None),
    ).all()
    print(f"{len(parked)} parked X account(s)", file=out)

    unparked = skipped = notices_read = 0
    for account in parked:
        # No refresh token, no way to resume — that account has to
        # reconnect whatever we do here.
        if not account.get_refresh_token():
            print(f"  user {account.user_id}: no refresh token, leaving "
                  f"parked (revoked {account.revoked_at})", file=out)
            skipped += 1
            continue
        print(f"  user {account.user_id}: connected {account.created_at}, "
              f"last synced {account.last_synced_at}, "
              f"parked {account.revoked_at}", file=out)
        unparked += 1
        if not apply:
            continue
        account.revoked_at = None
        for notice in UserNotification.query.filter_by(
                user_id=account.user_id, type="x_disconnected",
                status="unread").all():
            notice.status = "read"
            notice.read_at = datetime.utcnow()
            notices_read += 1
    if apply:
        db.session.commit()
    verb = "un-parked" if apply else "would un-park"
    print(f"{verb} {unparked}, left parked {skipped}, "
          f"notices marked read {notices_read}", file=out)
    if not apply:
        print("dry run — re-run with --apply to write", file=out)
    return {"unparked": unparked, "skipped": skipped,
            "notices_read": notices_read}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true",
                   help="clear revoked_at (default: dry run)")
    args = p.parse_args()
    from backend import create_app
    app = create_app()
    with app.app_context():
        _run(args.apply)


if __name__ == "__main__":
    main()
