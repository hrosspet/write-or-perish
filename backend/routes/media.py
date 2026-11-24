"""Blueprint that serves stored media files (audio) at /media/<path>.

Every file belongs to an object, and a file is served only to a viewer who
may see that object:

- ``user/<uid>/node/<nid>/...`` and ``nodes/<uid>/<nid>/...`` (uploads,
  generated speech, recorded chunks): the node decides. A signed-in viewer
  needs the same access as GET /api/nodes/<nid>; a visitor who is not
  signed in gets the file only when the node is on the public site (the
  rule the public pages use).
- Every other file under ``user/<uid>/``, ``nodes/<uid>/``,
  ``drafts/<uid>/``, ``chunks/<uid>/`` or ``streaming/<uid>/`` (profiles,
  saved references, drafts, upload staging) is written under its owner's
  id and is served to that user only.
- Any other path is refused.

The requested path is resolved once (``..`` segments and symlinks
followed) and must land inside the media root; the access rule is decided
on that resolved path, and that same resolved file is the one opened, so
the file checked is always the file served. A path with an empty, ``.``
or ``..`` segment is refused before any of that.

A refused request gets the same 404 as a missing file, so the response
does not say whether a file exists. Files only signed-in viewers may fetch
are sent with ``Cache-Control: private`` so no shared cache stores them.

Encrypted files (``.enc``) are decrypted on the fly when GCP KMS
encryption is enabled, into an unnamed temporary file that is streamed
and closed with the response, at most MEDIA_DECRYPTS_PER_WORKER at once
in a worker. HTTP Range requests are supported for seeking.
"""

from flask import Blueprint, jsonify, send_file, request, current_app
from flask_login import current_user
from urllib.parse import unquote
from werkzeug.wsgi import FileWrapper
import os
import pathlib
import tempfile
import threading

# Root storage folder mirrors the setting in nodes blueprint.
MEDIA_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()

media_bp = Blueprint("media_bp", __name__)

# Top-level folders whose second path segment is the owning user's id.
OWNER_DIRS = ("user", "nodes", "drafts", "chunks", "streaming")

# Cache-Control for a file anyone may fetch, and for one only some signed-in
# users may fetch. Same one-day lifetime nginx used to set for all media;
# generated speech carries a ?v= query that changes when it is regenerated.
PUBLIC_CACHE_CONTROL = "public, max-age=86400"
PRIVATE_CACHE_CONTROL = "private, max-age=86400"

# Heuristics: how many encrypted files one web worker decrypts at once, and
# how long a request waits for its turn before it is answered 503. A
# decryption holds the whole file twice in memory for a moment (ciphertext
# and plaintext: about 400 MB for a 200 MB upload) and takes well under a
# second for the files the apps play; a player fetches one file at a time.
MEDIA_DECRYPTS_PER_WORKER = 2
MEDIA_DECRYPT_WAIT_SECONDS = 30

# threading's semaphore is gevent's under the gevent workers.
_decrypt_slots = threading.BoundedSemaphore(MEDIA_DECRYPTS_PER_WORKER)


def _not_found():
    return jsonify({"error": "File not found"}), 404


def _node_id_in(parts):
    """The node id a media path belongs to, or None when the path is not
    a node's folder."""
    if parts[0] == "user" and len(parts) >= 5 and parts[2] == "node":
        raw = parts[3]
    elif parts[0] == "nodes" and len(parts) >= 4:
        raw = parts[2]
    else:
        return None
    return int(raw) if raw.isdigit() else None


def _has_dot_segment(filename):
    """True when *filename*, as received or decoded once more, has an
    empty, ``.`` or ``..`` segment. No stored media path has one."""
    for path in (filename, unquote(filename)):
        if any(seg in ("", ".", "..") for seg in path.replace("\\", "/").split("/")):
            return True
    return False


def _resolve_in_media_root(path):
    """*path* with symlinks and ``..`` resolved, and that path relative to
    the media root (POSIX form); None when it is not inside the root."""
    root = MEDIA_ROOT.resolve()
    try:
        resolved = pathlib.Path(os.path.realpath(path))
        rel = resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    if not rel.parts:
        return None
    return resolved, rel.as_posix()


