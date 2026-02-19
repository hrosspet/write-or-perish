"""
Shared helper for moving streaming audio chunks from a draft session
to a node.  Used by reflect / orient routes so that the original
voice recording is available for playback in the Log view.
"""
import os
import pathlib
import shutil

from flask import current_app

from backend.extensions import db
from backend.models import Draft, NodeTranscriptChunk
from backend.utils.webm_utils import fix_last_chunk_duration, is_ffmpeg_available

AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()


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

    if draft_audio_dir.exists():
        # Fix last-chunk WebM duration metadata if possible
        if is_ffmpeg_available():
            success, message = fix_last_chunk_duration(str(draft_audio_dir))
            if success:
                current_app.logger.info(
                    f"Fixed last chunk duration: {message}"
                )
            else:
                current_app.logger.warning(
                    f"Could not fix last chunk duration: {message}"
                )

        node_audio_dir = (
            AUDIO_STORAGE_ROOT / f"nodes/{user_id}/{node.id}"
        )
        try:
            node_audio_dir.mkdir(parents=True, exist_ok=True)
            for file_path in draft_audio_dir.iterdir():
                shutil.move(
                    str(file_path),
                    str(node_audio_dir / file_path.name),
                )
            draft_audio_dir.rmdir()
            current_app.logger.info(
                f"Moved audio from {draft_audio_dir} -> {node_audio_dir}"
            )
        except Exception as e:
            current_app.logger.warning(
                f"Failed to move audio files: {e}"
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
