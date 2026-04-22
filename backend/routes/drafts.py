from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Draft, Node, NodeTranscriptChunk
from backend.extensions import db
from backend.utils.privacy import can_user_edit_node
import uuid
import pathlib
import os
import shutil
from backend.utils.encryption import encrypt_file
from backend.utils.webm_utils import persist_init_segment

drafts_bp = Blueprint("drafts_bp", __name__)

# Audio storage root - same as in nodes.py
AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()


@drafts_bp.route("/", methods=["GET"])
@login_required
def get_draft():
    """
    Get a draft for the current user.
    Query params:
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    Returns the draft if found, or 404 if no draft exists.
    Drafts are private - only the owner can access them.
    """
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)

    # Validate node_id if provided - user must own the node OR be LLM requester (parent node owner)
    if node_id:
        node = Node.query.get(node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404

        # Check authorization using shared utility function
        if not can_user_edit_node(node):
            return jsonify({"error": "Not authorized to access drafts for this node"}), 403

    # Build query for the user's draft matching the context
    query = Draft.query.filter_by(user_id=current_user.id)

    # Exclude drafts already processed by server-side LLM chain
    # (Reflect/Orient workflows create nodes automatically but leave
    # the draft alive for the SSE all_complete event)
    query = query.filter(Draft.llm_node_id.is_(None))

    if node_id:
        # Editing an existing node
        query = query.filter_by(node_id=node_id)
    else:
        # Creating a new node (possibly under a parent)
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)

    # Prefer the most recent draft (avoids stale empty drafts hiding
    # newer ones with actual content or stored audio chunks)
    draft = query.order_by(Draft.updated_at.desc()).first()

    if not draft:
        return jsonify({"error": "No draft found"}), 404

    response_data = {
        "id": draft.id,
        "content": draft.get_content(),
        "node_id": draft.node_id,
        "parent_id": draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": draft.updated_at.isoformat() + "Z"
    }

    # Include streaming session info so frontend can trigger recovery
    if draft.session_id:
        response_data["session_id"] = draft.session_id
        stored_count = NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id,
            status='stored'
        ).count()
        response_data["has_stored_chunks"] = stored_count > 0

    return jsonify(response_data), 200


@drafts_bp.route("/interrupted", methods=["GET"])
@login_required
def get_interrupted_drafts():
    """
    Find streaming drafts that were interrupted (e.g. page refresh mid-recording).

    Returns drafts where:
    - streaming_status is 'recording' (never finalized)
    - session_id is set
    - llm_node_id is NULL (not already processed)
    - Has at least one stored or completed chunk

    Returns the most recent interrupted draft regardless of parent context,
    so recovery works from any entry point (Reflect, Orient, Log resume).
    """
    query = Draft.query.filter(
        Draft.user_id == current_user.id,
        Draft.session_id.isnot(None),
        Draft.streaming_status == 'recording',
        Draft.llm_node_id.is_(None),
    )

    drafts = query.order_by(Draft.updated_at.desc()).all()

    results = []
    for draft in drafts:
        chunk_count = NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id,
        ).count()
        if chunk_count == 0:
            continue

        stored_count = NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id,
            status='stored',
        ).count()

        results.append({
            "id": draft.id,
            "session_id": draft.session_id,
            "parent_id": draft.parent_id,
            "label": draft.label,
            "content": draft.get_content(),
            "chunk_count": chunk_count,
            "has_stored_chunks": stored_count > 0,
            "created_at": draft.created_at.isoformat() + "Z",
            "updated_at": draft.updated_at.isoformat() + "Z",
        })

    return jsonify(results), 200


@drafts_bp.route("/", methods=["POST"])
@login_required
def save_draft():
    """
    Create or update a draft for the current user.
    Body:
      - content: The draft content (required)
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    If a draft already exists for this context, it will be updated.
    Drafts are private - only the owner can access them.
    """
    data = request.get_json() or {}
    content = data.get("content", "")
    node_id = data.get("node_id")
    parent_id = data.get("parent_id")

    # Validate node_id if provided - user must own the node OR be LLM requester (parent node owner)
    if node_id:
        node = Node.query.get(node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404

        # Check authorization using shared utility function
        if not can_user_edit_node(node):
            return jsonify({"error": "Not authorized to edit this node"}), 403

    # Validate parent_id if provided - parent must exist
    if parent_id:
        parent = Node.query.get(parent_id)
        if not parent:
            return jsonify({"error": "Parent node not found"}), 404

    # Find existing draft for this context
    query = Draft.query.filter_by(user_id=current_user.id)

    if node_id:
        query = query.filter_by(node_id=node_id)
    else:
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)

    draft = query.first()

    if draft:
        # Update existing draft
        draft.set_content(content)
    else:
        # Create new draft
        draft = Draft(
            user_id=current_user.id,
            node_id=node_id,
            parent_id=parent_id
        )
        draft.set_content(content)
        db.session.add(draft)

    db.session.commit()

    # Refresh to get the updated timestamp from database
    db.session.refresh(draft)

    return jsonify({
        "id": draft.id,
        "content": draft.get_content(),
        "node_id": draft.node_id,
        "parent_id": draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": draft.updated_at.isoformat() + "Z"
    }), 200


