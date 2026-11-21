"""Clean-up of artifact names in records written before #729 (A.102).

Since #729 (2026-11-20) the name of an artifact a user created is kept
nowhere outside the artifact (Peter: nobody but the user sees the names
of the artifacts they create, admins included): a view ping stores
``kind=ArtifactView.USER_CREATED`` and ``artifact_id``, and the last-seen
path replaces the name (``activity.redact_artifact_path``). Records
written before then still hold the names. The Activity tab no longer
shows them; ``flask artifact-names-cleanup --apply`` (``run``) removes
them. These two are the only records the activity monitoring wrote a
name into.

``artifact_view``: every row whose ``kind`` is not a name Loore defines
(``UserArtifact.SYSTEM_KINDS``) gets ``kind=USER_CREATED`` and an
``artifact_id`` chosen so that the Activity tab counts the same
different artifacts as before (``activity.artifact_views`` matches an
old row to its artifact by name, and counts a name with no live
artifact as one artifact):

* the user has the artifact (``LIVE``): the id of its first live version
  (``UserArtifact.first_live_id``), the id #729 stores for a new view and
  the one the tab matched the row to;
* the artifact is deleted and the daily cleanup has not removed it yet
  (``DELETED``): the id of its earliest deleted version still stored, so
  that cleanup deletes these rows with the versions, as it deleted the
  rows naming it (``artifact_deletion.purge_deleted_artifacts``);
* otherwise (``NO_ARTIFACT``: no version under the name is left, or the
  deleted version's id already stands for views stored since #729, which
  the tab counts as another artifact): minus the id of the earliest of
  these rows. Negative, so it is no artifact's id; one value per user and
  name, so the tab still counts each name as one artifact. These rows go
  when the account or its content is deleted, like the rest.

The tab reads the artifacts through the hidden-rows hook (a waiting
"Delete all my content"), and so does this: what it counts is what is
kept. A row that already has an ``artifact_id`` and still a name
(``ID_KEPT``; none expected) keeps the id and loses the name. Rows of
the names Loore defines and rows already stored without a name are left
as they are.

``user.last_seen_path``: a path that carries a user-given name becomes
what #729 stores for a new visit (``stored_path``). The UPDATE sets that
column alone, only while it still holds the path that was read, so a
newer visit is never overwritten; ``User`` has no ``onupdate`` column and
``last_seen_at`` is not touched.

Safe to run again (a second run finds nothing to change) and to stop:
each user's rows under one name change in one statement, and the work is
committed between names every ``batch_size`` rows, so a stopped run
leaves every name either done or untouched, and the next run gives it
the same id. Pages: one user's names at a time, ``batch_size`` accounts
of last-seen paths at a time. Nothing is decrypted: names, ids and paths
only. The report holds counts only; nothing here prints or logs a name.
"""
import dataclasses
from collections import Counter

from sqlalchemy import func

from backend.extensions import db
from backend.models import ArtifactView, User, UserArtifact
from backend.utils.activity import redact_artifact_path

# Rows changed per commit (artifact_view) and accounts read per page
# (last_seen_path). A heuristic: small enough that a commit holds row
# locks for milliseconds on a small machine, large enough that a few
# thousand rows take a handful of commits.
BATCH_SIZE = 500

# Where artifact_view rows go (see the module docstring).
LIVE = "live"
DELETED = "deleted"
NO_ARTIFACT = "no_artifact"
ID_KEPT = "id_kept"
CATEGORIES = (LIVE, DELETED, NO_ARTIFACT, ID_KEPT)

_ARTIFACT_PATHS = "/api/artifacts/%"
_PATH_LENGTH = User.__table__.c.last_seen_path.type.length


@dataclasses.dataclass
class Report:
    """Counts only: never a name, a path or a user's content."""
    applied: bool
    view_rows: int = 0            # artifact_view rows in all
    view_system_rows: int = 0     # a name Loore defines: left as it is
    view_unnamed_rows: int = 0    # stored without a name: left as it is
    view_users: int = 0           # users with rows to change
    view_names: int = 0           # (user, name) pairs with rows to change
    rows: Counter = dataclasses.field(default_factory=Counter)
    names: Counter = dataclasses.field(default_factory=Counter)
    paths_to_change: int = 0
    paths_left: int = 0           # artifact addresses without a user's name

    @property
    def view_rows_to_change(self):
        return sum(self.rows.values())


def stored_path(path):
    """What #729 stores as ``last_seen_path`` for a visit to *path*
    (activity.touch_last_seen)."""
    return redact_artifact_path(path)[:_PATH_LENGTH]


def _not_a_user_name():
    return sorted(UserArtifact.SYSTEM_KINDS | {ArtifactView.USER_CREATED})


def _named():
    """Rows whose kind is a name a user gave."""
    return ArtifactView.kind.notin_(_not_a_user_name())


def _rows_of(user_id, kind):
    return ArtifactView.query.filter(ArtifactView.user_id == user_id,
                                     ArtifactView.kind == kind)


def _id_in_use(user_id, artifact_id):
    return (db.session.query(ArtifactView.id)
            .filter(ArtifactView.user_id == user_id,
                    ArtifactView.artifact_id == artifact_id)
            .first()) is not None


