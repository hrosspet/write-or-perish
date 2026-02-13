"""
Server-Sent Events (SSE) endpoints for real-time streaming updates.

These endpoints provide one-way server-to-client communication for:
1. Streaming transcription updates (as chunks are transcribed)
2. Streaming TTS playback (as audio chunks are generated)
"""

from flask import Blueprint, Response, request, jsonify, current_app
from flask_login import login_required, current_user
from backend.models import Node, UserProfile, NodeTranscriptChunk, TTSChunk, Draft
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


@sse_bp.route("/nodes/<int:node_id>/transcription-stream")
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
                            "text": chunk.get_text(),
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
                        "content": current_node.get_content()
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


def _tts_stream_generator(app, entity_cls, entity_id, chunk_fk_attr,
                          entity_label, last_chunk):
    """
    Shared SSE generator for TTS chunk streaming.

    Polls the database for new TTSChunk records and yields SSE events.
    Works for both nodes and profiles since they share the same
    tts_task_status / audio_tts_url / tts_task_progress interface.

    Args:
        app: Flask application object (for app context in generator)
        entity_cls: SQLAlchemy model class (Node or UserProfile)
        entity_id: Primary key of the entity
        chunk_fk_attr: TTSChunk foreign key column name ('node_id' or 'profile_id')
        entity_label: Human-readable label for error messages
        last_chunk: Last chunk index the client has already received
    """
    last_sent_chunk = last_chunk
    heartbeat_interval = 15  # seconds
    last_heartbeat = time.time()
    max_idle_time = 600  # 10 minutes max connection time
    start_time = time.time()
    chunk_filter = {chunk_fk_attr: entity_id}

    while True:
        if time.time() - start_time > max_idle_time:
            yield format_sse_message({"message": "Connection timeout"}, event="close")
            break

        with app.app_context():
            entity = entity_cls.query.get(entity_id)
            if not entity:
                yield format_sse_message(
                    {"error": f"{entity_label} not found"}, event="error"
                )
                break

            # Get any new completed TTS chunks
            new_chunks = TTSChunk.query.filter(
                getattr(TTSChunk, chunk_fk_attr) == entity_id,
                TTSChunk.chunk_index > last_sent_chunk,
                TTSChunk.status == 'completed'
            ).order_by(TTSChunk.chunk_index).all()

            for chunk in new_chunks:
                chunk_data = {
                    "chunk_index": chunk.chunk_index,
                    "audio_url": chunk.audio_url,
                    "status": "ready"
                }
                if chunk.duration is not None:
                    chunk_data["duration"] = chunk.duration
                yield format_sse_message(chunk_data, event="chunk_ready")
                last_sent_chunk = chunk.chunk_index

            if entity.tts_task_status == 'completed':
                yield format_sse_message({
                    "message": "TTS generation complete",
                    "tts_url": entity.audio_tts_url
                }, event="all_complete")
                break

            if entity.tts_task_status == 'failed':
                yield format_sse_message({
                    "message": "TTS generation failed"
                }, event="error")
                break

            if time.time() - last_heartbeat > heartbeat_interval:
                total_chunks = TTSChunk.query.filter_by(**chunk_filter).count()
                completed_chunks = TTSChunk.query.filter_by(
                    status='completed', **chunk_filter
                ).count()

                yield format_sse_message({
                    "timestamp": time.time(),
                    "completed_chunks": completed_chunks,
                    "total_chunks": total_chunks,
                    "progress": entity.tts_task_progress
                }, event="heartbeat")
                last_heartbeat = time.time()

        time.sleep(1)


def _tts_stream_response(app, entity_cls, entity_id, chunk_fk_attr,
                          entity_label, last_chunk):
    """Wrap the TTS stream generator in an SSE Response."""
    return Response(
        _tts_stream_generator(
            app, entity_cls, entity_id, chunk_fk_attr,
            entity_label, last_chunk
        ),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@sse_bp.route("/nodes/<int:node_id>/tts-stream")
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
        from backend.utils.privacy import can_user_access_node
        if not can_user_access_node(node, current_user.id):
            return jsonify({"error": "Unauthorized"}), 403

    if node.tts_task_status not in ['pending', 'processing']:
        if node.audio_tts_url:
            return jsonify({
                "status": "completed",
                "tts_url": node.audio_tts_url
            }), 200
        return jsonify({"error": "TTS not in progress for this node"}), 400

    last_chunk = request.args.get('last_chunk', -1, type=int)
    app = current_app._get_current_object()
    return _tts_stream_response(app, Node, node_id, 'node_id', 'Node', last_chunk)


@sse_bp.route("/profiles/<int:profile_id>/tts-stream")
@login_required
def profile_tts_stream(profile_id):
    """
    SSE endpoint for real-time TTS audio chunk updates for profiles.

    Sends events as TTS audio chunks are generated:
    - event: chunk_ready - An audio chunk is ready to play
    - event: all_complete - All chunks have been generated
    - event: error - An error occurred
    - event: heartbeat - Keep-alive ping

    Query params:
    - last_chunk: Index of the last chunk the client has received
    """
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    if profile.tts_task_status not in ['pending', 'processing']:
        if profile.audio_tts_url:
            return jsonify({
                "status": "completed",
                "tts_url": profile.audio_tts_url
            }), 200
        return jsonify({"error": "TTS not in progress for this profile"}), 400

    last_chunk = request.args.get('last_chunk', -1, type=int)
    app = current_app._get_current_object()
    return _tts_stream_response(
        app, UserProfile, profile_id, 'profile_id', 'Profile', last_chunk
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
        # Safety net timeout - connection normally closes when streaming_status becomes
        # 'completed' or 'failed'. This just prevents orphaned connections.
        max_connection_time = 7200  # 2 hours
        start_time = time.time()

        while True:
            # Check for max connection time (safety net)
            if time.time() - start_time > max_connection_time:
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
                            "text": chunk.get_text(),
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
                current_content = current_draft.get_content()
                if current_content != last_content_version:
                    yield format_sse_message({
                        "content": current_content,
                        "completed_chunks": current_draft.streaming_completed_chunks or 0
                    }, event="content_update")
                    last_content_version = current_content

                # Check if streaming is complete
                if current_draft.streaming_status == 'completed':
                    yield format_sse_message({
                        "message": "Transcription complete",
                        "content": current_draft.get_content(),
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