@drafts_bp.route("/", methods=["DELETE"])
@login_required
def delete_draft():
    """
    Delete a draft for the current user.
    Query params:
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    Called when user saves their work or explicitly discards the draft.
    Drafts are private - only the owner can delete them.
    """
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)

    # Validate node_id if provided - user must own the node OR be LLM requester (parent node owner)
    if node_id:
        node = Node.query.get(node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404

        # Check authorization using shared utility function
        if not can_user_edit_node(node):
            return jsonify({"error": "Not authorized to delete drafts for this node"}), 403

    # Build query for the user's draft matching the context
    query = Draft.query.filter_by(user_id=current_user.id)

    if node_id:
        query = query.filter_by(node_id=node_id)
    else:
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)

    draft = query.first()

    if not draft:
        return jsonify({"error": "No draft found"}), 404

    db.session.delete(draft)
    db.session.commit()

    return jsonify({"message": "Draft deleted"}), 200


@drafts_bp.route("/streaming/<session_id>/discard", methods=["DELETE"])
@login_required
def discard_streaming_draft(session_id):
    """
    Discard an interrupted streaming draft and its audio chunks.

    Deletes the draft, its NodeTranscriptChunk records, and audio files.
    """
    import shutil

    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id,
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # Delete chunk records
    NodeTranscriptChunk.query.filter_by(session_id=session_id).delete()

    # Delete audio files
    audio_dir = AUDIO_STORAGE_ROOT / f"drafts/{draft.user_id}/{session_id}"
    if audio_dir.exists():
        shutil.rmtree(audio_dir)

    db.session.delete(draft)
    db.session.commit()

    current_app.logger.info(
        f"Discarded interrupted draft {draft.id} (session {session_id})"
    )

    return jsonify({"message": "Draft discarded"}), 200


# =============================================================================
# Streaming Transcription Endpoints (Draft-based)
# =============================================================================

def _cleanup_stale_drafts(user_id):
    """Delete streaming drafts already processed by the server-side LLM chain.

    When Reflect/Orient workflows complete, the transcript is saved as a node
    and llm_node_id is set on the draft. The draft only lingers for the SSE
    all_complete event — after that it's safe to delete.
    """
    stale_drafts = Draft.query.filter(
        Draft.user_id == user_id,
        Draft.session_id.isnot(None),
        Draft.llm_node_id.isnot(None),
    ).all()

    deleted = 0
    for draft in stale_drafts:
        audio_dir = AUDIO_STORAGE_ROOT / f"drafts/{user_id}/{draft.session_id}"
        if audio_dir.exists():
            current_app.logger.warning(
                f"Draft {draft.id} (session {draft.session_id}) has "
                f"llm_node_id={draft.llm_node_id} but audio files were "
                f"not moved: {list(audio_dir.iterdir())}"
            )
            continue
        db.session.delete(draft)
        deleted += 1

    if deleted:
        db.session.commit()
        current_app.logger.info(
            f"Cleaned up {deleted} stale drafts for user {user_id}"
        )


