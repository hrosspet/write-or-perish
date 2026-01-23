"""
Flask shell script to recover full transcript from the last draft.

Usage:
    cd backend
    flask shell < recover_transcript.py

Or interactively:
    flask shell
    >>> exec(open('recover_transcript.py').read())
"""

from models import Draft, NodeTranscriptChunk
from app import db

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
    print(f"Status: {draft.streaming_status}")
    print(f"Total chunks: {draft.streaming_total_chunks}")
    print(f"Completed chunks: {draft.streaming_completed_chunks}")
    print(f"")
    print(f"=== Current Draft Content ({len(draft.content or '')} chars) ===")
    print(draft.content[:500] if draft.content else "(empty)")
    if draft.content and len(draft.content) > 500:
        print(f"... (truncated, {len(draft.content)} total chars)")
    print(f"")

    # Get all transcript chunks for this session
    chunks = NodeTranscriptChunk.query.filter_by(
        session_id=draft.session_id
    ).order_by(NodeTranscriptChunk.chunk_index).all()

    print(f"=== Transcript Chunks ({len(chunks)} found) ===")
    for chunk in chunks:
        status_icon = "✓" if chunk.status == "completed" else "✗" if chunk.status == "failed" else "⏳"
        text_preview = (chunk.text[:80] + "...") if chunk.text and len(chunk.text) > 80 else chunk.text
        print(f"  Chunk {chunk.chunk_index}: [{status_icon} {chunk.status}] {len(chunk.text or '')} chars")
        if chunk.error:
            print(f"    Error: {chunk.error}")

    # Assemble full transcript from completed chunks
    completed_chunks = [c for c in chunks if c.status == "completed" and c.text]
    completed_chunks.sort(key=lambda c: c.chunk_index)

    full_transcript = "\n\n".join(c.text for c in completed_chunks)

    print(f"")
    print(f"=== FULL RECOVERED TRANSCRIPT ({len(full_transcript)} chars) ===")
    print(full_transcript)
    print(f"")
    print(f"=== END OF TRANSCRIPT ===")

    # Optionally update the draft content
    print(f"")
    print(f"To update the draft with the recovered transcript, run:")
    print(f"  draft.content = full_transcript")
    print(f"  db.session.commit()")
