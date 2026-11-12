"""CLI for the Stripe catalog (#438; backend/utils/stripe_billing.py).

    flask stripe-sync-catalog     create or update, in the Stripe account the
                                  configured secret key belongs to, the
                                  product, the three monthly prices (by
                                  lookup key) and the customer-portal
                                  configuration. Safe to run again: what
                                  matches is left alone.
    flask alpha-free-month --start YYYY-MM-DD
                                  the alpha testers' free month from that
                                  day (00:00 UTC): shows the accounts and
                                  dates; --apply gives it, --change-date
                                  moves it, --undo takes it back.

The amounts, currency, tax behaviour and tax code come from the settings
(SUBSCRIPTION_PRICE_CENTS_*, SUBSCRIPTION_CURRENCY,
SUBSCRIPTION_TAX_BEHAVIOR, STRIPE_PRODUCT_TAX_CODE). The command prints
ids and whether the key is a test or a live one, never the key.
"""
import click
from flask import current_app
from flask.cli import with_appcontext

from backend.utils import stripe_billing


def _mode(key):
    if key.startswith(("sk_live_", "rk_live_")):
        return "LIVE"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    return "unknown"


@click.command("stripe-sync-catalog")
@with_appcontext
def stripe_sync_catalog_command():
    """Create or update the Stripe product, prices and portal settings."""
    key = current_app.config.get("STRIPE_SECRET_KEY")
    if not key:
        raise click.ClickException(
            "No Stripe secret key: set STRIPE_SECRET_KEY.")
    click.echo(f"Stripe key: {_mode(key)} mode")
    result = stripe_billing.sync_catalog(
        stripe_billing.get_gateway(), log=click.echo)
    if result["changes"]:
        click.echo("Changed:")
        for line in result["changes"]:
            click.echo(f"  {line}")
    else:
        click.echo("Nothing to change.")
    # Stripe's API does not show what happens after the last failed retry
    # of a payment, so this can't be checked here.
    click.echo(
        "Check by hand in the Dashboard (Billing > Revenue recovery > "
        "Retries > Subscription status): 'If all retries for a payment "
        "fail' must be 'Cancel the subscription'.")


@click.command("alpha-free-month")
@click.option("--start", "start_raw", metavar="YYYY-MM-DD",
              help="The first day of the free month, 00:00 UTC: the Beta "
                   "release date. Needed to give or move the month.")
@click.option("--apply", "apply_", is_flag=True,
              help="Make the change; without it, only show what it would "
                   "be.")
@click.option("--change-date", is_flag=True,
              help="Move the month of accounts that have it already to "
                   "--start.")
@click.option("--undo", is_flag=True,
              help="Put alpha testers back on the alpha plan.")
@click.option("--user", "usernames", multiple=True,
              help="Only this username (repeatable).")
@with_appcontext
def alpha_free_month_command(start_raw, apply_, change_date, undo,
                             usernames):
    """The alpha free month (#438): every approved account on the "alpha"
    plan moves to "free" with a free month from --start (00:00 UTC) for
    ALPHA_FREE_MONTH_DAYS days, the same dates for everyone, then the
    discounted price without a code. Until the start nothing changes for
    them. Shows what it would do unless --apply is given. --change-date
    moves the month of accounts that have it; --undo reverses it all."""
    from datetime import datetime

    from backend.utils.subscriptions import (
        alpha_free_month_candidates, alpha_testers, current_free_month,
        free_month_dates, grant_alpha_free_month, move_alpha_free_month,
        parse_start_date, payments_enabled, undo_alpha_free_month)
    if undo:
        # Works with payments off too: it is the way back after turning
        # them off.
        if not apply_:
            raise click.ClickException("--undo needs --apply as well.")
        n, skipped = undo_alpha_free_month(usernames or None)
        click.echo(f"{n} account(s) back on the alpha plan.")
        for name, why in skipped:
            click.echo(f"  left as it is: {name} ({why})")
        return
    if not payments_enabled():
        raise click.ClickException(
            "Payments are off: set PAYMENTS_ENABLED=true and restart "
            "first. The free month only means something while they are on.")
    given = current_free_month()
    if not start_raw and (change_date or given is None):
        raise click.ClickException(
            "--start YYYY-MM-DD is needed: the first day of the free month "
            "(the Beta release date), from 00:00 UTC.")
    try:
        start = parse_start_date(start_raw) if start_raw else given[0]
    except ValueError:
        raise click.ClickException(
            f"--start {start_raw!r} is not a date like 2026-12-01.")
    if usernames and change_date:
        raise click.ClickException(
            "--change-date moves every tester's month; it takes no --user.")
    if given is not None and not change_date and start != given[0]:
        raise click.ClickException(
            f"The free month was given from {given[0]:%Y-%m-%d}. To move "
            "it, use --change-date.")
    begin, end = free_month_dates(start)
    now = datetime.utcnow()
    fmt = "%Y-%m-%d %H:%M UTC"
    click.echo(f"Free month: from {begin.strftime(fmt)} to "
               f"{end.strftime(fmt)} "
               f"({current_app.config.get('ALPHA_FREE_MONTH_DAYS')} days).")
    if now < begin:
        click.echo("It has not started: until then these accounts keep the "
                   "alpha plan's access, as now.")
    elif now < end:
        left = (end - now).days
        click.echo(f"It started already: about {left} day(s) of it are "
                   "left.")
    else:
        click.echo("It has ended already: these accounts are lapsed at "
                   "once (reading, writing and export; no AI).")

    if change_date:
        users = alpha_testers(usernames or None)
        click.echo(f"{len(users)} alpha tester(s) get these dates instead "
                   "of their present ones:")
        for u in users:
            old = (f"{u.alpha_free_start:%Y-%m-%d} to "
                   f"{u.alpha_free_until:%Y-%m-%d}"
                   if u.alpha_free_start and u.alpha_free_until else "none")
            click.echo(f"  {u.username} (now {old})")
        if not apply_:
            click.echo("Nothing changed. Run again with --apply.")
            return
        n = move_alpha_free_month(users, start)
        click.echo(f"Done: the free month of {n} account(s) moved.")
        return

    users = alpha_free_month_candidates(usernames or None)
    click.echo(f"{len(users)} account(s) on the alpha plan get it:")
    for u in users:
        click.echo(f"  {u.username}")
    already = alpha_testers(usernames or None)
    if already:
        click.echo(f"{len(already)} account(s) have this free month "
                   "already and are left as they are.")
    if not apply_:
        click.echo("Nothing changed. Run again with --apply.")
        return
    n = grant_alpha_free_month(users, start)
    click.echo(f"Done: {n} account(s) moved to the free plan with this "
               "free month.")