@drafts_bp.route("/streaming/init", methods=["POST"])
@login_required
def init_streaming():
    """
    Initialize a streaming transcription session.

    Creates a Draft record to store the streaming session and transcript.
    NO node is created until the user explicitly saves.

    Request body:
    {
        "parent_id": 123,  // optional - parent node for the eventual node
        "privacy_level": "private",  // optional
        "ai_usage": "none",  // optional
    }

    Returns: { "session_id": "uuid", "draft_id": 456 }
    """
    _cleanup_stale_drafts(current_user.id)

    data = request.get_json() or {}

    parent_id = data.get("parent_id")
    privacy_level = data.get("privacy_level", "private")
    ai_usage = data.get("ai_usage", "none")
    label = data.get("label")  # 'Reflect', 'Orient', etc.

    # Generate session ID
    session_id = str(uuid.uuid4())

    # Check if a draft already exists for this parent_id context
    # If so, we'll use a new session but keep the draft context separate
    # For streaming, we always create a new draft with session_id
    draft = Draft(
        user_id=current_user.id,
        parent_id=parent_id,
        session_id=session_id,
        streaming_status="recording",
        streaming_total_chunks=None,
        streaming_completed_chunks=0,
        label=label,
        privacy_level=privacy_level,
        ai_usage=ai_usage
    )
    draft.set_content("")  # Will be populated as chunks are transcribed
    db.session.add(draft)
    db.session.commit()

    # Create directory for chunk storage
    chunk_dir = AUDIO_STORAGE_ROOT / f"drafts/{current_user.id}/{session_id}"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    current_app.logger.info(
        f"Initialized streaming session {session_id} for draft {draft.id} "
        f"(user: {current_user.id})"
    )

    return jsonify({
        "session_id": session_id,
        "draft_id": draft.id,
        "sse_url": f"/api/sse/drafts/{session_id}/transcription-stream"
    }), 201


@drafts_bp.route("/streaming/<session_id>/audio-chunk", methods=["POST"])
@login_required
def upload_streaming_chunk(session_id):
    """
    Upload an audio chunk for streaming transcription.

    Receives an audio chunk, stores it, and queues it for transcription.
    When transcription completes, the text is appended to the draft content.

    Expects multipart/form-data with:
    - chunk: audio file blob
    - chunk_index: integer (0-based)

    Returns: { "chunk_index": 0, "task_id": "celery-task-id" }
    """
    # Find the draft by session_id
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    if draft.streaming_status not in ["recording", "finalizing"]:
        return jsonify({"error": "Streaming session is not active"}), 400

    # Get form data
    if "chunk" not in request.files:
        return jsonify({"error": "Missing chunk file"}), 400

    chunk_file = request.files["chunk"]
    chunk_index = request.form.get("chunk_index")

    if chunk_index is None:
        return jsonify({"error": "Missing chunk_index"}), 400

    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return jsonify({"error": "Invalid chunk_index"}), 400

    # Save chunk to disk
    chunk_dir = AUDIO_STORAGE_ROOT / f"drafts/{current_user.id}/{session_id}"
    if not chunk_dir.exists():
        return jsonify({"error": "Streaming session directory not found"}), 404

    chunk_filename = f"chunk_{chunk_index:04d}.webm"
    chunk_path = chunk_dir / chunk_filename
    chunk_file.save(chunk_path)

    # Chunk 0 carries the EBML/Segment/Tracks init segment — the bytes that
    # every later batch needs as a prefix to remain a valid WebM (chunks 1+
    # are raw cluster fragments with no header). Extract and persist it now,
    # before encrypt_file() deletes the plaintext chunk.
    #
    # Reject the upload if extraction fails: batch 1 (chunks 0..19) would
    # still succeed because chunk 0 carries its own header, but any batch
    # starting at chunk 20+ would fail silently for want of an init segment.
    # Better to surface the problem on the first chunk than silently lose
    # later audio.
    if chunk_index == 0:
        # Narrow to the failure modes that actually signal "bad browser
        # output": ValueError from the EBML walker and OSError from the
        # file write. Anything else (e.g. KMS outage in encrypt_file) is
        # not the client's fault and should propagate as a 500 by the
        # usual Flask error path rather than getting misattributed to a
        # parse failure the user can't do anything about.
        try:
            persist_init_segment(chunk_path, chunk_dir)
        except (ValueError, OSError) as exc:
            current_app.logger.error(
                f"Failed to extract init segment from chunk 0 of "
                f"session {session_id}: {exc}"
            )
            try:
                chunk_path.unlink()
            except OSError:
                pass
            return jsonify({
                "error": "Could not parse WebM header from first chunk",
                "detail": str(exc),
                "code": "webm_header_parse_failed",
            }), 500

    # Encrypt the audio chunk at rest
    encrypted_path = encrypt_file(str(chunk_path))

    # Create transcript chunk record (linked to session, not node)
    # Chunks are stored on disk first; transcription is batched every 20 chunks
    # (20 × 15s = 5min) for better Whisper quality.
    existing_chunk = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        chunk_index=chunk_index
    ).first()

    if existing_chunk:
        # Update existing chunk if it failed before
        if existing_chunk.status == 'failed':
            existing_chunk.status = 'stored'
            existing_chunk.error = None
            db.session.commit()
            transcript_chunk = existing_chunk
        else:
            return jsonify({
                "message": "Chunk already uploaded",
                "chunk_index": chunk_index,
                "status": existing_chunk.status
            }), 200
    else:
        transcript_chunk = NodeTranscriptChunk(
            session_id=session_id,
            node_id=None,  # No node yet - this is draft-based
            chunk_index=chunk_index,
            status='stored'
        )
        db.session.add(transcript_chunk)
        db.session.commit()

    # Check if we have enough stored chunks for a batch (20 × 15s = 5min)
    BATCH_SIZE = 20
    stored_chunks = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        status='stored'
    ).order_by(NodeTranscriptChunk.chunk_index).all()

    task_id = None
    if len(stored_chunks) >= BATCH_SIZE:
        # Take the first BATCH_SIZE stored chunks and queue transcription
        batch = stored_chunks[:BATCH_SIZE]
        chunk_indices = [c.chunk_index for c in batch]

        from backend.tasks.streaming_transcription import transcribe_chunk_batch

        task = transcribe_chunk_batch.delay(
            session_id=session_id,
            chunk_indices=chunk_indices
        )
        task_id = task.id

        # Mark batch chunks as processing
        for c in batch:
            c.status = 'processing'
            c.task_id = task.id
        db.session.commit()

        current_app.logger.info(
            f"Queued batch transcription for session {session_id}, "
            f"chunks {chunk_indices}, task {task.id}"
        )

    # Count total chunks in DB for this session for debugging
    total_db_chunks = NodeTranscriptChunk.query.filter_by(
        session_id=session_id
    ).count()
    current_app.logger.info(
        f"Received audio chunk {chunk_index} for session {session_id}, "
        f"encrypted_path={encrypted_path}, "
        f"total_db_chunks={total_db_chunks}, "
        f"status=stored"
    )

    response = {
        "chunk_index": chunk_index,
        "status": "stored"
    }
    if task_id:
        response["task_id"] = task_id
        response["batch_queued"] = True
    return jsonify(response), 202


