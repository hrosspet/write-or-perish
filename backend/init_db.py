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


@click.command("backfill-human-owner")
@with_appcontext
def backfill_human_owner_command():
    """Backfill human_owner_id on all nodes. Idempotent."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.privacy import find_human_owner

    # Bulk update non-LLM nodes: human_owner_id = user_id
    count = Node.query.filter(
        Node.node_type != "llm",
        Node.human_owner_id.is_(None),
    ).update({Node.human_owner_id: Node.user_id}, synchronize_session=False)
    db.session.commit()
    click.echo(f"Set human_owner_id on {count} non-LLM nodes.")

    # LLM nodes: walk parent chain
    llm_nodes = Node.query.filter(
        Node.node_type == "llm",
        Node.human_owner_id.is_(None),
    ).all()
    updated = 0
    for node in llm_nodes:
        owner_id = find_human_owner(node)
        if owner_id:
            node.human_owner_id = owner_id
            updated += 1
    db.session.commit()
    click.echo(f"Set human_owner_id on {updated} LLM nodes ({len(llm_nodes)} total).")
