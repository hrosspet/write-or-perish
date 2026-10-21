"""CLI for the optional pgvector ranking (#331; backend/utils/pgvector.py).

    flask pgvector-enable            report what --apply would do (no change)
    flask pgvector-enable --apply    create the copy tables, copy every vector
    flask pgvector-compare --username NAME
                                     rank one user's neighbours both ways and
                                     time them (reads vectors only, no content)

The extension itself is created by a superuser beforehand:
    CREATE EXTENSION IF NOT EXISTS vector;
"""
import time

import click
import sqlalchemy as sa
from flask.cli import with_appcontext
from sqlalchemy.exc import DBAPIError

from backend.extensions import db
from backend.utils import pgvector

# Estimated bytes one copy adds: the vector (4 bytes x dim + 8), its TOAST
# chunk overhead, the row (id, content_hash, model) and its primary-key
# entry. Measured: 8.5 KB per row with 1536 dims (pgvector 0.8, PG 15),
# about what the source table already spends on each row.
_BYTES_PER_COPY = 8500


def _scalar(sql, **params):
    return db.session.execute(sa.text(sql), params).scalar()


def _report():
    """Print the current state; return (extension_installed, schema_owner,
    rows_to_copy)."""
    available = _scalar(
        "SELECT default_version FROM pg_available_extensions"
        " WHERE name = 'vector'")
    installed = _scalar(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    owner = _scalar(
        "SELECT nspowner::regrole::text FROM pg_namespace"
        " WHERE nspname = :name", name=pgvector.SCHEMA)
    click.echo("pgvector on the database server: "
               + (f"available ({available})" if available
                  else "NOT available (install the pgvector package)"))
    click.echo('extension "vector": '
               + (f"created ({installed})" if installed else "not created"))
    click.echo(f'schema "{pgvector.SCHEMA}": '
               + (f"exists (owner {owner})" if owner else "missing"))

    to_copy = 0
    for t in pgvector.TABLES:
        rows = _scalar(f"SELECT count(*) FROM {t}")  # nosec B608
        current = 0
        if installed and _scalar(
                "SELECT to_regclass(:name) IS NOT NULL",
                name=f"{pgvector.SCHEMA}.{t}"):
            current = _scalar(
                f"SELECT count(*) FROM {t} e"  # nosec B608
                f" JOIN {pgvector.SCHEMA}.{t} c ON c.id = e.id"
                " AND c.content_hash = e.content_hash"
                " AND c.model = e.model")
        to_copy += rows - current
        click.echo(f"{t}: {rows:,} rows, {current:,} with a current copy,"
                   f" {rows - current:,} to copy")
    click.echo(f"Copying adds about "
               f"{to_copy * _BYTES_PER_COPY / 1e9:.2f} GB to the database"
               f" ({_BYTES_PER_COPY / 1000:.1f} KB per row).")
    return bool(installed), owner, to_copy


def _ensure_schema(owner):
    if owner:
        return
    try:
        db.session.execute(sa.text(f"CREATE SCHEMA {pgvector.SCHEMA}"))
        db.session.commit()
    except DBAPIError:
        db.session.rollback()
        user = _scalar("SELECT current_user")
        raise click.ClickException(
            f"This role ({user}) may not create schemas. As a superuser,"
            f" connected to this database, run:\n"
            f"  CREATE SCHEMA {pgvector.SCHEMA} AUTHORIZATION {user};\n"
            "then run this command again.")


@click.command("pgvector-enable")
@click.option("--apply", "apply_", is_flag=True,
              help="Create the copy tables and copy every vector. Without"
                   " it, only report.")
@with_appcontext
def pgvector_enable_command(apply_):
    """Turn on SQL ranking for semantic search (#331). Idempotent."""
    if db.engine.dialect.name != "postgresql":
        raise click.ClickException(
            "Not a PostgreSQL database: semantic search ranks in Python.")
    installed, owner, _ = _report()
    if not installed:
        raise click.ClickException(
            "Create the extension first. As a superuser, connected to this"
            " database:\n  CREATE EXTENSION IF NOT EXISTS vector;")
    if not apply_:
        db.session.rollback()
        click.echo("Report only: nothing changed. Run with --apply to"
                   " create the tables and copy the vectors.")
        return
    _apply(owner)


def _apply(owner):
    _ensure_schema(owner)
    try:
        for statement in pgvector.ddl_statements():
            db.session.execute(sa.text(statement))
        db.session.commit()
    except DBAPIError as exc:
        db.session.rollback()
        raise click.ClickException(
            "Creating the copy tables failed"
            f" ({type(exc.orig).__name__}). If a lock timed out, a write"
            " held the table: run the command again.")
    click.echo("Copy tables ready. Copying vectors...")

    started = time.monotonic()

    def progress(table, read, copied):
        if read % (pgvector.SYNC_CHUNK * 20) == 0:
            click.echo(f"  {table}: {copied:,} copied"
                       f" ({time.monotonic() - started:.0f} s)")

    result = pgvector.sync_copies(on_chunk=progress)
    for table, (read, copied) in result.items():
        size = _scalar("SELECT pg_total_relation_size(:name)",
                       name=f"{pgvector.SCHEMA}.{table}")
        line = f"{table}: {copied:,} copied"
        if read > copied:
            line += (f", {read - copied:,} left to the Python path (not a"
                     " finite, non-zero vector of the expected dimension)")
        click.echo(f"{line}; the copy table is {size / 1e6:,.0f} MB.")
    click.echo(
        f"Done in {time.monotonic() - started:.0f} s. Restart the web and"
        " worker processes (a deploy does it): each one checks for the"
        " tables once, at its first search.")


@click.command("pgvector-compare")
@click.option("--username", required=True,
              help="Whose archive to rank (their newest embedding is the"
                   " query, as on the node page's neighbours).")
@click.option("--k", default=10, show_default=True)
@click.option("--repeat", default=2, show_default=True,
              help="Runs per path; the first one reads from disk.")
@with_appcontext
def pgvector_compare_command(username, k, repeat):
    """Rank one user's neighbours in SQL and in Python; compare and time.
    Reads embedding rows only: no content is loaded or decrypted."""
    from backend.models import NodeEmbedding, User
    from backend.utils.embeddings import (
        EMBEDDING_SCAN_CHUNK, top_k_similar, unpack_vector,
    )

    user = User.query.filter_by(username=username).first()
    if user is None:
        raise click.ClickException(f"No user {username!r}.")
    src = (NodeEmbedding.query.filter_by(user_id=user.id)
           .order_by(NodeEmbedding.id.desc()).first())
    if src is None:
        raise click.ClickException("That user has no embeddings.")
    if not pgvector.enabled():
        raise click.ClickException(
            "pgvector is not on in this database: run"
            " `flask pgvector-enable` first.")
    query_vector = unpack_vector(src.vector)
    literal = pgvector.vector_literal(query_vector)
    if literal is None:
        raise click.ClickException(
            "The newest embedding is not a usable vector; nothing to compare.")
    base = db.session.query(
        NodeEmbedding.node_id, NodeEmbedding.vector
    ).filter(NodeEmbedding.user_id == user.id,
             NodeEmbedding.node_id != src.node_id)
    total = base.count()
    missing = pgvector.without_copy(base, NodeEmbedding).count()
    click.echo(f"{total:,} rows; {missing:,} without a current copy.")

    def timed(run):
        times, result = [], None
        for _ in range(max(repeat, 1)):
            t0 = time.monotonic()
            result = run()
            times.append(time.monotonic() - t0)
        return result, ", ".join(f"{t:.3f} s" for t in times)

    in_sql, sql_times = timed(lambda: pgvector.finite_scores(
        pgvector.ranked_in_db(base, NodeEmbedding, literal, k).all(),
        float("-inf")))
    in_python, py_times = timed(lambda: top_k_similar(
        query_vector, base.yield_per(EMBEDDING_SCAN_CHUNK), k=k,
        min_score=-1.0))
    db.session.rollback()

    click.echo(f"SQL:    {sql_times}")
    click.echo(f"Python: {py_times}")
    same = [i for i, _ in in_sql] == [i for i, _ in in_python]
    diff = max((abs(a - b) for (_, a), (_, b) in zip(in_sql, in_python)),
               default=0.0)
    click.echo(f"Same top {k} in the same order: {'yes' if same else 'NO'};"
               f" largest score difference {diff:.1e}.")
    if not same and missing:
        click.echo("(Rows without a copy are left out of the SQL run: run"
                   " `flask pgvector-enable --apply` and compare again.)")
