"""
Audio storage helpers: building paths under the storage root, and moving
a streaming draft session's audio and transcript rows to the node it
becomes (so the original recording plays in the Log view).
"""
import os
import pathlib
import re
import shutil

from flask import current_app

from backend.extensions import db
from backend.models import Draft, NodeTranscriptChunk

AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()

# A folder name under the storage root that arrives in a request, such
# as a streaming session's id (a server uuid4 the client sends back).
# Letters, digits, '-' and '_', starting with a letter or digit, so it is
# always one plain folder name.
_STORAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def is_storage_id(value) -> bool:
    """True when *value* is a string that may name a folder under the
    storage root (see _STORAGE_ID_RE)."""
    return isinstance(value, str) and _STORAGE_ID_RE.fullmatch(value) is not None


def storage_path(root, *parts) -> pathlib.Path:
    """``root/part/part/...`` for the folders and files audio is kept in,
    e.g. ``storage_path(root, "drafts", user_id, session_id)``.

    Every part must be a non-negative int (a user or node id) or a string
    that passes is_storage_id(); anything else raises ValueError. The
    joined path is also checked to stay inside *root*, so no part can
    name a folder outside it."""
    segments = []
    for part in parts:
        if isinstance(part, bool):
            raise ValueError(f"Invalid storage path part: {part!r}")
        if isinstance(part, int):
            if part < 0:
                raise ValueError(f"Invalid storage path part: {part!r}")
            part = str(part)
        if not is_storage_id(part):
            raise ValueError(f"Invalid storage path part: {part!r}")
        segments.append(part)
    root = pathlib.Path(root)
    path = root.joinpath(*segments)
    base = os.path.normpath(str(root))
    if os.path.commonpath([base, os.path.normpath(str(path))]) != base:
        raise ValueError(f"Storage path outside {root}: {path}")
    return path


def clear_tts_artifacts(entity) -> bool:
    """Invalidate any generated TTS audio for a Node, UserProfile or
    ExternalItem (saved reference).

    Called when the entity's text content is edited so the user doesn't
    keep hearing audio that no longer matches the text (#66). Clears the
    scalar `audio_tts_url` AND the per-chunk `TTSChunk` rows used by the
    streaming player (clearing only the URL would leave chunked replay
    resurfacing the old audio), and resets the async-task tracking
    columns so the UI shows TTS as not-yet-generated.

    DB-only, mirroring the row cleanup in tasks/node_cleanup.py — on-disk
    audio files are left as orphans (issue #66 "Option 1": require an
    explicit regenerate; file GC is a separate follow-up).

    Returns True if anything was cleared (caller still owns the commit).
    """
    from backend.models import Node, UserProfile, ExternalItem, TTSChunk

    if isinstance(entity, Node):
        fk = {"node_id": entity.id}
    elif isinstance(entity, UserProfile):
        fk = {"profile_id": entity.id}
    elif isinstance(entity, ExternalItem):
        fk = {"item_id": entity.id}
    else:
        raise TypeError(f"clear_tts_artifacts: unsupported entity {type(entity)!r}")

    had_audio = bool(entity.audio_tts_url)
    deleted = TTSChunk.query.filter_by(**fk).delete(synchronize_session=False)

    entity.audio_tts_url = None
    # These columns exist on both Node and UserProfile.
    entity.tts_task_id = None
    entity.tts_task_status = None
    entity.tts_task_progress = 0

    return had_audio or bool(deleted)


