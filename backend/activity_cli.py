"""CLI for the admin Activity tab's stored records
(backend/utils/artifact_name_cleanup.py).

    flask artifact-names-cleanup
        how many view records and last-seen paths written before #729
        still hold a name a user gave an artifact, and what each would
        get instead; changes nothing. Counts only. Accounts whose
        content or account deletion waits or runs are skipped and
        counted; a later run cleans them.
    flask artifact-names-cleanup --apply
        makes the change, committing in batches. Safe to stop and to run
        again; a run after it shows 0 to change.
"""
import click
from flask.cli import with_appcontext

from backend.utils import artifact_name_cleanup as cleanup


@click.command("artifact-names-cleanup")
@click.option("--apply", "apply_", is_flag=True,
              help="Make the change; without it, only count what would "
                   "change.")
@click.option("--batch-size", type=click.IntRange(min=1),
              default=cleanup.BATCH_SIZE, show_default=True,
              help="Rows changed per commit, and accounts read per page.")
@with_appcontext
def artifact_names_cleanup_command(apply_, batch_size):
    """Remove the names users gave their artifacts from the view records
    and last-seen paths written before #729 (2026-11-20), keeping the
    Activity tab's counts. Prints counts only, never a name. Shows what
    it would do unless --apply is given."""
    from backend.extensions import db
    try:
        report = cleanup.run(apply=apply_, batch_size=batch_size)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        # The error's own text can quote a stored name (a database error
        # shows the statement's values), so only its type is shown.
        done = ("Batches committed before it stay; run the command again "
                "to finish." if apply_ else "Nothing changed.")
        raise click.ClickException(f"Stopped by {type(e).__name__}. {done}")
    for line in cleanup.report_lines(report):
        click.echo(line)