def _locate(filename):
    """The file a request for *filename* is served from, as
    ``(resolved path, path relative to the media root, encrypted)``, or
    None when there is no such file inside the media root.

    The plain file is preferred; otherwise its ``.enc`` sibling, which is
    resolved on its own so it too must stay inside the root."""
    if _has_dot_segment(filename):
        return None
    plain = _resolve_in_media_root(MEDIA_ROOT.resolve() / filename)
    if plain is None:
        return None
    if plain[0].is_file():
        return plain[0], plain[1], False
    encrypted = _resolve_in_media_root(
        plain[0].with_name(plain[0].name + ".enc"))
    if encrypted is not None and encrypted[0].is_file():
        return encrypted[0], encrypted[1], True
    return None


def _cache_control_for(rel_path):
    """The Cache-Control value to serve the file at *rel_path* (resolved,
    relative to the media root) with, or None when the current viewer may
    not fetch it."""
    parts = rel_path.split("/")
    if len(parts) < 3 or parts[0] not in OWNER_DIRS or not parts[1].isdigit():
        return None
    owner_id = int(parts[1])
    viewer_id = current_user.id if current_user.is_authenticated else None

    node_id = _node_id_in(parts)
    if node_id is not None:
        from backend.models import Node
        from backend.routes.commons import _publicly_visible
        from backend.utils.privacy import can_user_access_node
        node = Node.query.get(node_id)
        if node is not None:
            if (current_app.config.get("SHARE_V1", False)
                    and _publicly_visible(node)):
                return PUBLIC_CACHE_CONTROL
            if viewer_id is not None and can_user_access_node(node, viewer_id):
                return PRIVATE_CACHE_CONTROL

    # The owner's own folder (also covers a node the owner soft-deleted).
    if viewer_id is not None and viewer_id == owner_id:
        return PRIVATE_CACHE_CONTROL
    return None


def _decrypted_copy(file_path):
    """``(file, size)``: *file_path* decrypted into an unnamed temporary
    file (it has no name on disk, so nothing of it outlives the response
    or the process), positioned at its start. None when no decryption
    slot frees up within MEDIA_DECRYPT_WAIT_SECONDS. Raises when the file
    cannot be decrypted."""
    from backend.utils.encryption import decrypt_file
    if not _decrypt_slots.acquire(timeout=MEDIA_DECRYPT_WAIT_SECONDS):
        return None
    try:
        data = decrypt_file(str(file_path))
        copy = tempfile.TemporaryFile()
        try:
            copy.write(data)
            copy.flush()
            copy.seek(0)
        except BaseException:
            copy.close()
            raise
        return copy, len(data)
    finally:
        _decrypt_slots.release()


def _serve_open_file(copy, size, mime_type: str, filename: str):
    """Serve the open file *copy* of *size* bytes, streamed from disk,
    with HTTP Range support for seeking (206 with Content-Range). The
    server closes the file when the response ends."""
    try:
        response = current_app.response_class(
            FileWrapper(copy), mimetype=mime_type, direct_passthrough=True,
            headers={
                'Content-Disposition': f'inline; filename="{filename}"',
            })
        response.content_length = size
        return response.make_conditional(
            request.environ, accept_ranges=True, complete_length=size)
    except BaseException:
        copy.close()
        raise


@media_bp.route("/<path:filename>")
def serve_media(filename):
    located = _locate(filename)
    if located is None:
        return _not_found()
    file_path, rel_path, encrypted = located

    # Decide on the resolved path of the file that will be opened.
    cache_control = _cache_control_for(rel_path)
    if cache_control is None:
        return _not_found()

    if not encrypted:
        response = send_file(file_path)

    else:
        from backend.utils.encryption import reads_decrypt

        if not reads_decrypt():
            return jsonify({"error": "Encrypted file found but encryption is disabled"}), 500

        try:
            copy = _decrypted_copy(file_path)
        except Exception:
            # decrypt_file logs the failure (type only reaches Sentry)
            return jsonify({"error": "Failed to decrypt file"}), 500
        if copy is None:
            return jsonify({"error": "Busy, please try again"}), 503

        # Determine mime type from the extension before ".enc"
        plain_name = file_path.name[:-len(".enc")]
        ext = pathlib.PurePosixPath(plain_name).suffix.lower()
        mime_types = {
            '.mp3': 'audio/mpeg',
            '.webm': 'audio/webm',
            '.wav': 'audio/wav',
            '.m4a': 'audio/mp4',
            '.ogg': 'audio/ogg',
            '.flac': 'audio/flac',
        }
        mime_type = mime_types.get(ext, 'application/octet-stream')

        response = _serve_open_file(*copy, mime_type, plain_name)

    response.headers["Cache-Control"] = cache_control
    return response
