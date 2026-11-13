"""CLI for the pre-filled accounts nobody claimed (#685;
backend/utils/unclaimed_accounts.py).

    flask unclaimed-accounts
        what the nightly run would delete now and later: counts, account
        ids, dates, where each account's 90 days count from and why it is
        due (no handles); changes nothing
    flask unclaimed-accounts --keep 12,15 --keep 40 --until 2027-01-31
    flask unclaimed-accounts --keep-all-due --until 2027-01-31
        the admin's Keep on several accounts, or on every account due now
        (accounts are invited in waves); --until also takes "forever" and
        "clear"
"""
from datetime import datetime

import click
from flask import current_app
from flask.cli import with_appcontext

from backend.utils import unclaimed_accounts as ua


def _day(dt):
    return dt.strftime("%Y-%m-%d") if dt else "?"


def parse_ids(values):
    """Ids from repeated --keep values, each "12" or "12,15,40"."""
    out = []
    for value in values or ():
        for part in str(value).replace(" ", ",").split(","):
            if part:
                out.append(int(part))
    return out


def print_report(echo=click.echo, now=None):
    config = current_app.config
    now = now or datetime.utcnow()
    days = ua.period_days(config)
    assessments = ua.report(now, config=config)
    by = {}
    for a in assessments:
        by.setdefault(a.status, []).append(a)
    mode = ("ON: due accounts are deleted" if ua.deletion_enabled(config)
            else "DRY RUN: nothing is deleted")
    echo(f"Pre-filled accounts nobody claimed: deleted {days} days after "
         f"they were opened. {mode}.")
    due = by.get("due", [])
    echo(f"\nDue now: {len(due)} (a real run deletes at most "
         f"{ua.MAX_DELETIONS_PER_RUN} a night, these first)")
    for a in due:
        echo(f"  id {a.user_id}: {a.why(days)}")
    waiting = by.get("waiting", [])
    echo(f"\nWaiting (deleted on the date unless someone signs in): "
         f"{len(waiting)}")
    for a in waiting:
        echo(f"  id {a.user_id}: {a.why(days)}")
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
@click.option("--keep", "keep", multiple=True,
              help="Set the admin's Keep on these account ids (repeat it, "
                   "or give several separated by commas).")
@click.option("--keep-all-due", is_flag=True, default=False,
              help="Set the admin's Keep on every account due now.")
@click.option("--until", "until", default=None,
              help='With --keep/--keep-all-due: a date (YYYY-MM-DD), '
                   '"forever" or "clear".')
@with_appcontext
def unclaimed_accounts_command(keep, keep_all_due, until):
    """The pre-filled accounts nobody claimed (#685): what would be
    deleted, and the admin's Keep."""
    if not keep and not keep_all_due:
        print_report()
        return
    if until is None:
        raise click.ClickException(
            'give --until YYYY-MM-DD, "forever" or "clear"')
    try:
        value = ua.parse_until(until)
    except ValueError:
        raise click.ClickException(
            'use --until YYYY-MM-DD, "forever" or "clear"')
    try:
        ids = parse_ids(keep)
    except ValueError:
        raise click.ClickException("--keep takes account ids")
    changed = []
    if ids:
        changed += ua.keep_many(until=value, user_ids=ids)
        missing = sorted(set(ids) - set(changed))
        if missing:
            click.echo(f"No account with id {', '.join(map(str, missing))}")
    if keep_all_due:
        changed += ua.keep_many(until=value, scope="due")
    shown = ("for good" if value == ua.KEEP_FOREVER
             else f"until {_day(value)}" if value else "cleared")
    click.echo(f"Keep {shown} on {len(set(changed))} account(s): "
               + (", ".join(str(i) for i in sorted(set(changed))) or "none"))
