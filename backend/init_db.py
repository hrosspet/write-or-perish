"""Smart DB initialization for Docker-based environments.

On a fresh database (no alembic_version table), running `flask db upgrade`
fails because the migration chain has multiple roots with down_revision=None.
This script detects a fresh DB and uses db.create_all() + flask db stamp head
to bootstrap the schema from models, bypassing the broken migration history.

Usage (inside the backend container):
    flask init-db
"""
from flask import current_app
from flask.cli import with_appcontext
import click
import sqlalchemy as sa


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Initialize database: create_all for fresh DBs, upgrade for existing."""
    from backend.extensions import db

    engine = sa.create_engine(current_app.config["SQLALCHEMY_DATABASE_URI"])

    if _is_fresh_db(engine):
        click.echo("Fresh database detected — creating schema from models...")
        db.create_all()
        click.echo("Schema created. Stamping migration head...")
        _stamp_head()
        click.echo("Done. Database is ready.")
    else:
        click.echo("Existing database detected — running flask db upgrade...")
        _run_upgrade()
        click.echo("Done. Migrations applied.")


def _is_fresh_db(engine):
    """Check if the alembic_version table exists."""
    inspector = sa.inspect(engine)
    return "alembic_version" not in inspector.get_table_names()


def _stamp_head():
    """Mark all migrations as applied without running them."""
    from flask_migrate import stamp
    stamp(revision="head")


def _run_upgrade():
    """Run pending migrations."""
    from flask_migrate import upgrade
    upgrade()
