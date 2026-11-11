"""Daily Celery task that finalizes soft-deleted nodes, and since #603
soft-deleted artifacts (backend/utils/artifact_deletion.py).

Runs once per day. Two distinct actions per soft-deleted candidate:

1. **Full purge** when the row has zero children in DB. Deletes the
   node's audio files, then NodeVersion / NodeTranscriptChunk / TTSChunk /
   Draft references and the row itself. Same body as the legacy
   `delete_node()` cleanup minus the orphan-children step (which is
   unnecessary by the predicate).

2. **Content wipe** when the row still has children but content hasn't yet
   been cleared. Deletes the node's audio files, sets `content = None`,
   removes versions and audio rows. Row stays as a tombstone shell until
   its descendants eventually purge.

Audio files go first, in both actions (backend/utils/audio_storage.py,
`delete_node_audio`: the node's ``user/<author>/node/<id>/``,
``nodes/<author>/<id>/`` and streaming-session folders). A file that
cannot be deleted never holds the rows back: it is logged by node id and
the purge or wipe goes on, so deleted text is gone after the grace
period whatever the disk does. The audio sweep at the end of each run
(backend/utils/audio_sweep.py) retries such files on a later day, and
deletes what earlier code left behind, a deleted reference's speech and
superseded speech (#66); an error in it is logged and never stops the
node cleanup.

The predicate "no child rows in DB" (rather than "no alive descendants")
matches Postgres' default FK behavior — `Node.parent_id` has no
`ondelete` rule so we can't `db.session.delete(parent)` while a
soft-deleted-but-not-yet-purged child row still references it. The
cascade is therefore eventual, one tree-level per daily run.

The needs-action SQL filter (`content IS NOT NULL OR no children`)
excludes long-stable tombstones from per-day re-scan; one bad row's
exception is caught and logged without poisoning the batch (per-node
commits). The candidates' ids are read into a list before the first
node is touched: a streamed query (``yield_per``) reads through a
Postgres server-side cursor, which the first per-node commit closes, so
the next fetch raised ``ProgrammingError: named cursor isn't valid
anymore`` and the run stopped after its first batch, before the
artifact purge and the audio sweep. sqlite has no such cursor.
"""

import time
from datetime import datetime, timedelta, timezone

from celery.utils.log import get_task_logger
from sqlalchemy import and_, exists, or_

from backend.celery_app import celery, flask_app
from backend.constants import SOFT_DELETE_GRACE_DAYS
from backend.extensions import db
from backend.models import (
    Node, NodeVersion, NodeTranscriptChunk, TTSChunk, Draft,
    NodeContextArtifact, ReferenceAction, ShareDraft, Thread,
)

logger = get_task_logger(__name__)


def _delete_audio(node):
    """Delete the node's audio files before any of its rows change, and
    return the FolderDeletion. A file left behind is logged by node id
    and never stops the purge or the wipe: the audio sweep retries it
    on a later run."""
    from backend.utils.audio_storage import delete_node_audio
    result = delete_node_audio(node)
    if result.skipped:
        # A symlink or a folder outside the storage root: never followed
        # or deleted.
        logger.warning(
            "cleanup_deleted_nodes: node_id=%s: %d audio folder(s) left "
            "alone (symlink or outside the storage root)",
            node.id, result.skipped)
    if result.failed:
        logger.error(
            "cleanup_deleted_nodes: node_id=%s: %d audio file(s) not "
            "deleted (%s); the audio sweep retries them",
            node.id, result.failed, ", ".join(result.errors))
    return result


