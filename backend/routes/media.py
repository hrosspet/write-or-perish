"""Light-weight blueprint to serve media files (audio) during development / tests.

This is *not* intended for production use.  In a real deployment static files
would be served directly by the web server or a storage provider/CDN.

Supports serving encrypted audio files (with .enc extension) by decrypting
them on-the-fly when GCP KMS encryption is enabled.
Supports HTTP Range requests for seeking in audio players.
"""

from flask import Blueprint, jsonify, send_file, Response, request
import os
import pathlib

# Root storage folder mirrors the setting in nodes blueprint.
MEDIA_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()

media_bp = Blueprint("media_bp", __name__)


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
    file_path = MEDIA_ROOT / filename

    # Check if file exists (either plain or encrypted)
    encrypted_path = file_path.with_suffix(file_path.suffix + '.enc')

    if file_path.is_file():
        # Plain file exists, serve it directly
        return send_file(file_path)

    elif encrypted_path.is_file():
        # Encrypted file exists, decrypt and serve
        from backend.utils.encryption import decrypt_file, is_encryption_enabled

        if not is_encryption_enabled():
            return jsonify({"error": "Encrypted file found but encryption is disabled"}), 500

        try:
            decrypted_content = decrypt_file(str(encrypted_path))

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

            return _serve_bytes_with_range(
                decrypted_content, mime_type, file_path.name
            )
        except Exception as e:
            return jsonify({"error": f"Failed to decrypt file: {str(e)}"}), 500

    else:
        return jsonify({"error": "File not found"}), 404
