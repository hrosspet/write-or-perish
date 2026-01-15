"""
Server-Sent Events (SSE) endpoints for real-time streaming updates.

These endpoints provide one-way server-to-client communication for:
1. Streaming transcription updates (as chunks are transcribed)
2. Streaming TTS playback (as audio chunks are generated)
"""

from flask import Blueprint, Response, request, jsonify, current_app
from flask_login import login_required, current_user
from backend.models import Node, NodeTranscriptChunk, TTSChunk, Draft
from backend.extensions import db
import json
import time

sse_bp = Blueprint("sse_bp", __name__)


def format_sse_message(data, event=None):
    """Format data as an SSE message."""
    msg = ""
    if event:
        msg += f"event: {event}\n"
    msg += f"data: {json.dumps(data)}\n\n"
    return msg


@sse_bp.route("/<int:node_id>/transcription-stream")
@login_required
def transcription_stream(node_id):
    """
    SSE endpoint for real-time transcription updates.

    Sends events as transcript chunks complete:
    - event: chunk_complete - A chunk has been transcribed
    - event: all_complete - All chunks have been transcribed
    - event: error - An error occurred
    - event: heartbeat - Keep-alive ping

    Query params:
    - last_chunk: Index of the last chunk the client has received
    """
    node = Node.query.get_or_404(node_id)

    # Check ownership
    if node.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    # Check if streaming transcription is enabled for this node
    if not node.streaming_transcription:
        return jsonify({"error": "Streaming transcription not enabled for this node"}), 400

    # Get the last chunk index the client has seen
    last_chunk = request.args.get('last_chunk', -1, type=int)

    # Capture app reference for use in generator (needed for app context)
    app = current_app._get_current_object()

    def generate():
        last_sent_chunk = last_chunk
        heartbeat_interval = 15  # seconds
        last_heartbeat = time.time()
        max_idle_time = 600  # 10 minutes max connection time
        start_time = time.time()

        while True:
            # Check for timeout
            if time.time() - start_time > max_idle_time:
                yield format_sse_message({"message": "Connection timeout"}, event="close")
                break

            # Use app context for database operations (generator runs outside request context)
            with app.app_context():
                # Re-fetch node to get fresh data
                current_node = Node.query.get(node_id)
                if not current_node:
                    yield format_sse_message({"error": "Node not found"}, event="error")
                    break

                # Get any new completed chunks
                new_chunks = NodeTranscriptChunk.query.filter(
                    NodeTranscriptChunk.node_id == node_id,
                    NodeTranscriptChunk.chunk_index > last_sent_chunk,
                    NodeTranscriptChunk.status.in_(['completed', 'failed'])
                ).order_by(NodeTranscriptChunk.chunk_index).all()

                for chunk in new_chunks:
                    if chunk.status == 'completed':
                        yield format_sse_message({
                            "chunk_index": chunk.chunk_index,
                            "text": chunk.text,
                            "status": "completed"
                        }, event="chunk_complete")
                    else:
                        yield format_sse_message({
                            "chunk_index": chunk.chunk_index,
                            "error": chunk.error,
                            "status": "failed"
                        }, event="chunk_error")

                    last_sent_chunk = chunk.chunk_index

                # Check if transcription is complete (status is 'completed')
                if current_node.transcription_status == 'completed':
                    yield format_sse_message({
                        "message": "Transcription complete",
                        "content": current_node.content
                    }, event="all_complete")
                    break

                # Check if transcription failed
                if current_node.transcription_status == 'failed':
                    yield format_sse_message({
                        "message": "Transcription failed",
                        "error": current_node.transcription_error
                    }, event="error")
                    break

                # Send heartbeat to keep connection alive
                if time.time() - last_heartbeat > heartbeat_interval:
                    yield format_sse_message({
                        "timestamp": time.time(),
                        "completed_chunks": current_node.streaming_completed_chunks or 0,
                        "total_chunks": current_node.streaming_total_chunks
                    }, event="heartbeat")
                    last_heartbeat = time.time()

            # Sleep briefly before checking again (outside app context)
            time.sleep(1)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable nginx buffering
        }
    )


