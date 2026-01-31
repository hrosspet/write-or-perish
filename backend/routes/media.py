"""Light‑weight blueprint to serve media files (audio) during development / tests.

This is *not* intended for production use.  In a real deployment static files
would be served directly by the web server or a storage provider/CDN.

Supports serving encrypted audio files (with .enc extension) by decrypting
them on-the-fly when GCP KMS encryption is enabled.
"""

from flask import Blueprint, jsonify, send_file, Response
import os
import pathlib
import io

# Root storage folder mirrors the setting in nodes blueprint.
MEDIA_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()

media_bp = Blueprint("media_bp", __name__)


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

            return Response(
                decrypted_content,
                mimetype=mime_type,
                headers={
                    'Content-Disposition': f'inline; filename="{file_path.name}"'
                }
            )
        except Exception as e:
            return jsonify({"error": f"Failed to decrypt file: {str(e)}"}), 500

    else:
        return jsonify({"error": "File not found"}), 404
