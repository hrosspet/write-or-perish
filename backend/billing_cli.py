"""CLI for the Stripe catalog (#438; backend/utils/stripe_billing.py).

    flask stripe-sync-catalog     create or update, in the Stripe account the
                                  configured secret key belongs to, the
                                  product, the three monthly prices (by
                                  lookup key) and the customer-portal
                                  configuration. Safe to run again: what
                                  matches is left alone.

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
