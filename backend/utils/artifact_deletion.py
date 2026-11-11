"""Deleting an artifact (#603).

Peter, 2026-11-06: deletion works like node deletion. The user's delete
is a soft delete: every version of the artifact is stamped
``deleted_at`` at once, and from then on no reader sees any of them
(``UserArtifact.live()`` / ``get_live()`` / ``latest_for()``): not the
artifact list, the version history, the AI's artifacts index, the
session pins, ``read_artifact``, exports or quote preambles. There is no
restore UI. After SOFT_DELETE_GRACE_DAYS the daily cleanup
(backend/tasks/node_cleanup.py) deletes the rows for good, with the
sessions' pins to them.

Only custom artifacts can be deleted (``UserArtifact.UNDELETABLE_KINDS``)
and only by their owner: every query here is scoped by the user's id.
"""
import logging
from datetime import datetime

from sqlalchemy import func

from backend.extensions import db
from backend.models import ArtifactView, NodeContextArtifact, UserArtifact

logger = logging.getLogger(__name__)

# Ids per DELETE statement in the purge (Postgres takes far more; this
# keeps one statement small). INTRODUCED CONSTANT.
PURGE_CHUNK = 500


def soft_delete_artifact(user_id, kind, now=None):
    """Stamp every live version of *user_id*'s *kind* as deleted.

    Returns how many versions it stamped (0: nothing to delete). The
    caller checks ``is_deletable_kind`` first and commits."""
    now = now or datetime.utcnow()
    return (UserArtifact.live()
            .filter(UserArtifact.user_id == user_id,
                    UserArtifact.kind == kind)
            .update({UserArtifact.deleted_at: now},
                    synchronize_session="fetch"))


def deleted_kinds(user_id):
    """The user's kinds that were deleted and not made again: a link to
    one says the artifact was deleted instead of opening an empty one.
    Kind names only, never content."""
    deleted = {k for (k,) in db.session.query(UserArtifact.kind).filter(
        UserArtifact.user_id == user_id,
        UserArtifact.deleted_at.isnot(None)).distinct()}
    if not deleted:
        return []
    live = {k for (k,) in db.session.query(UserArtifact.kind).filter(
        UserArtifact.user_id == user_id,
        UserArtifact.deleted_at.is_(None),
        UserArtifact.kind.in_(list(deleted))).distinct()}
    return sorted(deleted - live)


def deletion_stamp(user_id):
    """When the user last deleted an artifact (None: never, or every
    deleted version is purged), as a string for a cache key.

    The #192 render cache keys the rendered system prompt by it, so a
    render built before a delete (its artifacts index still lists the
    artifact) is never served after it. It reads the rows a waiting
    "Delete all my content" hid too (#268), so hiding them does not move
    the stamp back to one an older render was stored under."""
    from backend.utils.hidden_rows import including_hidden_rows
    with including_hidden_rows():
        stamp = (db.session.query(func.max(UserArtifact.deleted_at))
                 .filter(UserArtifact.user_id == user_id)
                 .scalar())
    if stamp is None:
        return None
    if isinstance(stamp, str):  # sqlite returns the raw text
        return stamp.replace(" ", "T")
    return stamp.isoformat()


def purge_deleted_artifacts(cutoff):
    """Delete for good the artifact versions deleted before *cutoff*.

    One artifact (user, kind) per transaction, so one bad group does not
    poison the rest. The sessions' pins to those versions
    (NodeContextArtifact, which has no foreign key to the rows) go first,
    so no session keeps a pin to a row that is gone. The rows go with
    their content. When no live version of the kind is left, its view
    pings (ArtifactView, admin Activity) go too: they name the artifact.

    Rows a waiting "Delete all my content" hid (#268) are the purge's:
    the hidden-rows hook leaves them out of the SELECT below.
    Returns {"purged": versions deleted, "errors": groups that failed}.
    """
    groups = {}
    for row_id, user_id, kind in (
            db.session.query(UserArtifact.id, UserArtifact.user_id,
                             UserArtifact.kind)
            .filter(UserArtifact.deleted_at.isnot(None),
                    UserArtifact.deleted_at <= cutoff)
            .order_by(UserArtifact.id.asc())):
        groups.setdefault((user_id, kind), []).append(row_id)

    purged = 0
    errors = 0
    for (user_id, kind), ids in groups.items():
        try:
            gone = 0
            for start in range(0, len(ids), PURGE_CHUNK):
                chunk = ids[start:start + PURGE_CHUNK]
                NodeContextArtifact.query.filter(
                    NodeContextArtifact.artifact_type == "user_artifact",
                    NodeContextArtifact.artifact_id.in_(chunk),
                ).delete(synchronize_session=False)
                gone += UserArtifact.query.filter(
                    UserArtifact.id.in_(chunk),
                ).delete(synchronize_session=False)
            if UserArtifact.version_count(user_id, kind) == 0:
                ArtifactView.query.filter_by(
                    user_id=user_id, kind=kind,
                ).delete(synchronize_session=False)
            db.session.commit()
            purged += gone
        except Exception:
            db.session.rollback()
            logger.exception(
                "purge_deleted_artifacts failed for user_id=%s (%d rows)",
                user_id, len(ids))
            errors += 1
    return {"purged": purged, "errors": errors}