def key_for(user_id, kind):
    """(artifact_id, category) for the user's rows that name *kind* and
    have no id: the same artifact for the Activity tab as before."""
    live = UserArtifact.first_live_id(user_id, kind)
    if live is not None:
        return live, LIVE
    deleted = (db.session.query(func.min(UserArtifact.id))
               .filter(UserArtifact.user_id == user_id,
                       UserArtifact.kind == kind,
                       UserArtifact.deleted_at.isnot(None))
               .scalar())
    if deleted is not None and not _id_in_use(user_id, deleted):
        return deleted, DELETED
    earliest = (db.session.query(func.min(ArtifactView.id))
                .filter(ArtifactView.user_id == user_id,
                        ArtifactView.kind == kind,
                        ArtifactView.artifact_id.is_(None))
                .scalar())
    return -earliest, NO_ARTIFACT


def _end_batch(apply):
    """Commit the batch, or end the dry run's read transaction."""
    if apply:
        db.session.commit()
    else:
        db.session.rollback()


def _count(*criteria):
    return (db.session.query(func.count(ArtifactView.id))
            .filter(*criteria).scalar()) or 0


def clean_artifact_views(report, apply, batch_size=BATCH_SIZE):
    report.view_rows = _count()
    report.view_system_rows = _count(
        ArtifactView.kind.in_(sorted(UserArtifact.SYSTEM_KINDS)))
    report.view_unnamed_rows = _count(
        ArtifactView.kind == ArtifactView.USER_CREATED)
    pending = 0
    last_user = 0
    while True:
        user_id = (db.session.query(func.min(ArtifactView.user_id))
                   .filter(_named(), ArtifactView.user_id > last_user)
                   .scalar())
        if user_id is None:
            break
        last_user = user_id
        report.view_users += 1
        groups = (db.session.query(ArtifactView.kind,
                                   func.count(ArtifactView.id),
                                   func.count(ArtifactView.artifact_id))
                  .filter(ArtifactView.user_id == user_id, _named())
                  .group_by(ArtifactView.kind).all())
        for kind, total, with_id in groups:
            report.view_names += 1
            if total > with_id:
                key, category = key_for(user_id, kind)
                report.rows[category] += total - with_id
                report.names[category] += 1
                if apply:
                    _rows_of(user_id, kind).filter(
                        ArtifactView.artifact_id.is_(None),
                    ).update({ArtifactView.kind: ArtifactView.USER_CREATED,
                              ArtifactView.artifact_id: key},
                             synchronize_session=False)
            if with_id:
                report.rows[ID_KEPT] += with_id
                report.names[ID_KEPT] += 1
                if apply:
                    _rows_of(user_id, kind).filter(
                        ArtifactView.artifact_id.isnot(None),
                    ).update({ArtifactView.kind: ArtifactView.USER_CREATED},
                             synchronize_session=False)
            pending += total
            if pending >= batch_size:
                _end_batch(apply)
                pending = 0
    _end_batch(apply)


def clean_last_seen_paths(report, apply, batch_size=BATCH_SIZE):
    last_id = 0
    while True:
        page = (db.session.query(User.id, User.last_seen_path)
                .filter(User.id > last_id,
                        User.last_seen_path.like(_ARTIFACT_PATHS))
                .order_by(User.id).limit(batch_size).all())
        if not page:
            break
        for user_id, path in page:
            new = stored_path(path)
            if new == path:
                report.paths_left += 1
                continue
            report.paths_to_change += 1
            if apply:
                (db.session.query(User)
                 .filter(User.id == user_id, User.last_seen_path == path)
                 .update({User.last_seen_path: new},
                         synchronize_session=False))
        last_id = page[-1][0]
        _end_batch(apply)


def run(apply=False, batch_size=BATCH_SIZE):
    """Find (and with *apply*, change) the records that still hold a
    name a user gave an artifact. Returns a Report."""
    report = Report(applied=apply)
    clean_artifact_views(report, apply, batch_size)
    clean_last_seen_paths(report, apply, batch_size)
    return report


_EXPLAINED = {
    LIVE: "the artifact exists; stored by the id of its first version",
    DELETED: ("the artifact is deleted and waits for the daily cleanup; "
              "stored by the id of its earliest deleted version, so the "
              "cleanup removes them with it"),
    NO_ARTIFACT: ("no artifact is left under the name, or its id already "
                  "counts views stored since #729; stored by a negative "
                  "number, one per name"),
    ID_KEPT: "already stored by id; only the name is removed",
}


def _n(count, word, plural=None):
    return f"{count} {word if count == 1 else plural or word + 's'}"


def report_lines(report):
    """The command's output: counts only."""
    verb = "changed" if report.applied else "to change"
    head = ("Applied." if report.applied
            else "Dry run: nothing is changed.")
    lines = [f"Artifact names in records written before #729. {head}",
             f"artifact_view: {_n(report.view_rows, 'row')}",
             f"  {verb}: {_n(report.view_rows_to_change, 'row')} "
             f"({_n(report.view_names, 'name')}, "
             f"{_n(report.view_users, 'user')})"]
    for category in CATEGORIES:
        lines.append(f"    {_n(report.rows[category], 'row')} "
                     f"({_n(report.names[category], 'name')}): "
                     f"{_EXPLAINED[category]}")
    lines.append(f"  left as they are: {_n(report.view_system_rows, 'row')} "
                 f"with a name Loore defines, "
                 f"{_n(report.view_unnamed_rows, 'row')} stored without a "
                 f"name")
    lines.append("user.last_seen_path:")
    lines.append(f"  {verb}: {_n(report.paths_to_change, 'value')}")
    lines.append(f"  left as they are: "
                 f"{_n(report.paths_left, 'artifact address', 'artifact addresses')}"
                 f" without a name a user gave")
    if report.applied:
        lines.append("Run it again without --apply: it should show 0 rows "
                     "and 0 values to change.")
    elif report.view_rows_to_change or report.paths_to_change:
        lines.append("Run again with --apply to make these changes.")
    return lines
