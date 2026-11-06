"""
Audio storage helpers: building paths under the storage root, finding and
deleting a node's audio folders, and moving a streaming draft session's
audio and transcript rows to the node it becomes (so the original
recording plays in the Log view).
"""
import logging
import os
import pathlib
import re
import shutil
from typing import NamedTuple

from flask import current_app

from backend.extensions import db
from backend.models import Draft, NodeTranscriptChunk

logger = logging.getLogger(__name__)

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


# ── A node's audio on disk ──────────────────────────────────────────────
#
# A node's audio is kept in folders named by the node's author, which for
# an AI reply is the model's account (backend/routes/media.py,
# ``_node_id_in``):
#
# - ``user/<author>/node/<node id>/``: an uploaded recording
#   (``original.*``) and generated speech (``tts.*``, ``tts_chunk_*``,
#   also a reply spoken while it is written);
# - ``nodes/<author>/<node id>/``: a recording's chunks, moved there from
#   its streaming session when the recording became the node;
# - ``streaming/<author>/<session id>/``: the chunks of a recording made
#   with the node streaming endpoint, until its transcription finishes.
#
# Each file is ``.enc`` when encrypted at rest and plain otherwise.

class FolderDeletion(NamedTuple):
    """What delete_folders did."""
    files: int = 0       # files (and symlinks, never followed) deleted
    bytes: int = 0       # their size on disk
    failed: int = 0      # files that could not be deleted
    errors: tuple = ()   # the failures' error types, e.g. ("PermissionError",)
    skipped: int = 0     # folders left alone: a symlink, or not inside the root


def node_audio_dirs(node, root=None) -> list:
    """The folders that hold *node*'s audio (see above), for the node's
    author and its human owner (the same pair the user purge uses, so an
    AI reply's folder under the model account is found). Folders that
    do not exist are included; delete_folders passes over them."""
    root = AUDIO_STORAGE_ROOT if root is None else root
    dirs = []
    for author in sorted({node.user_id, node.human_owner_id} - {None}):
        dirs.append(storage_path(root, "user", author, "node", node.id))
        dirs.append(storage_path(root, "nodes", author, node.id))
    session_id = getattr(node, "streaming_session_id", None)
    if node.user_id is not None and is_storage_id(session_id):
        # A server uuid4 made for this node (nodes.py streaming/init).
        dirs.append(storage_path(root, "streaming", node.user_id, session_id))
    return dirs


def _inside(real_root, path) -> bool:
    """True when *path*, with symlinks resolved, is strictly inside
    *real_root* (itself already resolved)."""
    try:
        real = os.path.realpath(path)
        return (real != real_root
                and os.path.commonpath([real_root, real]) == real_root)
    except (OSError, ValueError):
        return False


