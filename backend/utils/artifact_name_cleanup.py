"""Clean-up of artifact names in records written before #729 (A.113).

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
``artifact_id``. #729's tab (``activity.artifact_views``) matches an old
row to its artifact by name and counts a name with no live artifact as
one artifact; the id keeps that count, and keeps the rule that deleting
an artifact deletes its view records (``artifact_deletion.
purge_deleted_artifacts`` deletes the rows that hold a purged version's
id):

* the user has the artifact (``LIVE``): the id of its first live version
  (``UserArtifact.first_live_id``), the id #729 stores for a new view and
  the one the tab matched the row to. The count stays.
* the artifact is deleted, the daily cleanup has not removed it yet, and
  views since #729 hold the id of one of its versions (``JOINED``): that
  id, so the old rows are counted with their newer views, as one
  artifact, and go with the artifact. The tab counted these as two
  artifacts (the old rows apart, a known inaccuracy of #729 for old
  records); after the clean-up it counts the true one (review of #735,
  2026-11-21: deleting an artifact deletes its view records, which wins
  over keeping that count).
* the artifact is deleted and waits for the cleanup, with no view since
  #729 (``DELETED``): the id of its earliest deleted version still
  stored, so the cleanup deletes the rows with the versions, as it
  deleted the rows naming it. The count stays.
* no version under the name is stored any more (``NO_ARTIFACT``): minus
  the id of the earliest of these rows. Negative, so it is no artifact's
  id; one value per user and name, so the tab still counts each name as
  one artifact. These rows go when the account or its content is
  deleted, like the rest.

A row that already has an ``artifact_id`` and still a name (``ID_KEPT``;
none expected) keeps the id and loses the name. Rows of the names Loore
defines and rows already stored without a name are left as they are.

``user.last_seen_path``: a path that carries a user-given name becomes
what #729 stores for a new visit (``stored_path``). The UPDATE sets that
column alone, only while it still holds the path that was read, so a
newer visit is never overwritten; ``User`` has no ``onupdate`` column and
``last_seen_at`` is not touched.

Skipped: a user with a deletion of their content or of their account
waiting or running (an active ``UserDataPurge``), or whose artifacts such
a request hid (``UserDataPurgeHidden`` rows of ``user_artifact``). The
hidden-rows hook leaves those artifacts out of every query, so their
rows would be matched as if the artifacts were gone, and be wrong after a
restore. Nothing of theirs changes; a later run, once the request is done
or cancelled, cleans what is left (a purge deletes their rows from before
the request anyway).

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

from sqlalchemy import func, select

from backend.extensions import db
from backend.models import (
    ArtifactView, User, UserArtifact, UserDataPurge, UserDataPurgeHidden,
)
from backend.utils.activity import redact_artifact_path

# Rows changed per commit (artifact_view) and accounts read per page
# (last_seen_path). A heuristic: small enough that a commit holds row
# locks for milliseconds on a small machine, large enough that a few
# thousand rows take a handful of commits.
BATCH_SIZE = 500

# Where artifact_view rows go (see the module docstring).
LIVE = "live"
JOINED = "joined"
DELETED = "deleted"
NO_ARTIFACT = "no_artifact"
ID_KEPT = "id_kept"
CATEGORIES = (LIVE, JOINED, DELETED, NO_ARTIFACT, ID_KEPT)

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
    skipped_users: set = dataclasses.field(default_factory=set)  # ids
    skipped_rows: int = 0
    skipped_paths: int = 0

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


def held(user_ids):
    """Those of *user_ids* skipped for now: a deletion of their content or
    account waits or runs, or a request hid their artifacts (a failed one
    keeps them hidden)."""
    user_ids = list(user_ids)
    if not user_ids:
        return set()
    waiting = db.session.query(UserDataPurge.user_id).filter(
        UserDataPurge.user_id.in_(user_ids),
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES))
    hid = (db.session.query(UserDataPurge.user_id)
           .join(UserDataPurgeHidden,
                 UserDataPurgeHidden.job_id == UserDataPurge.id)
           .filter(UserDataPurge.user_id.in_(user_ids),
                   UserDataPurgeHidden.kind == UserArtifact.__tablename__))
    return {uid for (uid,) in waiting.union(hid)}


def key_for(user_id, kind):
    """(artifact_id, category) for the user's rows that name *kind* and
    have no id (see the module docstring)."""
    live = UserArtifact.first_live_id(user_id, kind)
    if live is not None:
        return live, LIVE
    deleted_ids = select(UserArtifact.id).where(
        UserArtifact.user_id == user_id, UserArtifact.kind == kind,
        UserArtifact.deleted_at.isnot(None))
    joined = (db.session.query(func.min(ArtifactView.artifact_id))
              .filter(ArtifactView.user_id == user_id,
                      ArtifactView.artifact_id.in_(deleted_ids))
              .scalar())
    if joined is not None:
        return joined, JOINED
    deleted = (db.session.query(func.min(UserArtifact.id))
               .filter(UserArtifact.user_id == user_id,
                       UserArtifact.kind == kind,
                       UserArtifact.deleted_at.isnot(None))
               .scalar())
    if deleted is not None:
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


def _clean_name(report, apply, user_id, kind, total, with_id):
    """The user's rows under one name, in one statement per kind of row
    (in the caller's transaction)."""
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
        if held([user_id]):
            report.skipped_users.add(user_id)
            report.skipped_rows += _count(
                _named(), ArtifactView.user_id == user_id)
            continue
        report.view_users += 1
        groups = (db.session.query(ArtifactView.kind,
                                   func.count(ArtifactView.id),
                                   func.count(ArtifactView.artifact_id))
                  .filter(ArtifactView.user_id == user_id, _named())
                  .group_by(ArtifactView.kind).all())
        for kind, total, with_id in groups:
            _clean_name(report, apply, user_id, kind, total, with_id)
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
        waiting = held(user_id for user_id, _ in page)
        for user_id, path in page:
            new = stored_path(path)
            if new == path:
                report.paths_left += 1
            elif user_id in waiting:
                report.skipped_users.add(user_id)
                report.skipped_paths += 1
            else:
                report.paths_to_change += 1
                if apply:
                    (db.session.query(User)
                     .filter(User.id == user_id,
                             User.last_seen_path == path)
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
    JOINED: ("the artifact is deleted and waits for the daily cleanup, "
             "and was viewed since #729 too; stored by the id those views "
             "hold, so they are counted with their newer views (one "
             "artifact, not two) and the cleanup removes them with it"),
    DELETED: ("the artifact is deleted and waits for the daily cleanup; "
              "stored by the id of its earliest deleted version, so the "
              "cleanup removes them with it"),
    NO_ARTIFACT: ("no version is left under the name; stored by a "
                  "negative number, one per name"),
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
    skipped = len(report.skipped_users)
    lines.append(
        f"skipped: {_n(skipped, 'user')} whose content or account deletion "
        f"is waiting or running ({_n(report.skipped_rows, 'row')}, "
        f"{_n(report.skipped_paths, 'value')}), left as they are."
        + (" A later run cleans them once the deletion is done or "
           "cancelled." if skipped else ""))
    if report.applied:
        lines.append("Run it again without --apply: it should show 0 rows "
                     "and 0 values to change.")
    elif report.view_rows_to_change or report.paths_to_change:
        lines.append("Run again with --apply to make these changes.")
    return lines