@sse_bp.route("/<int:node_id>/tts-stream")
@login_required
def tts_stream(node_id):
    """
    SSE endpoint for real-time TTS audio chunk updates.

    Sends events as TTS audio chunks are generated:
    - event: chunk_ready - An audio chunk is ready to play
    - event: all_complete - All chunks have been generated
    - event: error - An error occurred
    - event: heartbeat - Keep-alive ping

    Query params:
    - last_chunk: Index of the last chunk the client has received
    """
    node = Node.query.get_or_404(node_id)

    # Check ownership or voice mode access
    if node.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        # For non-owners, check if they can access via privacy settings
        from backend.utils.privacy import can_user_access_node
        if not can_user_access_node(node, current_user.id):
            return jsonify({"error": "Unauthorized"}), 403

    # Check if TTS is in progress
    if node.tts_task_status not in ['pending', 'processing']:
        # If already completed, return the final URL
        if node.audio_tts_url:
            return jsonify({
                "status": "completed",
                "tts_url": node.audio_tts_url
            }), 200
        return jsonify({"error": "TTS not in progress for this node"}), 400

    # Get the last chunk index the client has seen
    last_chunk = request.args.get('last_chunk', -1, type=int)

    # Capture app reference for use in generator (needed for app context)
    app = current_app._get_current_object()

    def generate():
        last_sent_chunk = last_chunk
        heartbeat_interval = 15  # seconds
        last_heartbeat = time.time()
        max_idle_time = 600  # 10 minutes max connection time
        start_time = time.time()

        while True:
            # Check for timeout
            if time.time() - start_time > max_idle_time:
                yield format_sse_message({"message": "Connection timeout"}, event="close")
                break

            # Use app context for database operations (generator runs outside request context)
            with app.app_context():
                # Re-fetch node to get fresh data
                current_node = Node.query.get(node_id)
                if not current_node:
                    yield format_sse_message({"error": "Node not found"}, event="error")
                    break

                # Get any new completed TTS chunks
                new_chunks = TTSChunk.query.filter(
                    TTSChunk.node_id == node_id,
                    TTSChunk.chunk_index > last_sent_chunk,
                    TTSChunk.status == 'completed'
                ).order_by(TTSChunk.chunk_index).all()

                for chunk in new_chunks:
                    yield format_sse_message({
                        "chunk_index": chunk.chunk_index,
                        "audio_url": chunk.audio_url,
                        "status": "ready"
                    }, event="chunk_ready")
                    last_sent_chunk = chunk.chunk_index

                # Check if TTS generation is complete
                if current_node.tts_task_status == 'completed':
                    yield format_sse_message({
                        "message": "TTS generation complete",
                        "tts_url": current_node.audio_tts_url
                    }, event="all_complete")
                    break

                # Check if TTS generation failed
                if current_node.tts_task_status == 'failed':
                    yield format_sse_message({
                        "message": "TTS generation failed"
                    }, event="error")
                    break

                # Send heartbeat to keep connection alive
                if time.time() - last_heartbeat > heartbeat_interval:
                    total_chunks = TTSChunk.query.filter_by(node_id=node_id).count()
                    completed_chunks = TTSChunk.query.filter_by(
                        node_id=node_id,
                        status='completed'
                    ).count()

                    yield format_sse_message({
                        "timestamp": time.time(),
                        "completed_chunks": completed_chunks,
                        "total_chunks": total_chunks,
                        "progress": current_node.tts_task_progress
                    }, event="heartbeat")
                    last_heartbeat = time.time()

            # Sleep briefly before checking again (outside app context)
            time.sleep(1)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable nginx buffering
        }
    )


@sse_bp.route("/drafts/<session_id>/transcription-stream")
@login_required
def draft_transcription_stream(session_id):
    """
    SSE endpoint for real-time transcription updates on drafts.

    This is the draft-based version of the transcription stream. It uses
    session_id instead of node_id and works with Draft instead of Node.

    Sends events as transcript chunks complete:
    - event: chunk_complete - A chunk has been transcribed
    - event: all_complete - All chunks have been transcribed
    - event: content_update - Draft content has been updated
    - event: error - An error occurred
    - event: heartbeat - Keep-alive ping

    Query params:
    - last_chunk: Index of the last chunk the client has received
    """
    draft = Draft.query.filter_by(session_id=session_id).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # Check ownership
    if draft.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    # Get the last chunk index the client has seen
    last_chunk = request.args.get('last_chunk', -1, type=int)

    # Capture app reference for use in generator (needed for app context)
    app = current_app._get_current_object()

    def generate():
        last_sent_chunk = last_chunk
        last_content_version = ""
        heartbeat_interval = 15  # seconds
        last_heartbeat = time.time()
        max_idle_time = 600  # 10 minutes max connection time
        start_time = time.time()

        while True:
            # Check for timeout
            if time.time() - start_time > max_idle_time:
                yield format_sse_message({"message": "Connection timeout"}, event="close")
                break

            # Use app context for database operations (generator runs outside request context)
            with app.app_context():
                # Re-fetch draft to get fresh data
                current_draft = Draft.query.filter_by(session_id=session_id).first()
                if not current_draft:
                    yield format_sse_message({"error": "Draft not found"}, event="error")
                    break

                # Get any new completed chunks
                new_chunks = NodeTranscriptChunk.query.filter(
                    NodeTranscriptChunk.session_id == session_id,
                    NodeTranscriptChunk.chunk_index > last_sent_chunk,
                    NodeTranscriptChunk.status.in_(['completed', 'failed'])
                ).order_by(NodeTranscriptChunk.chunk_index).all()

                for chunk in new_chunks:
                    if chunk.status == 'completed':
                        yield format_sse_message({
                            "chunk_index": chunk.chunk_index,
                            "text": chunk.text,
                            "status": "completed"
                        }, event="chunk_complete")
                    else:
                        yield format_sse_message({
                            "chunk_index": chunk.chunk_index,
                            "error": chunk.error,
                            "status": "failed"
                        }, event="chunk_error")

                    last_sent_chunk = chunk.chunk_index

                # Check if content has been updated
                if current_draft.content != last_content_version:
                    yield format_sse_message({
                        "content": current_draft.content,
                        "completed_chunks": current_draft.streaming_completed_chunks or 0
                    }, event="content_update")
                    last_content_version = current_draft.content

                # Check if streaming is complete
                if current_draft.streaming_status == 'completed':
                    yield format_sse_message({
                        "message": "Transcription complete",
                        "content": current_draft.content,
                        "draft_id": current_draft.id
                    }, event="all_complete")
                    break

                # Check if streaming failed
                if current_draft.streaming_status == 'failed':
                    yield format_sse_message({
                        "message": "Transcription failed"
                    }, event="error")
                    break

                # Send heartbeat to keep connection alive
                if time.time() - last_heartbeat > heartbeat_interval:
                    yield format_sse_message({
                        "timestamp": time.time(),
                        "completed_chunks": current_draft.streaming_completed_chunks or 0,
                        "total_chunks": current_draft.streaming_total_chunks,
                        "status": current_draft.streaming_status
                    }, event="heartbeat")
                    last_heartbeat = time.time()

            # Sleep briefly before checking again (outside app context)
            time.sleep(1)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable nginx buffering
        }
    )