def delete_folders(dirs, root=None) -> FolderDeletion:
    """Delete every file under *dirs*, then the emptied folders.

    Reads names and sizes only, never a file's content. Symlinks are
    never followed: one inside a folder is removed as a link (its target
    stays), and a folder that is itself a symlink, or that resolves
    outside the storage *root* (default AUDIO_STORAGE_ROOT), is left
    alone and counted as skipped. A missing folder or file is not an
    error. A file that cannot be deleted is counted as failed with its
    error type, and the rest goes on; the caller decides what to log
    (paths hold ids only) and when to try again."""
    root = AUDIO_STORAGE_ROOT if root is None else root
    real_root = os.path.realpath(root)
    totals = [0, 0, 0]   # files, bytes, failed
    errors = set()
    skipped = 0
    for d in dirs:
        d = str(d)
        if not os.path.lexists(d):
            continue
        if (os.path.islink(d) or not os.path.isdir(d)
                or not _inside(real_root, d)):
            skipped += 1
            continue
        _empty_folder(d, totals, errors)
        for dirpath, _, _ in os.walk(d, topdown=False, followlinks=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass   # not empty: a file in it failed, and is counted
    return FolderDeletion(totals[0], totals[1], totals[2],
                          tuple(sorted(errors)), skipped)


def delete_files(paths, root=None) -> FolderDeletion:
    """Delete the single files *paths*, as delete_folders does for the
    files under a folder: names and sizes only, a symlink removed as a
    link. A path that is a folder, or whose folder resolves outside the
    storage *root*, is left alone and counted as skipped."""
    root = AUDIO_STORAGE_ROOT if root is None else root
    real_root = os.path.realpath(root)
    totals = [0, 0, 0]   # files, bytes, failed
    errors = set()
    skipped = 0
    for p in paths:
        p = str(p)
        if not os.path.lexists(p):
            continue
        if ((os.path.isdir(p) and not os.path.islink(p))
                or not _inside(real_root, os.path.dirname(p))):
            skipped += 1
            continue
        _unlink(p, totals, errors)
    return FolderDeletion(totals[0], totals[1], totals[2],
                          tuple(sorted(errors)), skipped)


def _unlink(path, totals, errors):
    """Unlink one file or symlink, adding to *totals* (files, bytes,
    failed) and *errors*. A file already gone is not an error."""
    try:
        n_bytes = os.lstat(path).st_size
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as e:
        totals[2] += 1
        errors.add(type(e).__name__)
        return
    totals[0] += 1
    totals[1] += n_bytes


def _empty_folder(folder, totals, errors):
    """Unlink every file and symlink under *folder* (never following a
    symlink), adding to *totals* (files, bytes, failed) and *errors*."""
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        links = [n for n in dirnames
                 if os.path.islink(os.path.join(dirpath, n))]
        for name in filenames + links:
            _unlink(os.path.join(dirpath, name), totals, errors)


def speech_folder(entity, root=None) -> pathlib.Path:
    """The folder that holds a Node's, UserProfile's or ExternalItem's
    generated speech (tasks/tts.py): ``user/<user id>/node|profile|item/
    <id>/``, named by the row's user_id (a reply's author is the model
    account)."""
    from backend.models import Node, UserProfile, ExternalItem
    root = AUDIO_STORAGE_ROOT if root is None else root
    kinds = ((Node, "node"), (UserProfile, "profile"), (ExternalItem, "item"))
    for model, kind in kinds:
        if isinstance(entity, model):
            return storage_path(root, "user", entity.user_id, kind, entity.id)
    raise TypeError(f"speech_folder: unsupported entity {type(entity)!r}")


def media_rel_path(url):
    """The path under the storage root that a stored ``/media/...`` URL
    names, without its query (``?v=``) and as stored (URLs never carry
    the ``.enc`` of an encrypted file); None for anything else."""
    if not isinstance(url, str) or not url.startswith("/media/"):
        return None
    rel = url[len("/media/"):].split("?", 1)[0]
    return rel or None


def mark_speech_superseded(entity, urls=(), root=None) -> None:
    """Set the modification time of *entity*'s speech folder, and of
    every folder its old speech *urls* point into (profile versions
    share files), to now. The nightly sweep (backend/utils/audio_sweep.py)
    leaves a folder alone for a day after anything in it changed, so the
    speech this invalidated goes a day later at the earliest, and a
    playback of it started before is not cut. Best effort: a folder that
    does not exist is passed over, and an error is logged and never fails
    the edit."""
    root = AUDIO_STORAGE_ROOT if root is None else root
    real_root = os.path.realpath(root)
    try:
        folders = {str(speech_folder(entity, root))}
    except (ValueError, TypeError):
        folders = set()
    for url in urls:
        rel = media_rel_path(url)
        if rel is not None:
            folders.add(os.path.dirname(os.path.join(str(root), rel)))
    for folder in sorted(folders):
        if (os.path.islink(folder) or not os.path.isdir(folder)
                or not _inside(real_root, folder)):
            continue
        try:
            os.utime(folder)
        except OSError as e:
            logger.warning(
                "Could not mark the speech of %s %s superseded (%s)",
                type(entity).__name__, getattr(entity, "id", None),
                type(e).__name__)


def delete_node_audio(node, root=None) -> FolderDeletion:
    """Delete *node*'s audio folders (node_audio_dirs) and every file in
    them, encrypted or plain, recordings and generated speech alike."""
    return delete_folders(node_audio_dirs(node, root), root)


def clear_tts_artifacts(entity) -> bool:
    """Invalidate any generated TTS audio for a Node, UserProfile or
    ExternalItem (saved reference).

    Called when the entity's text content is edited so the user doesn't
    keep hearing audio that no longer matches the text (#66). Clears the
    scalar `audio_tts_url` AND the per-chunk `TTSChunk` rows used by the
    streaming player (clearing only the URL would leave chunked replay
    resurfacing the old audio), and resets the async-task tracking
    columns so the UI shows TTS as not-yet-generated.

    The files stay for now, so a playback that started before the edit
    plays on: this only marks the speech folder changed
    (mark_speech_superseded), and the nightly sweep
    (backend/utils/audio_sweep.py) deletes the speech files no row points
    at once the folder has been unchanged for a day. A regeneration
    overwrites the same file names, as before; the chunks it no longer
    makes go the same way.

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
    old_urls = [entity.audio_tts_url] + [
        u for (u,) in db.session.query(TTSChunk.audio_url).filter_by(**fk)]
    deleted = TTSChunk.query.filter_by(**fk).delete(synchronize_session=False)

    entity.audio_tts_url = None
    # These columns exist on both Node and UserProfile.
    entity.tts_task_id = None
    entity.tts_task_status = None
    entity.tts_task_progress = 0

    mark_speech_superseded(entity, old_urls)
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
