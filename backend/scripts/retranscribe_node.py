#!/usr/bin/env python3
"""
One-time script to manually trigger transcription for a stuck voice note.

Usage (from project root, with write-or-perish conda env active):
    python backend/scripts/retranscribe_node.py <node_id>

Example:
    python backend/scripts/retranscribe_node.py 59

The script will:
1. Look up the node in the database
2. Find the audio file (webm, mp3, etc.)
3. Reset the transcription status
4. Queue a new transcription task via Celery
"""
import sys
import os
import pathlib

# Add project root to path
project_root = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backend.app import create_app
from backend.models import Node
from backend.tasks.transcription import transcribe_audio

AUDIO_STORAGE_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()


def find_audio_file(user_id: int, node_id: int) -> pathlib.Path | None:
    """Find the audio file for a node, checking common extensions."""
    node_dir = AUDIO_STORAGE_ROOT / f"user/{user_id}/node/{node_id}"

    if not node_dir.exists():
        return None

    # Check for original.* files with common audio extensions
    extensions = ['.webm', '.mp3', '.m4a', '.wav', '.ogg', '.flac', '.aac']
    for ext in extensions:
        file_path = node_dir / f"original{ext}"
        if file_path.exists():
            return file_path

    # Fallback: find any file starting with "original"
    for file in node_dir.iterdir():
        if file.name.startswith("original"):
            return file

    return None


def main():
    if len(sys.argv) != 2:
        print("Usage: python backend/scripts/retranscribe_node.py <node_id>")
        sys.exit(1)

    try:
        node_id = int(sys.argv[1])
    except ValueError:
        print(f"Error: '{sys.argv[1]}' is not a valid node ID")
        sys.exit(1)

    app = create_app()

    with app.app_context():
        # Look up the node
        node = Node.query.get(node_id)
        if not node:
            print(f"Error: Node {node_id} not found")
            sys.exit(1)

        print(f"Found node {node_id}:")
        print(f"  User ID: {node.user_id}")
        print(f"  Current status: {node.transcription_status}")
        print(f"  Progress: {node.transcription_progress}%")
        print(f"  Audio URL: {node.audio_original_url}")

        # Find the audio file
        audio_file = find_audio_file(node.user_id, node_id)

        if not audio_file:
            print(f"\nError: Could not find audio file in {AUDIO_STORAGE_ROOT}/user/{node.user_id}/node/{node_id}/")
            sys.exit(1)

        print(f"\nFound audio file: {audio_file}")
        print(f"  Size: {audio_file.stat().st_size / 1024 / 1024:.2f} MB")

        # Reset transcription status
        node.transcription_status = 'pending'
        node.transcription_progress = 0
        node.transcription_error = None
        from backend.extensions import db
        db.session.commit()
        print("\nReset transcription status to 'pending'")

        # Queue the transcription task
        filename = audio_file.name  # e.g., "original.webm"
        task = transcribe_audio.delay(node_id, str(audio_file), filename)

        print(f"\nTranscription task queued!")
        print(f"  Task ID: {task.id}")
        print(f"\nMonitor progress with:")
        print(f"  curl http://localhost:5010/api/nodes/{node_id}/transcription-status")


if __name__ == "__main__":
    main()
