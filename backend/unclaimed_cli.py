"""CLI for the pre-filled accounts nobody claimed (#685;
backend/utils/unclaimed_accounts.py).

    flask unclaimed-accounts
        what the nightly run would delete now and later: counts, account
        ids and dates (no handles); changes nothing
    flask unclaimed-accounts --keep ID --until 2027-01-31
    flask unclaimed-accounts --keep ID --until forever
    flask unclaimed-accounts --keep ID --until clear
        the admin's Keep on one account, as in the admin's Users tab
"""
from datetime import datetime

import click
from flask import current_app
from flask.cli import with_appcontext

from backend.extensions import db
from backend.utils import unclaimed_accounts as ua


def _day(dt):
    return dt.strftime("%Y-%m-%d") if dt else "?"


def print_report(echo=click.echo, now=None):
    config = current_app.config
    now = now or datetime.utcnow()
    assessments = ua.report(now, config=config)
    by = {}
    for a in assessments:
        by.setdefault(a.status, []).append(a)
    mode = ("ON: due accounts are deleted" if ua.deletion_enabled(config)
            else "DRY RUN: nothing is deleted")
    echo(f"Pre-filled accounts nobody claimed: deleted "
         f"{ua.period_days(config)} days after they were opened. {mode}.")
    due = by.get("due", [])
    echo(f"\nDue now: {len(due)} (a real run deletes at most "
         f"{ua.MAX_DELETIONS_PER_RUN} a night, these first)")
    for a in due:
        echo(f"  id {a.user_id}: opened {_day(a.opened_at)}, "
             f"date {_day(a.delete_on)}")
    waiting = by.get("waiting", [])
    echo(f"\nWaiting: {len(waiting)}")
    for a in waiting:
        why = f" ({'; '.join(a.reasons)})" if a.reasons else ""
        echo(f"  id {a.user_id}: opened {_day(a.opened_at)}, deleted on "
             f"{_day(a.delete_on)} unless someone signs in{why}")
    kept = by.get("kept", [])
    echo(f"\nNever deleted here (excluded): {len(kept)}")
    for a in kept:
        echo(f"  id {a.user_id}: {'; '.join(a.reasons)}")
    used = by.get("used", [])
    echo(f"\nSomeone used the account (theirs, never deleted): {len(used)}")
    if used:
        echo("  ids " + ", ".join(str(a.user_id) for a in used))
    before = by.get("before_record", [])
    from backend.utils.activity import LAST_SEEN_SINCE
    echo(f"\nOpened before {_day(LAST_SEEN_SINCE)}, when sign-ins left no "
         f"record (never deleted automatically): {len(before)}")
    if before:
        echo("  ids " + ", ".join(str(a.user_id) for a in before))
    return assessments


@click.command("unclaimed-accounts")
@click.option("--keep", "keep_id", type=int, default=None,
              help="Set the admin's Keep on this account id.")
@click.option("--until", "until", default=None,
              help='With --keep: a date (YYYY-MM-DD), "forever" or "clear".')
@with_appcontext
def unclaimed_accounts_command(keep_id, until):
    """The pre-filled accounts nobody claimed (#685): what would be
    deleted, and the admin's Keep."""
    if keep_id is not None:
        from backend.models import User
        user = db.session.get(User, keep_id)
        if user is None:
            raise click.ClickException(f"no account with id {keep_id}")
        try:
            value = ua.parse_until(until)
        except ValueError:
            raise click.ClickException(
                'use --until YYYY-MM-DD, "forever" or "clear"')
        ua.set_keep(user, value)
        shown = ("for good" if value == ua.KEEP_FOREVER
                 else f"until {_day(value)}" if value else "cleared")
        click.echo(f"Keep on account {keep_id}: {shown}")
        return
    print_report()