@drafts_bp.route("/streaming/<session_id>/finalize", methods=["POST"])
@login_required
def finalize_streaming(session_id):
    """
    Finalize streaming transcription.

    Called when the user stops recording. Marks the session as finalizing
    and waits for all chunks to complete transcription.

    Request body:
    {
        "total_chunks": 5  // Total number of chunks sent
    }

    Returns: { "message": "Finalization started", "draft_id": 123 }
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    if draft.streaming_status != "recording":
        return jsonify({"error": "Streaming session is not in recording state"}), 400

    data = request.get_json() or {}
    total_chunks = data.get("total_chunks")
    label = data.get("label")  # e.g. "Reflect", "Orient"
    parent_id = data.get("parent_id")  # thread parent for LLM chain
    model = data.get("model")  # LLM model for server-side generation
    if not model and label in ("Reflect", "Orient"):
        model = current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.5")

    if total_chunks is None:
        return jsonify({"error": "Missing total_chunks"}), 400

    # Update draft with total chunks and status
    draft.streaming_total_chunks = total_chunks
    draft.streaming_status = "finalizing"
    db.session.commit()

    # Queue finalization task
    from backend.tasks.streaming_transcription import finalize_draft_streaming

    task = finalize_draft_streaming.delay(
        session_id=session_id,
        total_chunks=total_chunks,
        label=label,
        user_id=current_user.id,
        parent_id=parent_id,
        model=model,
    )

    # Log chunk status at time of finalize request
    existing_chunks = NodeTranscriptChunk.query.filter_by(session_id=session_id).all()
    chunk_summary = [(c.chunk_index, c.status) for c in existing_chunks]
    current_app.logger.info(
        f"Finalizing streaming session {session_id}, "
        f"total_chunks: {total_chunks}, task: {task.id}, "
        f"existing_chunks_in_db: {chunk_summary}"
    )

    return jsonify({
        "message": "Finalization started",
        "task_id": task.id,
        "draft_id": draft.id,
        "total_chunks": total_chunks
    }), 202


@drafts_bp.route("/streaming/<session_id>/transcribe-remaining", methods=["POST"])
@login_required
def transcribe_remaining(session_id):
    """
    Trigger transcription of all remaining stored (untranscribed) chunks.

    Used for:
    - On-access recovery: user opens a draft that has stored but untranscribed chunks
    - Finalize flow: transcribe whatever remains regardless of batch size

    Returns: { "task_id": "...", "chunk_count": N }
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # Find all stored (untranscribed) chunks
    stored_chunks = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        status='stored'
    ).order_by(NodeTranscriptChunk.chunk_index).all()

    if not stored_chunks:
        return jsonify({
            "message": "No stored chunks to transcribe",
            "chunk_count": 0
        }), 200

    chunk_indices = [c.chunk_index for c in stored_chunks]

    from backend.tasks.streaming_transcription import transcribe_chunk_batch

    task = transcribe_chunk_batch.delay(
        session_id=session_id,
        chunk_indices=chunk_indices
    )

    # Mark as processing
    for c in stored_chunks:
        c.status = 'processing'
        c.task_id = task.id
    db.session.commit()

    current_app.logger.info(
        f"Triggered transcribe-remaining for session {session_id}, "
        f"chunks {chunk_indices}, task {task.id}"
    )

    return jsonify({
        "task_id": task.id,
        "chunk_count": len(chunk_indices),
        "chunk_indices": chunk_indices
    }), 202


