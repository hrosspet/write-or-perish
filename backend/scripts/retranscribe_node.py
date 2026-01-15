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
3. Convert webm to mp3 if needed (browser webm lacks duration metadata)
4. Reset the transcription status
5. Queue a new transcription task via Celery
"""
import sys
import os
import pathlib
import subprocess
from typing import Optional

# Add project root to path
project_root = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backend.app import create_app
from backend.models import Node
from backend.tasks.transcription import transcribe_audio

AUDIO_STORAGE_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()


def convert_webm_to_mp3(webm_path: pathlib.Path) -> Optional[pathlib.Path]:
    """Convert webm to mp3 using ffmpeg (streams, doesn't load all to memory)."""
    mp3_path = webm_path.with_suffix('.mp3')

    if mp3_path.exists():
        print(f"  MP3 already exists: {mp3_path}")
        return mp3_path

    print(f"  Converting webm to mp3 (browser webm lacks duration metadata)...")
    try:
        result = subprocess.run([
            'ffmpeg', '-i', str(webm_path),
            '-vn', '-acodec', 'libmp3lame', '-b:a', '128k',
            str(mp3_path)
        ], capture_output=True, text=True)

        if result.returncode != 0:
            print(f"  FFmpeg error: {result.stderr[:500]}")
            return None

        print(f"  Converted: {mp3_path.stat().st_size / 1024 / 1024:.2f} MB")
        return mp3_path
    except Exception as e:
        print(f"  Conversion failed: {e}")
        return None


def find_audio_file(user_id: int, node_id: int) -> Optional[pathlib.Path]:
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

        # Convert webm to mp3 if needed (browser webm lacks duration metadata)
        if audio_file.suffix.lower() == '.webm':
            mp3_file = convert_webm_to_mp3(audio_file)
            if mp3_file:
                audio_file = mp3_file
            else:
                print("\nWarning: Could not convert webm to mp3, trying with original")

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
