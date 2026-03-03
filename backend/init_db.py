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


@click.command("backfill-prompt-refs")
@with_appcontext
def backfill_prompt_refs_command():
    """Link existing system prompt nodes to UserPrompt rows. Idempotent."""
    from backend.extensions import db
    from backend.models import Node, UserPrompt, User
    from backend.utils.encryption import decrypt_content
    from backend.utils.prompts import get_user_prompt_record, PROMPT_DEFAULTS

    # Prompt keys used for system prompt nodes
    workflow_keys = ['reflect', 'orient', 'converse']

    users = User.query.all()
    total_linked = 0

    for user in users:
        # Build lookup: decrypted_content → user_prompt_id
        content_to_prompt_id = {}

        # Include all existing UserPrompt rows for this user
        prompts = UserPrompt.query.filter_by(user_id=user.id).filter(
            UserPrompt.prompt_key.in_(workflow_keys)
        ).all()
        for p in prompts:
            try:
                plaintext = p.get_content()
                content_to_prompt_id[plaintext] = p.id
            except Exception:
                continue

        # Ensure file defaults exist as rows (creates if needed)
        for key in workflow_keys:
            record = get_user_prompt_record(user.id, key)
            if record:
                try:
                    plaintext = record.get_content()
                    if plaintext not in content_to_prompt_id:
                        content_to_prompt_id[plaintext] = record.id
                except Exception:
                    continue

        if not content_to_prompt_id:
            continue

        # Find unlinked nodes for this user
        nodes = Node.query.filter(
            Node.user_id == user.id,
            Node.user_prompt_id.is_(None),
            Node.content.isnot(None),
        ).all()

        linked = 0
        for node in nodes:
            try:
                plaintext = decrypt_content(node.content)
            except Exception:
                continue
            if plaintext in content_to_prompt_id:
                node.user_prompt_id = content_to_prompt_id[plaintext]
                node.content = None
                linked += 1

        if linked:
            db.session.commit()
            total_linked += linked
            click.echo(f"  User {user.username}: linked {linked} nodes")

    click.echo(f"Done. Linked {total_linked} system prompt nodes total.")
