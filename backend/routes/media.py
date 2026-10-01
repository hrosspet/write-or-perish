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

A refused request gets the same 404 as a missing file, so the response
does not say whether a file exists. Files only signed-in viewers may fetch
are sent with ``Cache-Control: private`` so no shared cache stores them.

Encrypted files (``.enc``) are decrypted on the fly when GCP KMS
encryption is enabled. HTTP Range requests are supported for seeking.
"""

from flask import Blueprint, jsonify, send_file, Response, request, current_app
from flask_login import current_user
from werkzeug.security import safe_join
import os
import pathlib

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


def _cache_control_for(filename):
    """The Cache-Control value to serve *filename* with, or None when the
    current viewer may not fetch it."""
    parts = filename.split("/")
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


def _serve_bytes_with_range(data: bytes, mime_type: str, filename: str):
    """Serve binary data with HTTP Range request support for seeking."""
    total_length = len(data)

    range_header = request.headers.get('Range')
    if range_header:
        # Parse Range header: "bytes=start-end"
        try:
            range_spec = range_header.replace('bytes=', '')
            parts = range_spec.split('-')
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else total_length - 1
        except (ValueError, IndexError):
            start = 0
            end = total_length - 1

        # Clamp to valid range
        start = max(0, min(start, total_length - 1))
        end = max(start, min(end, total_length - 1))
        content_length = end - start + 1

        return Response(
            data[start:end + 1],
            status=206,
            mimetype=mime_type,
            headers={
                'Content-Range': f'bytes {start}-{end}/{total_length}',
                'Content-Length': content_length,
                'Accept-Ranges': 'bytes',
                'Content-Disposition': f'inline; filename="{filename}"',
            }
        )

    # No Range header — serve the full content
    return Response(
        data,
        mimetype=mime_type,
        headers={
            'Content-Length': total_length,
            'Accept-Ranges': 'bytes',
            'Content-Disposition': f'inline; filename="{filename}"',
        }
    )


@media_bp.route("/<path:filename>")
def serve_media(filename):
    # Refuse anything that would resolve outside MEDIA_ROOT.
    if safe_join(str(MEDIA_ROOT), filename) is None:
        return _not_found()

    cache_control = _cache_control_for(filename)
    if cache_control is None:
        return _not_found()

    file_path = MEDIA_ROOT / filename

    # Check if file exists (either plain or encrypted)
    encrypted_path = file_path.with_suffix(file_path.suffix + '.enc')

    if file_path.is_file():
        # Plain file exists, serve it directly
        response = send_file(file_path)

    elif encrypted_path.is_file():
        # Encrypted file exists, decrypt and serve
        from backend.utils.encryption import decrypt_file, is_encryption_enabled

        if not is_encryption_enabled():
            return jsonify({"error": "Encrypted file found but encryption is disabled"}), 500

        try:
            decrypted_content = decrypt_file(str(encrypted_path))
        except Exception as e:
            return jsonify({"error": f"Failed to decrypt file: {str(e)}"}), 500

        # Determine mime type from original extension
        ext = file_path.suffix.lower()
        mime_types = {
            '.mp3': 'audio/mpeg',
            '.webm': 'audio/webm',
            '.wav': 'audio/wav',
            '.m4a': 'audio/mp4',
            '.ogg': 'audio/ogg',
            '.flac': 'audio/flac',
        }
        mime_type = mime_types.get(ext, 'application/octet-stream')

        response = _serve_bytes_with_range(
            decrypted_content, mime_type, file_path.name
        )

    else:
        return _not_found()

    response.headers["Cache-Control"] = cache_control
    return response
