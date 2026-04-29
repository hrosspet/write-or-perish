"""
Shared helper for moving streaming audio chunks from a draft session
to a node.  Used by voice route so that the original recording is
available for playback in the Log view.
"""
import os
import pathlib
import shutil

from flask import current_app

from backend.extensions import db
from backend.models import Draft, NodeTranscriptChunk

AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()


def move_draft_audio_to_node_dir(
    draft_audio_dir: pathlib.Path,
    node_audio_dir: pathlib.Path,
    logger,
) -> None:
    """Move every audio file from a streaming-session draft dir into the
    node's permanent audio dir, then remove the emptied draft dir.

    Skips `init.webm` (and `init.webm.enc`) — that file is the extracted
    EBML/Segment/Tracks prefix cached on chunk-0 upload so later batches
    can remux into valid WebM, and has no purpose in node storage.
    Callers pass their own logger so the messages land with the right
    request/task context.

    Best-effort: exceptions are logged at warning level rather than
    raised, since by the time this is called the DB record has already
    been committed and a move failure shouldn't undo that.
    """
    if not draft_audio_dir.exists():
        return
    try:
        node_audio_dir.mkdir(parents=True, exist_ok=True)
        for fp in draft_audio_dir.iterdir():
            if fp.name.startswith("init.webm"):
                fp.unlink()
                continue
            shutil.move(str(fp), str(node_audio_dir / fp.name))
        draft_audio_dir.rmdir()
        logger.info(
            f"Moved audio from {draft_audio_dir} -> {node_audio_dir}"
        )
    except Exception as e:
        logger.warning(f"Failed to move audio files: {e}")


def attach_streaming_audio_to_node(session_id, node, user_id):
    """
    Move audio chunks from a draft streaming session to a node.

    * Moves files: drafts/{user_id}/{session_id}/ -> nodes/{user_id}/{node.id}/
    * Updates NodeTranscriptChunk rows to reference the node
    * Marks the node as ``streaming_transcription = True``
    * Deletes the draft record

    This is a best-effort operation: if the draft or audio directory
    doesn't exist the node still works (just without playback of the
    original recording).
    """
    if not session_id:
        return

    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=user_id,
    ).first()

    draft_audio_dir = AUDIO_STORAGE_ROOT / f"drafts/{user_id}/{session_id}"

    node_audio_dir = AUDIO_STORAGE_ROOT / f"nodes/{user_id}/{node.id}"
    move_draft_audio_to_node_dir(
        draft_audio_dir, node_audio_dir, current_app.logger,
    )

    # Point transcript-chunk rows at the node
    NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
    ).update({"node_id": node.id})

    # Mark the node so the playback path knows it has chunked audio
    node.streaming_transcription = True

    # Clean up the draft
    if draft:
        db.session.delete(draft)

    db.session.commit()
