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

Both delete the node's audio files (#541): its folders under the node's
user_id and human_owner_id (an AI reply's speech is under the model
account's folder), and the folders of the recording sessions its rows
name, once no row names them any more. The files go after the node's
commit, so a rolled-back cleanup never leaves a row whose audio is gone.
When a crash or a failed delete comes between the two,
``backend/scripts/sweep_orphan_audio.py`` finds the node's own folders
and a wiped node's streaming session folder (the tombstone still names
it). A purged node's session folders are no longer named by any row, so
the sweep keeps and counts those.

The predicate "no child rows in DB" (rather than "no alive descendants")
matches Postgres' default FK behavior — `Node.parent_id` has no
`ondelete` rule so we can't `db.session.delete(parent)` while a
soft-deleted-but-not-yet-purged child row still references it. The
cascade is therefore eventual, one tree-level per daily run.

The needs-action SQL filter (`content IS NOT NULL OR no children`)
excludes long-stable tombstones from per-day re-scan; one bad row's
exception is caught and logged without poisoning the batch (per-node
commits). The candidates' ids are read in full first and each node is
loaded by id, so no cursor is open across those commits.
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from celery.utils.log import get_task_logger
from sqlalchemy import exists, or_

from backend.celery_app import celery, flask_app
from backend.constants import SOFT_DELETE_GRACE_DAYS
from backend.extensions import db
from backend.models import (
    Node, NodeVersion, NodeTranscriptChunk, TTSChunk, Draft,
    NodeContextArtifact, ReferenceAction, Thread,
)
from backend.utils import audio_storage

logger = get_task_logger(__name__)


class NodeAudio(NamedTuple):
    """Where a node's audio is on disk, read before its rows change."""
    node_id: int
    owner_ids: frozenset    # the node's user_id and human_owner_id
    session_ids: frozenset  # the recording sessions its rows name


def _audio_of(node):
    """*node*'s audio on disk: its own folders, and the recording sessions
    its rows name (the older streaming upload's session, and the voice
    session its transcript chunks came from, whose folder stays behind
    when moving it onto the node failed)."""
    sessions = {s for (s,) in db.session.query(
        NodeTranscriptChunk.session_id).filter(
        NodeTranscriptChunk.node_id == node.id,
        NodeTranscriptChunk.session_id.isnot(None))}
    if node.streaming_session_id:
        sessions.add(node.streaming_session_id)
    return NodeAudio(node.id,
                     frozenset({node.user_id, node.human_owner_id} - {None}),
                     frozenset(sessions))


def remove_node_audio(audio):
    """Delete the folders of *audio* (a NodeAudio) once the node's rows
    are committed: the node's own folders, and a session's folders when
    no row names the session any more. Returns how many folders were
    deleted. Never raises: a failure is logged (ids only) and the folders
    stay for the sweep script."""
    root = audio_storage.AUDIO_STORAGE_ROOT
    try:
        dirs = audio_storage.node_audio_dirs(
            root, audio.node_id, audio.owner_ids)
        for sid in sorted(audio.session_ids):
            if (audio_storage.is_storage_id(sid)
                    and not audio_storage.session_has_row(sid)):
                dirs += audio_storage.session_audio_dirs(
                    root, sid, audio.owner_ids)
    except Exception:
        db.session.rollback()
        logger.exception(
            "cleanup_deleted_nodes: audio of node_id=%s not deleted",
            audio.node_id)
        return 0
    return audio_storage.remove_audio_dirs(dirs, logger)


def _full_purge(node):
    """Hard-delete a node + cascading dependents.

    Same body as the legacy delete_node() cleanup steps, minus the
    orphan-children line: the predicate guarantees zero child rows.

    Returns the node's NodeAudio: the caller commits, then deletes the
    files with remove_node_audio().
    """
    audio = _audio_of(node)
    NodeVersion.query.filter_by(node_id=node.id).delete()
    NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
    TTSChunk.query.filter_by(node_id=node.id).delete()

    Draft.query.filter_by(node_id=node.id).update({"node_id": None})
    Draft.query.filter_by(parent_id=node.id).update({"parent_id": None})
    Draft.query.filter_by(llm_node_id=node.id).delete()

    NodeContextArtifact.query.filter_by(node_id=node.id).delete()
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

    # No orphan-children: predicate guarantees zero child rows.
    db.session.delete(node)
    return audio


