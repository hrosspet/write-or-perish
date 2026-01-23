"""
Flask shell script to recover full transcript from the last draft.

Usage (on production server):
    cd /path/to/project-root
    flask shell
    >>> exec(open('backend/recover_transcript.py').read())
"""

import os
import pathlib
from backend.models import Draft, NodeTranscriptChunk
from backend.app import db

AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()

# Get the most recent draft
draft = Draft.query.order_by(Draft.created_at.desc()).first()

if not draft:
    print("No drafts found!")
else:
    print(f"=== Last Draft ===")
    print(f"ID: {draft.id}")
    print(f"Session ID: {draft.session_id}")
    print(f"User ID: {draft.user_id}")
    print(f"Created: {draft.created_at}")
    print(f"Updated: {draft.updated_at}")
    print(f"Status: {draft.streaming_status}")
    print(f"Total chunks (expected): {draft.streaming_total_chunks}")
    print(f"Completed chunks (db): {draft.streaming_completed_chunks}")
    print()

    # Check filesystem for audio chunks
    audio_dir = AUDIO_STORAGE_ROOT / "drafts" / str(draft.user_id) / str(draft.session_id)
    print(f"=== Filesystem: {audio_dir} ===")
    if audio_dir.exists():
        files = sorted(audio_dir.glob("chunk_*.webm"))
        print(f"Found {len(files)} audio file(s):")
        for f in files:
            size_kb = f.stat().st_size / 1024
            print(f"  {f.name}: {size_kb:.1f} KB")
    else:
        print(f"Directory does not exist!")
        # Check if there are any recent files in the drafts audio folder
        parent_dir = AUDIO_STORAGE_ROOT / "drafts" / str(draft.user_id)
        if parent_dir.exists():
            print(f"  Other sessions in {parent_dir}:")
            for d in parent_dir.iterdir():
                print(f"    {d.name}")
    print()

    # Get all transcript chunks for this session from DB
    chunks = NodeTranscriptChunk.query.filter_by(
        session_id=draft.session_id
    ).order_by(NodeTranscriptChunk.chunk_index).all()

    print(f"=== DB Transcript Chunks ({len(chunks)} found) ===")
    for chunk in chunks:
        status_icon = "✓" if chunk.status == "completed" else "✗" if chunk.status == "failed" else "⏳"
        print(f"  Chunk {chunk.chunk_index}: [{status_icon} {chunk.status}] {len(chunk.text or '')} chars")
        if chunk.error:
            print(f"    Error: {chunk.error}")
        if chunk.task_id:
            print(f"    Task ID: {chunk.task_id}")
    print()

    # Check for orphaned chunks (in DB but missing audio file)
    # and missing chunks (audio file exists but no DB record)
    if audio_dir.exists():
        fs_indices = set()
        for f in audio_dir.glob("chunk_*.webm"):
            idx = int(f.name.replace("chunk_", "").replace(".webm", ""))
            fs_indices.add(idx)
        db_indices = set(c.chunk_index for c in chunks)

        missing_in_db = fs_indices - db_indices
        missing_on_fs = db_indices - fs_indices

        if missing_in_db:
            print(f"=== Audio files WITHOUT DB records: {missing_in_db} ===")
            print("These chunks have audio but were never transcribed!")
            print("You can manually transcribe them.")
        if missing_on_fs:
            print(f"=== DB records WITHOUT audio files: {missing_on_fs} ===")
    print()

    # Show current draft content
    print(f"=== Current Draft Content ({len(draft.content or '')} chars) ===")
    print(draft.content[:500] if draft.content else "(empty)")
    if draft.content and len(draft.content) > 500:
        print(f"... (truncated)")
    print()

    # Assemble full transcript from completed chunks
    completed_chunks = [c for c in chunks if c.status == "completed" and c.text]
    completed_chunks.sort(key=lambda c: c.chunk_index)
    full_transcript = "\n\n".join(c.text for c in completed_chunks)

    print(f"=== RECOVERED TRANSCRIPT FROM DB ({len(full_transcript)} chars) ===")
    print(full_transcript)
    print()
    print("=== END ===")
    print()
    print("To manually transcribe a missing chunk, you can run:")
    print("  from tasks.streaming_transcription import transcribe_audio_chunk")
    print("  text = transcribe_audio_chunk('path/to/chunk_X.webm')")
    print()
    print("To update draft content:")
    print("  draft.content = full_transcript")
    print("  db.session.commit()")