def _full_purge(node):
    """Hard-delete a node + cascading dependents, its audio files first.

    Same body as the legacy delete_node() cleanup steps, minus the
    orphan-children line: the predicate guarantees zero child rows.
    """
    audio = _delete_audio(node)
    NodeVersion.query.filter_by(node_id=node.id).delete()
    NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
    TTSChunk.query.filter_by(node_id=node.id).delete()

    Draft.query.filter_by(node_id=node.id).update({"node_id": None})
    Draft.query.filter_by(parent_id=node.id).update({"parent_id": None})
    Draft.query.filter_by(llm_node_id=node.id).delete()

    NodeContextArtifact.query.filter_by(node_id=node.id).delete()
    # Circles: who the entry was shared with, and the notes about it.
    from backend.utils.circles import node_rows_filters
    for model, cond in node_rows_filters([node.id]):
        model.query.filter(cond).delete(synchronize_session=False)
    # What the reader did with the reply's recommendations (#352). Its
    # FeedPick rows go with the node (ORM cascade); the actions are
    # deleted rather than set to NULL, which would read as done outside
    # any reply and count for other recommendations of the reference.
    ReferenceAction.query.filter_by(node_id=node.id).delete()
    # The thread's user-given name (root rows only; no-op elsewhere). Kept
    # through the content wipe — it names the thread, whose replies may
    # still be alive — and goes with the root's row.
    Thread.query.filter_by(root_node_id=node.id).delete()

    Node.query.filter_by(linked_node_id=node.id).update(
        {"linked_node_id": None}
    )
    # An agentic interim step keeps continuation_node_id pointing at the
    # turn that followed it; soft-delete leaves the link (undelete restores
    # the chain), so the hard purge must clear it or the FK blocks the
    # DELETE every day.
    Node.query.filter_by(continuation_node_id=node.id).update(
        {"continuation_node_id": None}
    )
    # A share (#228) names the reply it was proposed in and the public
    # node it published; revoking keeps the public node's pointer, so a
    # republish can bring the same node back. Both foreign keys block the
    # DELETE on Postgres (sqlite does not enforce them), so clear them,
    # as the user data purge does (backend/utils/user_purge.py,
    # _REF_COLUMNS). The share stays; a republish makes a new node.
    ShareDraft.query.filter_by(source_node_id=node.id).update(
        {"source_node_id": None}
    )
    ShareDraft.query.filter_by(public_node_id=node.id).update(
        {"public_node_id": None}
    )

    # No orphan-children: predicate guarantees zero child rows.
    db.session.delete(node)
    return audio


def _wipe_content_and_versions(node):
    """Clear a tombstone's content + edit history + audio, its audio
    files first.

    Keeps the row so descendants still have a parent_id anchor; the row
    will become eligible for full purge in a later cleanup run once all
    its child rows have purged.
    """
    audio = _delete_audio(node)
    node.content = None
    NodeVersion.query.filter_by(node_id=node.id).delete()
    NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
    TTSChunk.query.filter_by(node_id=node.id).delete()
    # The files are gone; clear the URL columns too so the frontend
    # doesn't try to fetch them.
    node.audio_original_url = None
    node.audio_tts_url = None
    return audio


def sweep_answers_of_deleted_entries(cutoff):
    """Mark as deleted, as of *cutoff*, every "Ask my AI" answer whose
    source entry is gone: the row no longer exists, its content was
    wiped (a tombstone of the author's purge), or it was deleted before
    *cutoff* (its own grace period is over). The run that follows wipes
    or purges them like any deleted node. Until then they are already
    closed to everyone, their owner included
    (privacy.CircleSets.closed_answers). Ids and timestamps only. Returns how
    many were marked; commits."""
    from sqlalchemy.orm import aliased
    from backend.utils.privacy import others_thread_answer_filter
    entry = aliased(Node)
    ids = [nid for (nid,) in (
        db.session.query(Node.id)
        .outerjoin(entry, entry.id == Node.audience_root_id)
        .filter(Node.deleted_at.is_(None),
                Node.audience_root_id.isnot(None),
                others_thread_answer_filter(Node),
                or_(entry.id.is_(None),
                    and_(entry.deleted_at.isnot(None),
                         or_(entry.deleted_at <= cutoff,
                             entry.content.is_(None))))))]
    for start in range(0, len(ids), 500):
        Node.query.filter(Node.id.in_(ids[start:start + 500])).update(
            {Node.deleted_at: cutoff, Node.updated_at: Node.updated_at},
            synchronize_session=False)
    db.session.commit()
    return len(ids)