def _wipe_content_and_versions(node):
    """Clear a tombstone's content + edit history + audio artifacts.

    Keeps the row so descendants still have a parent_id anchor; the row
    will become eligible for full purge in a later cleanup run once all
    its child rows have purged.

    Returns the node's NodeAudio: the caller commits, then deletes the
    files with remove_node_audio().
    """
    audio = _audio_of(node)
    node.content = None
    NodeVersion.query.filter_by(node_id=node.id).delete()
    NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
    TTSChunk.query.filter_by(node_id=node.id).delete()
    # The audio files go after the commit (remove_node_audio); clear the
    # URL columns too so the frontend doesn't try to fetch what's no
    # longer there. streaming_session_id stays: should deleting the
    # session's folder fail, the tombstone still names it, and the sweep
    # script deletes a session folder only cleaned-up tombstones name.
    node.audio_original_url = None
    node.audio_tts_url = None
    return audio


def _candidate_filter(cutoff):
    """The nodes the cleanup acts on: deleted before *cutoff*, not hidden
    by a waiting "Delete all my writing", and either not yet wiped or with
    no child rows (purge-eligible)."""
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
    # A node a "Delete all my writing" hid (#268) is the purge's: it
    # comes back whole on a restore, or the purge deletes it with
    # everything else the request hid.
    from backend.utils.hidden_rows import hidden_ids
    return (
        Node.deleted_at <= cutoff,
        ~Node.id.in_(hidden_ids("node")),
        or_(Node.content.isnot(None), ~has_any_child),
    )


# Candidates looked at again, and then processed, per batch.
CLEANUP_BATCH_SIZE = 100


def _candidate_ids(cutoff):
    """The ids of today's candidates, oldest deletion first. Ids only,
    fetched in full before anything is committed: the loop commits per
    node, and on Postgres a commit closes a streaming (yield_per) cursor,
    which used to end every run after its first 100 rows with "named
    cursor isn't valid anymore"."""
    return [nid for (nid,) in db.session.query(Node.id).filter(
        *_candidate_filter(cutoff)).order_by(
        Node.deleted_at.asc(), Node.id.asc())]


def _still_candidates(ids, cutoff):
    """The ids of *ids* that are still candidates now, in their order:
    a node restored or hidden since the first select is left alone."""
    fresh = {nid for (nid,) in db.session.query(Node.id).filter(
        Node.id.in_(ids), *_candidate_filter(cutoff))}
    return [nid for nid in ids if nid in fresh]


@celery.task(name='backend.tasks.node_cleanup.cleanup_deleted_nodes')
def cleanup_deleted_nodes():
    """Daily cleanup: wipe content + finalize purge for soft-deleted nodes."""
    with flask_app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=SOFT_DELETE_GRACE_DAYS)
        ids = _candidate_ids(cutoff)

        purged = 0
        wiped = 0
        errors = 0
        audio_folders = 0
        for start in range(0, len(ids), CLEANUP_BATCH_SIZE):
            for nid in _still_candidates(
                    ids[start:start + CLEANUP_BATCH_SIZE], cutoff):
                outcome, audio = _clean_one(nid, cutoff)
                if outcome == "purged":
                    purged += 1
                elif outcome == "wiped":
                    wiped += 1
                elif outcome == "error":
                    errors += 1
                # The rows are committed; now the files (#541).
                if audio is not None:
                    audio_folders += remove_node_audio(audio)

        logger.info(
            "cleanup_deleted_nodes: purged=%d wiped=%d errors=%d "
            "audio_folders=%d",
            purged, wiped, errors, audio_folders,
        )
        return {"purged": purged, "wiped": wiped, "errors": errors,
                "audio_folders": audio_folders}


def _clean_one(nid, cutoff):
    """Purge or wipe node *nid* and commit. Returns (outcome, NodeAudio or
    None); the outcome is "purged", "wiped", "skipped" or "error". One
    bad row's exception is logged and rolled back, never raised."""
    try:
        node = db.session.get(Node, nid)
        if node is None or node.deleted_at is None \
                or node.deleted_at > cutoff:
            return "skipped", None   # gone, or restored meanwhile
        audio, outcome = None, "skipped"
        child_count = Node.query.filter_by(parent_id=node.id).count()
        if child_count == 0:
            audio, outcome = _full_purge(node), "purged"
        elif node.content is not None:
            audio, outcome = _wipe_content_and_versions(node), "wiped"
        # Else: stable tombstone, content already None, children
        # still exist — skip until next descendant cleanup pass.
        db.session.commit()
        return outcome, audio
    except Exception:
        db.session.rollback()
        logger.exception("cleanup_deleted_nodes failed on node_id=%s", nid)
        return "error", None
