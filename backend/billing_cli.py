"""CLI for the Stripe catalog (#438; backend/utils/stripe_billing.py).

    flask stripe-sync-catalog     create or update, in the Stripe account the
                                  configured secret key belongs to, the
                                  product, the three monthly prices (by
                                  lookup key) and the customer-portal
                                  configuration. Safe to run again: what
                                  matches is left alone.
    flask alpha-free-month        the alpha testers' free month: lists the
                                  accounts; --apply gives it, --undo takes
                                  it back.

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
@click.option("--apply", "apply_", is_flag=True,
              help="Make the change; without it, only list the accounts.")
@click.option("--undo", is_flag=True,
              help="Put alpha testers back on the alpha plan.")
@click.option("--user", "usernames", multiple=True,
              help="Only this username (repeatable).")
@with_appcontext
def alpha_free_month_command(apply_, undo, usernames):
    """The alpha free month (#438): move every approved account on the
    "alpha" plan to "free" with a free month (ALPHA_FREE_MONTH_DAYS) that
    starts at its next visit, then the discounted price without a code.
    Run once, right after PAYMENTS_ENABLED is turned on. Lists the
    accounts unless --apply is given; --undo reverses it."""
    from backend.utils.subscriptions import (
        alpha_free_month_candidates, grant_alpha_free_month,
        payments_enabled, undo_alpha_free_month)
    if undo:
        if not apply_:
            raise click.ClickException("--undo needs --apply as well.")
        n = undo_alpha_free_month(usernames or None)
        click.echo(f"{n} account(s) back on the alpha plan.")
        return
    if not payments_enabled():
        raise click.ClickException(
            "Payments are off: set PAYMENTS_ENABLED=true and restart "
            "first. The free month only means something while they are on.")
    users = alpha_free_month_candidates(usernames or None)
    days = current_app.config.get("ALPHA_FREE_MONTH_DAYS")
    click.echo(f"{len(users)} account(s) on the alpha plan get a free "
               f"month ({days} days from their next visit):")
    for u in users:
        click.echo(f"  {u.username}")
    if not apply_:
        click.echo("Nothing changed. Run again with --apply.")
        return
    n = grant_alpha_free_month(users)
    click.echo(f"Done: {n} account(s) moved to the free plan with a free "
               "month.")