def finalize_deleted_nodes(now=None):
    """The cleanup's work, in the caller's app context: wipe content and
    audio of, or purge, every soft-deleted node past the grace period
    (deleted SOFT_DELETE_GRACE_DAYS or more before *now*, a POSIX time,
    default the current time), purge deleted artifacts, then sweep the
    audio folders left behind."""
    now = time.time() if now is None else now
    cutoff = (datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)
              - timedelta(days=SOFT_DELETE_GRACE_DAYS))

    # Circles: an "Ask my AI" answer goes with the entry it was written
    # from (it holds that entry's content in derived form).
    swept = sweep_answers_of_deleted_entries(cutoff)
    if swept:
        logger.info("cleanup_deleted_nodes: %d Ask-my-AI answer(s) of "
                    "deleted entries marked for deletion", swept)

    # "Has any child row in DB" subquery — true when the node still
    # has children referencing it (alive or soft-deleted, doesn't
    # matter for the FK constraint). When false, full-purge eligible.
    # Aliased so the inner Child.parent_id refers to a distinct row
    # while the outer Node.id is the candidate row being filtered.
    from sqlalchemy.orm import aliased
    Child = aliased(Node)
    has_any_child = exists().where(Child.parent_id == Node.id)

    # Needs-action filter: content not yet wiped, OR no children
    # (purge-eligible). Excludes long-stable tombstones from per-day
    # re-scan.
    # A node a "Delete all my content" hid (#268) is the purge's: it
    # comes back whole on a restore, or the purge deletes it with
    # everything else the request hid.
    # Ids only, read in full before the loop: the loop commits per node,
    # and a commit closes the server-side cursor a streamed query reads
    # through on Postgres (see the module docstring).
    from backend.utils.hidden_rows import hidden_ids
    candidate_ids = [nid for (nid,) in (
        db.session.query(Node.id)
        .filter(Node.deleted_at <= cutoff)
        .filter(~Node.id.in_(hidden_ids("node")))
        .filter(or_(
            Node.content.isnot(None),
            ~has_any_child,
        ))
        .order_by(Node.deleted_at.asc(), Node.id.asc())
    )]

    purged = 0
    wiped = 0
    errors = 0
    audio_files = 0
    audio_errors = 0
    for node_id in candidate_ids:
        try:
            # Locked until this node's commit or rollback: a restore
            # (re-import, share republish) still uncommitted when the
            # node's turn comes is waited for, then seen here, instead
            # of the DELETE or the wipe landing on the restored row.
            node = db.session.get(Node, node_id, with_for_update=True,
                                  populate_existing=True)
            if (node is None or node.deleted_at is None
                    or node.deleted_at > cutoff):
                # Gone, or restored, since the ids were read.
                db.session.rollback()
                continue
            child_count = Node.query.filter_by(parent_id=node.id).count()
            if child_count == 0:
                audio = _full_purge(node)
                purged += 1
            elif node.content is not None:
                audio = _wipe_content_and_versions(node)
                wiped += 1
            else:
                # Stable tombstone, content already None, children
                # still exist — skip until next descendant cleanup pass.
                db.session.rollback()  # releases the row lock
                continue
            db.session.commit()
            audio_files += audio.files
            audio_errors += 1 if audio.failed else 0
        except Exception:
            db.session.rollback()
            logger.exception(
                "cleanup_deleted_nodes failed on node_id=%s", node_id,
            )
            errors += 1
            continue

    # Deleted artifacts (#603) go for good after the same grace
    # period, with the sessions' pins to their versions.
    from backend.utils.artifact_deletion import purge_deleted_artifacts
    artifacts = purge_deleted_artifacts(cutoff)

    sweep = _sweep_orphaned_audio(now)

    logger.info(
        "cleanup_deleted_nodes: purged=%d wiped=%d errors=%d "
        "audio_files_deleted=%d audio_errors=%d "
        "artifact_versions_purged=%d artifact_errors=%d "
        "orphan_folders_deleted=%d orphan_files_deleted=%d "
        "orphan_files_failed=%d orphan_sweep_failed=%s",
        purged, wiped, errors, audio_files, audio_errors,
        artifacts["purged"], artifacts["errors"],
        sweep["folders"], sweep["files"], sweep["failed"], sweep["crashed"],
    )
    return {"purged": purged, "wiped": wiped, "errors": errors,
            "audio_files_deleted": audio_files,
            "audio_errors": audio_errors,
            "artifact_versions_purged": artifacts["purged"],
            "artifact_errors": artifacts["errors"],
            "orphan_folders_deleted": sweep["folders"],
            "orphan_files_deleted": sweep["files"],
            "orphan_files_failed": sweep["failed"],
            "orphan_sweep_failed": sweep["crashed"]}


def _sweep_orphaned_audio(now):
    """Run the audio sweep (backend/utils/audio_sweep.py) for the files
    left behind, after the nodes are done. Its errors are logged and
    returned, never raised: the node cleanup has committed and goes on
    whatever the sweep meets."""
    out = {"folders": 0, "files": 0, "failed": 0, "crashed": False}
    try:
        from backend.utils.audio_sweep import sweep
        result = sweep(apply=True, now=now, measure_kept=False)
    except Exception:
        db.session.rollback()
        logger.exception("cleanup_deleted_nodes: audio sweep failed")
        out["crashed"] = True
        return out
    out.update(folders=result["done"][0], files=result["done"][1],
               failed=result["failed"])
    if result["failed"]:
        ids = result["failed_ids"]
        logger.error(
            "cleanup_deleted_nodes: audio sweep: %d audio file(s) not "
            "deleted (%s) in %d folder(s), e.g. ids=%s; retried on the "
            "next run",
            result["failed"], ", ".join(result["errors"]), len(ids),
            ids[:20])
    return out


@celery.task(name='backend.tasks.node_cleanup.cleanup_deleted_nodes')
def cleanup_deleted_nodes():
    """Daily cleanup: wipe content + finalize purge for soft-deleted nodes."""
    with flask_app.app_context():
        return finalize_deleted_nodes()
