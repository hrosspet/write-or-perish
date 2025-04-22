"""Light‑weight blueprint to serve media files (audio) during development / tests.

This is *not* intended for production use.  In a real deployment static files
would be served directly by the web server or a storage provider/CDN.
"""

from flask import Blueprint, jsonify, send_file
import os
import pathlib

# Root storage folder mirrors the setting in nodes blueprint.
MEDIA_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()

media_bp = Blueprint("media_bp", __name__)


@media_bp.route("/<path:filename>")
def serve_media(filename):
    file_path = MEDIA_ROOT / filename
    if not file_path.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path)