@drafts_bp.route("/streaming/<session_id>/status", methods=["GET"])
@login_required
def get_streaming_status(session_id):
    """
    Get the current streaming transcription status.

    Returns status of all chunks and overall transcription progress.
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # Get all chunks
    chunks = NodeTranscriptChunk.query.filter_by(session_id=session_id).order_by(
        NodeTranscriptChunk.chunk_index
    ).all()

    chunk_statuses = [{
        "chunk_index": c.chunk_index,
        "status": c.status,
        "text": c.get_text() if c.status == 'completed' else None,
        "error": c.error if c.status == 'failed' else None
    } for c in chunks]

    completed_count = sum(1 for c in chunks if c.status == 'completed')
    failed_count = sum(1 for c in chunks if c.status == 'failed')
    pending_count = sum(1 for c in chunks if c.status in ('stored', 'processing', 'pending'))

    # Auto-complete interrupted recordings: if all chunks are done
    # (no pending/stored/processing) and the draft is still in 'recording'
    # state, the user refreshed mid-recording. Mark as completed so the
    # frontend recovery polling can finish.
    if (draft.streaming_status == 'recording'
            and chunks and pending_count == 0):
        draft.streaming_status = 'completed'
        draft.streaming_completed_chunks = completed_count
        db.session.commit()

    status_data = {
        "session_id": session_id,
        "draft_id": draft.id,
        "streaming_status": draft.streaming_status,
        "total_chunks": draft.streaming_total_chunks,
        "completed_chunks": completed_count,
        "failed_chunks": failed_count,
        "chunks": chunk_statuses,
        "content": draft.get_content(),
    }
    if draft.llm_node_id:
        status_data["llm_node_id"] = draft.llm_node_id
    return jsonify(status_data)


@drafts_bp.route("/streaming/<session_id>/save-as-node", methods=["POST"])
@login_required
def save_streaming_as_node(session_id):
    """
    Save the streaming draft as a node.

    Creates a new node from the draft content and moves audio files
    from the drafts folder to the nodes folder.

    Request body:
    {
        "content": "optional edited content"  // If not provided, uses draft.content
    }

    Returns: The created node data
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    if draft.streaming_status not in ["completed", "finalizing"]:
        return jsonify({"error": "Streaming session is not complete"}), 400

    data = request.get_json() or {}
    content = data.get("content", draft.get_content())

    # Create the node
    node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=draft.parent_id,
        node_type="user",
        privacy_level=draft.privacy_level or "private",
        ai_usage=draft.ai_usage or "none",
        transcription_status="completed",
        streaming_transcription=True  # Mark as having chunked audio
    )
    from backend.utils.tokens import approximate_token_count
    node.set_content(content)
    node.token_count = approximate_token_count(content)
    db.session.add(node)
    db.session.commit()

    # Move audio files from drafts folder to nodes folder
    draft_audio_dir = AUDIO_STORAGE_ROOT / f"drafts/{current_user.id}/{session_id}"
    node_audio_dir = AUDIO_STORAGE_ROOT / f"nodes/{current_user.id}/{node.id}"

    from backend.utils.audio_storage import move_draft_audio_to_node_dir
    move_draft_audio_to_node_dir(
        draft_audio_dir, node_audio_dir, current_app.logger,
    )

    # Update transcript chunks to reference the node
    NodeTranscriptChunk.query.filter_by(session_id=session_id).update({
        "node_id": node.id
    })

    # Delete the draft
    db.session.delete(draft)
    db.session.commit()

    current_app.logger.info(
        f"Saved streaming session {session_id} as node {node.id}"
    )

    return jsonify({
        "id": node.id,
        "content": node.get_content(),
        "parent_id": node.parent_id,
        "privacy_level": node.privacy_level,
        "ai_usage": node.ai_usage,
        "created_at": node.created_at.isoformat() + "Z"
    }), 201
