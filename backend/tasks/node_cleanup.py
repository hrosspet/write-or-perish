"""Daily Celery task that finalizes soft-deleted nodes.

Runs once per day. Two distinct actions per soft-deleted candidate:

1. **Full purge** when the row has zero children in DB. Deletes
   NodeVersion / NodeTranscriptChunk / TTSChunk / Draft references and the
   row itself. Same body as the legacy `delete_node()` cleanup minus the
   orphan-children step (which is unnecessary by the predicate).

2. **Content wipe** when the row still has children but content hasn't yet
   been cleared. Sets `content = None`, removes versions and audio
   artifacts. Row stays as a tombstone shell until its descendants
   eventually purge.

The predicate "no child rows in DB" (rather than "no alive descendants")
matches Postgres' default FK behavior — `Node.parent_id` has no
`ondelete` rule so we can't `db.session.delete(parent)` while a
soft-deleted-but-not-yet-purged child row still references it. The
cascade is therefore eventual, one tree-level per daily run.

The needs-action SQL filter (`content IS NOT NULL OR no children`)
excludes long-stable tombstones from per-day re-scan; one bad row's
exception is caught and logged without poisoning the batch (per-node
commits).
"""

from datetime import datetime, timedelta

from celery.utils.log import get_task_logger
from sqlalchemy import exists, or_

from backend.celery_app import celery, flask_app
from backend.constants import SOFT_DELETE_GRACE_DAYS
from backend.extensions import db
from backend.models import (
    Node, NodeVersion, NodeTranscriptChunk, TTSChunk, Draft,
    NodeContextArtifact,
)

logger = get_task_logger(__name__)


def _full_purge(node):
    """Hard-delete a node + cascading dependents.

    Same body as the legacy delete_node() cleanup steps, minus the
    orphan-children line: the predicate guarantees zero child rows.
    """
    NodeVersion.query.filter_by(node_id=node.id).delete()
    NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
    TTSChunk.query.filter_by(node_id=node.id).delete()

    Draft.query.filter_by(node_id=node.id).update({"node_id": None})
    Draft.query.filter_by(parent_id=node.id).update({"parent_id": None})
    Draft.query.filter_by(llm_node_id=node.id).delete()

    NodeContextArtifact.query.filter_by(node_id=node.id).delete()

    Node.query.filter_by(linked_node_id=node.id).update(
        {"linked_node_id": None}
    )

    # No orphan-children: predicate guarantees zero child rows.
    db.session.delete(node)


def _wipe_content_and_versions(node):
    """Clear a tombstone's content + edit history + audio artifacts.

    Keeps the row so descendants still have a parent_id anchor; the row
    will become eligible for full purge in a later cleanup run once all
    its child rows have purged.
    """
    node.content = None
    NodeVersion.query.filter_by(node_id=node.id).delete()
    NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
    TTSChunk.query.filter_by(node_id=node.id).delete()
    # Audio file URLs are kept on the row but the underlying chunks are
    # gone; clear the URL columns too so the frontend doesn't try to
    # fetch what's no longer there.
    node.audio_original_url = None
    node.audio_tts_url = None


@celery.task(name='backend.tasks.node_cleanup.cleanup_deleted_nodes')
def cleanup_deleted_nodes():
    """Daily cleanup: wipe content + finalize purge for soft-deleted nodes."""
    with flask_app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=SOFT_DELETE_GRACE_DAYS)

        # "Has any child row in DB" subquery — true when the node still
        # has children referencing it (alive or soft-deleted, doesn't
        # matter for the FK constraint). When false, full-purge eligible.
        has_any_child = exists().where(Node.parent_id == Node.id)
        # Note: the above expression refers to Node.id twice — the outer
        # one is what we filter on. SQLAlchemy resolves this correctly
        # because of the EXISTS scoping; we rebuild explicitly below.
        from sqlalchemy.orm import aliased
        Child = aliased(Node)
        has_any_child = exists().where(Child.parent_id == Node.id)

        # Needs-action filter: content not yet wiped, OR no children
        # (purge-eligible). Excludes long-stable tombstones from per-day
        # re-scan.
        candidates = (
            Node.query
            .filter(Node.deleted_at <= cutoff)
            .filter(or_(
                Node.content.isnot(None),
                ~has_any_child,
            ))
            .order_by(Node.deleted_at.asc())
            .yield_per(100)
        )

        purged = 0
        wiped = 0
        errors = 0
        for node in candidates:
            try:
                child_count = Node.query.filter_by(parent_id=node.id).count()
                if child_count == 0:
                    _full_purge(node)
                    purged += 1
                elif node.content is not None:
                    _wipe_content_and_versions(node)
                    wiped += 1
                # Else: stable tombstone, content already None, children
                # still exist — skip until next descendant cleanup pass.
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception(
                    "cleanup_deleted_nodes failed on node_id=%s", node.id,
                )
                errors += 1
                continue

        logger.info(
            "cleanup_deleted_nodes: purged=%d wiped=%d errors=%d",
            purged, wiped, errors,
        )
        return {"purged": purged, "wiped": wiped, "errors": errors}
