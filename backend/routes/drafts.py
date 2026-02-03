from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Draft, Node, NodeTranscriptChunk
from backend.extensions import db
from backend.utils.privacy import can_user_edit_node
from backend.utils.webm_utils import fix_last_chunk_duration, is_ffmpeg_available
import uuid
import pathlib
import os
import shutil
from backend.utils.encryption import encrypt_file

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

    draft = query.first()

    if not draft:
        return jsonify({"error": "No draft found"}), 404

    return jsonify({
        "id": draft.id,
        "content": draft.get_content(),
        "node_id": draft.node_id,
        "parent_id": draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": draft.updated_at.isoformat() + "Z"
    }), 200


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


# =============================================================================
# Streaming Transcription Endpoints (Draft-based)
# =============================================================================

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
    data = request.get_json() or {}

    parent_id = data.get("parent_id")
    privacy_level = data.get("privacy_level", "private")
    ai_usage = data.get("ai_usage", "none")

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

    # Encrypt the audio chunk at rest
    encrypted_path = encrypt_file(str(chunk_path))

    # Create transcript chunk record (linked to session, not node)
    existing_chunk = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        chunk_index=chunk_index
    ).first()

    if existing_chunk:
        # Update existing chunk if it failed before
        if existing_chunk.status == 'failed':
            existing_chunk.status = 'pending'
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
            status='pending'
        )
        db.session.add(transcript_chunk)
        db.session.commit()

    # Queue transcription task
    from backend.tasks.streaming_transcription import transcribe_draft_chunk

    task = transcribe_draft_chunk.delay(
        session_id=session_id,
        chunk_index=chunk_index,
        chunk_path=encrypted_path
    )

    # Update chunk with task ID
    transcript_chunk.task_id = task.id
    transcript_chunk.status = 'processing'
    db.session.commit()

    current_app.logger.info(
        f"Received audio chunk {chunk_index} for session {session_id}, "
        f"enqueued transcription task {task.id}"
    )

    return jsonify({
        "chunk_index": chunk_index,
        "task_id": task.id,
        "status": "processing"
    }), 202


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
        total_chunks=total_chunks
    )

    current_app.logger.info(
        f"Finalizing streaming session {session_id}, "
        f"total_chunks: {total_chunks}, task: {task.id}"
    )

    return jsonify({
        "message": "Finalization started",
        "task_id": task.id,
        "draft_id": draft.id,
        "total_chunks": total_chunks
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

    return jsonify({
        "session_id": session_id,
        "draft_id": draft.id,
        "streaming_status": draft.streaming_status,
        "total_chunks": draft.streaming_total_chunks,
        "completed_chunks": completed_count,
        "failed_chunks": failed_count,
        "chunks": chunk_statuses,
        "content": draft.get_content()
    })


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
        parent_id=draft.parent_id,
        node_type="user",
        privacy_level=draft.privacy_level or "private",
        ai_usage=draft.ai_usage or "none",
        transcription_status="completed",
        streaming_transcription=True  # Mark as having chunked audio
    )
    node.set_content(content)
    db.session.add(node)
    db.session.commit()

    # Move audio files from drafts folder to nodes folder
    draft_audio_dir = AUDIO_STORAGE_ROOT / f"drafts/{current_user.id}/{session_id}"
    node_audio_dir = AUDIO_STORAGE_ROOT / f"nodes/{current_user.id}/{node.id}"

    if draft_audio_dir.exists():
        # Fix the last chunk's duration metadata before moving (if not already fixed during finalization)
        if is_ffmpeg_available():
            success, message = fix_last_chunk_duration(str(draft_audio_dir))
            if success:
                current_app.logger.info(f"Fixed last chunk duration: {message}")
            else:
                current_app.logger.warning(f"Could not fix last chunk duration: {message}")

        try:
            # Create node audio directory
            node_audio_dir.mkdir(parents=True, exist_ok=True)

            # Move all files
            for file_path in draft_audio_dir.iterdir():
                shutil.move(str(file_path), str(node_audio_dir / file_path.name))

            # Remove the empty draft directory
            draft_audio_dir.rmdir()

            current_app.logger.info(
                f"Moved audio files from {draft_audio_dir} to {node_audio_dir}"
            )
        except Exception as e:
            current_app.logger.warning(f"Failed to move audio files: {e}")

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