def list_streaming_audio_files(chunk_dir: pathlib.Path) -> list:
    """Return sorted list of streaming-recording chunk files for playback.

    Searches `chunk_dir` for chunk_*.{webm,mp4}[.enc] first (recording
    in-progress or pre-batch-transcription state), then falls back to
    batch_*.{webm,mp4}[.enc] (the modern post-batch-transcription
    format). Within each tier, prefers plain files over encrypted to
    match prior endpoint behavior. Returns [] if nothing matches.

    The `.legacy_backup` suffix used by `backfill_legacy_chunks.py` is
    deliberately not matched — those files exist for rollback only and
    must not be served to clients.
    """
    if not chunk_dir.exists():
        return []
    for prefix in ("chunk", "batch"):
        for ext in (".webm", ".mp4"):
            plain = sorted(chunk_dir.glob(f"{prefix}_*{ext}"))
            if plain:
                return plain
            enc = sorted(chunk_dir.glob(f"{prefix}_*{ext}.enc"))
            if enc:
                return enc
    return []


def move_draft_audio_to_node_dir(
    draft_audio_dir: pathlib.Path,
    node_audio_dir: pathlib.Path,
    logger,
) -> None:
    """Move every audio file from a streaming-session draft dir into the
    node's permanent audio dir, then remove the emptied draft dir.

    Skips `init.{webm,mp4}` (and their `.enc` variants) — those files
    are the format-specific init segments cached on chunk-0 upload so
    later batches can remux into valid output, and have no purpose in
    node storage. Callers pass their own logger so the messages land
    with the right request/task context.

    Best-effort: exceptions are logged at warning level rather than
    raised, since by the time this is called the DB record has already
    been committed and a move failure shouldn't undo that.
    """
    if not draft_audio_dir.exists():
        return
    try:
        node_audio_dir.mkdir(parents=True, exist_ok=True)
        for fp in draft_audio_dir.iterdir():
            # Match init.webm, init.webm.enc, init.mp4, init.mp4.enc.
            # The dir is server-controlled, never holds user-uploaded
            # files, so a permissive prefix match is safe here.
            if fp.name.startswith("init."):
                fp.unlink()
                continue
            shutil.move(str(fp), str(node_audio_dir / fp.name))
        draft_audio_dir.rmdir()
        logger.info(
            f"Moved audio from {draft_audio_dir} -> {node_audio_dir}"
        )
    except Exception as e:
        logger.warning(f"Failed to move audio files: {e}")


def move_session_audio_to_node(draft, node, logger, root=None) -> None:
    """Move *draft*'s streaming session to *node*: its audio files from
    ``drafts/<uid>/<session>/`` to ``nodes/<uid>/<node id>/`` and its
    transcript chunk rows (the rows with the draft's session id) onto the
    node. The caller has made sure the draft and the node belong to the
    same user, and commits. *root* defaults to AUDIO_STORAGE_ROOT."""
    root = AUDIO_STORAGE_ROOT if root is None else root
    move_draft_audio_to_node_dir(
        storage_path(root, "drafts", draft.user_id, draft.session_id),
        storage_path(root, "nodes", draft.user_id, node.id),
        logger,
    )
    NodeTranscriptChunk.query.filter_by(
        session_id=draft.session_id,
    ).update({"node_id": node.id})


def attach_streaming_audio_to_node(session_id, node, user_id):
    """
    Move audio chunks from a draft streaming session to a node.

    * Moves files: drafts/{user_id}/{session_id}/ -> nodes/{user_id}/{node.id}/
    * Updates the session's NodeTranscriptChunk rows to reference the node
    * Marks the node as ``streaming_transcription = True``
    * Deletes the draft record

    Only *user_id*'s own draft session moves, and only onto a node of
    theirs: nothing happens when the session id is malformed, the draft
    is not theirs, or the node belongs to someone else. This is a
    best-effort operation: without the draft the node still works (just
    without playback of the original recording).
    """
    if not is_storage_id(session_id):
        return
    if node.user_id != user_id:
        current_app.logger.warning(
            "Not attaching session %s of user %s to node %s of user %s",
            session_id, user_id, node.id, node.user_id)
        return

    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=user_id,
    ).first()
    if draft is None:
        return

    move_session_audio_to_node(draft, node, current_app.logger)

    # Mark the node so the playback path knows it has chunked audio
    node.streaming_transcription = True

    # Clean up the draft
    db.session.delete(draft)

    db.session.commit()
