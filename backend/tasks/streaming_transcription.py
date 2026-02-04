"""
Celery tasks for streaming audio transcription.

These tasks handle real-time transcription of audio chunks as they're
recorded, enabling the user to see transcript text appear in their
draft while still recording.
"""
from celery import Task
from celery.utils.log import get_task_logger
from openai import OpenAI
import pathlib
import os
import shutil
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node, NodeTranscriptChunk, Draft
from backend.extensions import db
from backend.utils.audio_processing import compress_audio_if_needed
from backend.utils.webm_utils import fix_last_chunk_duration, is_ffmpeg_available
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import decrypt_file_to_temp

logger = get_task_logger(__name__)


class StreamingTranscriptionTask(Task):
    """Custom task class with error handling for chunk transcription."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        node_id = kwargs.get('node_id') or (args[0] if args else None)
        chunk_index = kwargs.get('chunk_index') or (args[1] if len(args) > 1 else None)

        if node_id is not None and chunk_index is not None:
            with flask_app.app_context():
                chunk = NodeTranscriptChunk.query.filter_by(
                    node_id=node_id,
                    chunk_index=chunk_index
                ).first()

                if chunk:
                    chunk.status = 'failed'
                    chunk.error = str(exc)[:500]
                    chunk.completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.error(f"Chunk transcription failed for node {node_id}, chunk {chunk_index}: {exc}")


@celery.task(base=StreamingTranscriptionTask, bind=True)
def transcribe_chunk(self, node_id: int, chunk_index: int, chunk_path: str):
    """
    Transcribe a single audio chunk.

    This task is designed to be fast and lightweight - it only transcribes
    a single 5-minute chunk, which should complete quickly without OOM issues.

    Args:
        node_id: Database ID of the node
        chunk_index: Zero-based index of the chunk
        chunk_path: Absolute path to the audio chunk file
    """
    logger.info(f"Starting chunk transcription for node {node_id}, chunk {chunk_index}")

    with flask_app.app_context():
        # Get the transcript chunk record
        chunk_record = NodeTranscriptChunk.query.filter_by(
            node_id=node_id,
            chunk_index=chunk_index
        ).first()

        if not chunk_record:
            raise ValueError(f"Chunk record not found for node {node_id}, chunk {chunk_index}")

        # Update status to processing
        chunk_record.status = 'processing'
        db.session.commit()

        try:
            # Get OpenAI API key (always use CHAT key for audio operations)
            api_key = get_openai_chat_key(flask_app.config)
            if not api_key:
                raise ValueError("OpenAI API key not configured (set OPENAI_API_KEY_CHAT or OPENAI_API_KEY)")

            client = OpenAI(api_key=api_key)
            file_path = pathlib.Path(chunk_path)

            # Check for encrypted version (.enc) if plain file doesn't exist
            temp_decrypted = None
            if not file_path.exists() and pathlib.Path(chunk_path + '.enc').exists():
                logger.info(f"Found encrypted chunk, decrypting: {chunk_path}.enc")
                temp_decrypted = decrypt_file_to_temp(chunk_path + '.enc')
                file_path = pathlib.Path(temp_decrypted)
            elif file_path.suffix == '.enc':
                # Path was passed with .enc extension directly
                logger.info(f"Decrypting encrypted chunk: {chunk_path}")
                temp_decrypted = decrypt_file_to_temp(chunk_path)
                file_path = pathlib.Path(temp_decrypted)
            elif not file_path.exists():
                raise FileNotFoundError(f"Audio chunk file not found: {chunk_path}")

            # Compress if needed (webm -> mp3 for better compatibility)
            processed_path = compress_audio_if_needed(file_path, logger)

            try:
                # Transcribe the chunk
                with open(processed_path, "rb") as audio_file:
                    resp = client.audio.transcriptions.create(
                        model="gpt-4o-transcribe",
                        file=audio_file,
                        response_format="text"
                    )

                    if hasattr(resp, "text"):
                        transcript = resp.text
                    elif isinstance(resp, dict):
                        transcript = resp.get("text") or resp.get("transcript") or ""
                    else:
                        transcript = str(resp)

            finally:
                # Clean up compressed file if different from original
                if processed_path != file_path:
                    try:
                        os.unlink(processed_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete compressed file: {e}")
                # Clean up temp decrypted file
                if temp_decrypted:
                    try:
                        os.unlink(temp_decrypted)
                    except Exception as e:
                        logger.warning(f"Failed to delete temp decrypted file: {e}")

            # Update chunk record with transcript
            chunk_record.set_text(transcript)
            chunk_record.status = 'completed'
            chunk_record.completed_at = datetime.utcnow()
            db.session.commit()

            # Update node's completed chunk count
            node = Node.query.get(node_id)
            if node:
                completed_count = NodeTranscriptChunk.query.filter_by(
                    node_id=node_id,
                    status='completed'
                ).count()
                node.streaming_completed_chunks = completed_count
                db.session.commit()

            logger.info(
                f"Chunk transcription successful for node {node_id}, chunk {chunk_index}: "
                f"{len(transcript)} characters"
            )

            return {
                'node_id': node_id,
                'chunk_index': chunk_index,
                'status': 'completed',
                'transcript_length': len(transcript)
            }

        except Exception as e:
            logger.error(f"Chunk transcription error for node {node_id}, chunk {chunk_index}: {e}", exc_info=True)
            chunk_record.status = 'failed'
            chunk_record.error = str(e)[:500]
            chunk_record.completed_at = datetime.utcnow()
            db.session.commit()
            raise


class FinalizationTask(Task):
    """Custom task class with error handling for finalization."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        node_id = kwargs.get('node_id') or (args[0] if args else None)

        if node_id:
            with flask_app.app_context():
                node = Node.query.get(node_id)
                if node:
                    node.transcription_status = 'failed'
                    node.transcription_error = str(exc)[:500]
                    node.transcription_completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.error(f"Streaming finalization failed for node {node_id}: {exc}")


@celery.task(base=FinalizationTask, bind=True)
def finalize_streaming(self, node_id: int, session_id: str, total_chunks: int):
    """
    Finalize streaming transcription by assembling all chunks.

    Waits for all chunks to complete, then assembles them into the final
    node content. Also handles cleanup of temporary chunk files.

    Args:
        node_id: Database ID of the node
        session_id: Streaming session ID
        total_chunks: Total number of chunks expected
    """
    logger.info(f"Finalizing streaming transcription for node {node_id}, {total_chunks} chunks")

    with flask_app.app_context():
        node = Node.query.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        # Wait for all chunks to complete (with timeout)
        import time
        max_wait_seconds = 600  # 10 minutes max wait
        poll_interval = 2  # seconds
        elapsed = 0

        while elapsed < max_wait_seconds:
            # Check chunk statuses
            chunks = NodeTranscriptChunk.query.filter_by(node_id=node_id).all()
            completed = [c for c in chunks if c.status == 'completed']
            failed = [c for c in chunks if c.status == 'failed']
            pending = [c for c in chunks if c.status in ['pending', 'processing']]

            logger.info(
                f"Node {node_id}: {len(completed)} completed, "
                f"{len(failed)} failed, {len(pending)} pending of {total_chunks}"
            )

            # All chunks processed (either completed or failed)
            if len(completed) + len(failed) >= total_chunks:
                break

            time.sleep(poll_interval)
            elapsed += poll_interval

            # Refresh the session to get updated data
            db.session.expire_all()

        # Get final chunk status
        chunks = NodeTranscriptChunk.query.filter_by(node_id=node_id).order_by(
            NodeTranscriptChunk.chunk_index
        ).all()

        completed_chunks = [c for c in chunks if c.status == 'completed']
        failed_chunks = [c for c in chunks if c.status == 'failed']

        # Assemble transcript from completed chunks
        transcripts = []
        for chunk in sorted(completed_chunks, key=lambda c: c.chunk_index):
            chunk_text = chunk.get_text()
            if chunk_text:
                transcripts.append(chunk_text)

        full_transcript = "\n\n".join(transcripts)

        # Format final content
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if failed_chunks:
            # Note any failed chunks in the content
            failed_indices = [c.chunk_index for c in failed_chunks]
            final_content = (
                f"# {timestamp} Voice note\n\n"
                f"*Note: Chunks {failed_indices} failed to transcribe*\n\n"
                f"{full_transcript}"
            )
        else:
            final_content = f"# {timestamp} Voice note\n\n{full_transcript}"

        # Update node
        node.set_content(final_content)
        node.transcription_status = 'completed'
        node.transcription_completed_at = datetime.utcnow()
        node.transcription_progress = 100
        db.session.commit()

        # Clean up streaming chunk files
        audio_storage_root = pathlib.Path(
            os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
        ).resolve()
        chunk_dir = audio_storage_root / f"streaming/{node.user_id}/{session_id}"

        if chunk_dir.exists():
            try:
                shutil.rmtree(chunk_dir)
                logger.info(f"Cleaned up streaming chunks for session {session_id}")
            except Exception as e:
                logger.warning(f"Failed to clean up chunk directory: {e}")

        logger.info(
            f"Streaming transcription finalized for node {node_id}: "
            f"{len(completed_chunks)} chunks, {len(full_transcript)} characters"
        )

        return {
            'node_id': node_id,
            'status': 'completed',
            'completed_chunks': len(completed_chunks),
            'failed_chunks': len(failed_chunks),
            'transcript_length': len(full_transcript)
        }


# =============================================================================
# Draft-based Streaming Transcription Tasks
# =============================================================================

class DraftStreamingTranscriptionTask(Task):
    """Custom task class with error handling for draft chunk transcription."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        session_id = kwargs.get('session_id') or (args[0] if args else None)
        chunk_index = kwargs.get('chunk_index') or (args[1] if len(args) > 1 else None)

        if session_id is not None and chunk_index is not None:
            with flask_app.app_context():
                chunk = NodeTranscriptChunk.query.filter_by(
                    session_id=session_id,
                    chunk_index=chunk_index
                ).first()

                if chunk:
                    chunk.status = 'failed'
                    chunk.error = str(exc)[:500]
                    chunk.completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.error(f"Draft chunk transcription failed for session {session_id}, chunk {chunk_index}: {exc}")


@celery.task(base=DraftStreamingTranscriptionTask, bind=True)
def transcribe_draft_chunk(self, session_id: str, chunk_index: int, chunk_path: str):
    """
    Transcribe a single audio chunk for draft-based streaming.

    This task transcribes a chunk and immediately appends the text to the
    Draft's content, enabling real-time transcript updates.

    Args:
        session_id: UUID of the streaming session
        chunk_index: Zero-based index of the chunk
        chunk_path: Absolute path to the audio chunk file
    """
    logger.info(f"Starting draft chunk transcription for session {session_id}, chunk {chunk_index}")

    with flask_app.app_context():
        # Get the transcript chunk record
        chunk_record = NodeTranscriptChunk.query.filter_by(
            session_id=session_id,
            chunk_index=chunk_index
        ).first()

        if not chunk_record:
            raise ValueError(f"Chunk record not found for session {session_id}, chunk {chunk_index}")

        # Update status to processing
        chunk_record.status = 'processing'
        db.session.commit()

        try:
            # Get OpenAI API key (always use CHAT key for audio operations)
            api_key = get_openai_chat_key(flask_app.config)
            if not api_key:
                raise ValueError("OpenAI API key not configured (set OPENAI_API_KEY_CHAT or OPENAI_API_KEY)")

            client = OpenAI(api_key=api_key)
            file_path = pathlib.Path(chunk_path)

            # Check for encrypted version (.enc) if plain file doesn't exist
            temp_decrypted = None
            if not file_path.exists() and pathlib.Path(chunk_path + '.enc').exists():
                logger.info(f"Found encrypted chunk, decrypting: {chunk_path}.enc")
                temp_decrypted = decrypt_file_to_temp(chunk_path + '.enc')
                file_path = pathlib.Path(temp_decrypted)
            elif file_path.suffix == '.enc':
                # Path was passed with .enc extension directly
                logger.info(f"Decrypting encrypted chunk: {chunk_path}")
                temp_decrypted = decrypt_file_to_temp(chunk_path)
                file_path = pathlib.Path(temp_decrypted)
            elif not file_path.exists():
                raise FileNotFoundError(f"Audio chunk file not found: {chunk_path}")

            # Compress if needed (webm -> mp3 for better compatibility)
            processed_path = compress_audio_if_needed(file_path, logger)

            try:
                # Transcribe the chunk
                with open(processed_path, "rb") as audio_file:
                    resp = client.audio.transcriptions.create(
                        model="gpt-4o-transcribe",
                        file=audio_file,
                        response_format="text"
                    )

                    if hasattr(resp, "text"):
                        transcript = resp.text
                    elif isinstance(resp, dict):
                        transcript = resp.get("text") or resp.get("transcript") or ""
                    else:
                        transcript = str(resp)

            finally:
                # Clean up compressed file if different from original
                if processed_path != file_path:
                    try:
                        os.unlink(processed_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete compressed file: {e}")
                # Clean up temp decrypted file
                if temp_decrypted:
                    try:
                        os.unlink(temp_decrypted)
                    except Exception as e:
                        logger.warning(f"Failed to delete temp decrypted file: {e}")

            # Update chunk record with transcript
            chunk_record.set_text(transcript)
            chunk_record.status = 'completed'
            chunk_record.completed_at = datetime.utcnow()
            db.session.commit()

            # Update draft content with the new chunk
            # We append to content in order, but need to handle out-of-order completion
            draft = Draft.query.filter_by(session_id=session_id).first()
            if draft:
                # Get all completed chunks in order
                completed_chunks = NodeTranscriptChunk.query.filter_by(
                    session_id=session_id,
                    status='completed'
                ).order_by(NodeTranscriptChunk.chunk_index).all()

                # Reassemble content from all completed chunks
                transcripts = [c.get_text() for c in completed_chunks if c.get_text()]
                draft.set_content("\n\n".join(transcripts))
                draft.streaming_completed_chunks = len(completed_chunks)
                db.session.commit()

            logger.info(
                f"Draft chunk transcription successful for session {session_id}, chunk {chunk_index}: "
                f"{len(transcript)} characters"
            )

            return {
                'session_id': session_id,
                'chunk_index': chunk_index,
                'status': 'completed',
                'transcript_length': len(transcript)
            }

        except Exception as e:
            logger.error(f"Draft chunk transcription error for session {session_id}, chunk {chunk_index}: {e}", exc_info=True)
            chunk_record.status = 'failed'
            chunk_record.error = str(e)[:500]
            chunk_record.completed_at = datetime.utcnow()
            db.session.commit()
            raise


class DraftFinalizationTask(Task):
    """Custom task class with error handling for draft finalization."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        session_id = kwargs.get('session_id') or (args[0] if args else None)

        if session_id:
            with flask_app.app_context():
                draft = Draft.query.filter_by(session_id=session_id).first()
                if draft:
                    draft.streaming_status = 'failed'
                    db.session.commit()
                    logger.error(f"Draft streaming finalization failed for session {session_id}: {exc}")


@celery.task(base=DraftFinalizationTask, bind=True)
def finalize_draft_streaming(self, session_id: str, total_chunks: int):
    """
    Finalize streaming transcription for a draft.

    Waits for all chunks to complete, then marks the draft as completed.
    The transcript text is already assembled incrementally in Draft.content
    as each chunk completes.

    Args:
        session_id: UUID of the streaming session
        total_chunks: Total number of chunks expected
    """
    logger.info(f"Finalizing draft streaming for session {session_id}, {total_chunks} chunks")

    with flask_app.app_context():
        draft = Draft.query.filter_by(session_id=session_id).first()
        if not draft:
            raise ValueError(f"Draft not found for session {session_id}")

        # Wait for all chunks to complete (with timeout)
        import time
        max_wait_seconds = 600  # 10 minutes max wait
        poll_interval = 2  # seconds
        elapsed = 0

        while elapsed < max_wait_seconds:
            # Check chunk statuses
            chunks = NodeTranscriptChunk.query.filter_by(session_id=session_id).all()
            completed = [c for c in chunks if c.status == 'completed']
            failed = [c for c in chunks if c.status == 'failed']
            pending = [c for c in chunks if c.status in ['pending', 'processing']]

            logger.info(
                f"Session {session_id}: {len(completed)} completed, "
                f"{len(failed)} failed, {len(pending)} pending of {total_chunks}"
            )

            # All chunks processed (either completed or failed)
            if len(completed) + len(failed) >= total_chunks:
                break

            time.sleep(poll_interval)
            elapsed += poll_interval

            # Refresh the session to get updated data
            db.session.expire_all()

        # Log whether we exited by completion or timeout
        if elapsed >= max_wait_seconds:
            logger.warning(
                f"Session {session_id}: TIMED OUT after {elapsed}s waiting for chunks. "
                f"Expected {total_chunks}, got {len(completed)} completed + {len(failed)} failed"
            )
        else:
            logger.info(
                f"Session {session_id}: All chunks processed in {elapsed}s"
            )

        # Get final chunk status
        chunks = NodeTranscriptChunk.query.filter_by(session_id=session_id).order_by(
            NodeTranscriptChunk.chunk_index
        ).all()

        completed_chunks = [c for c in chunks if c.status == 'completed']
        failed_chunks = [c for c in chunks if c.status == 'failed']

        # Final content assembly from completed chunks
        transcripts = []
        for chunk in sorted(completed_chunks, key=lambda c: c.chunk_index):
            chunk_text = chunk.get_text()
            if chunk_text:
                transcripts.append(chunk_text)

        full_transcript = "\n\n".join(transcripts)

        # Format final content
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if failed_chunks:
            # Note any failed chunks in the content
            failed_indices = [c.chunk_index for c in failed_chunks]
            final_content = (
                f"# {timestamp} Voice note\n\n"
                f"*Note: Chunks {failed_indices} failed to transcribe*\n\n"
                f"{full_transcript}"
            )
        else:
            final_content = f"# {timestamp} Voice note\n\n{full_transcript}"

        # Fix the duration metadata of the last chunk (WebM files from MediaRecorder
        # often lack proper duration, which causes playback issues)
        if is_ffmpeg_available():
            audio_storage_root = pathlib.Path(
                os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
            ).resolve()
            chunk_dir = audio_storage_root / f"drafts/{draft.user_id}/{session_id}"
            if chunk_dir.exists():
                success, message = fix_last_chunk_duration(str(chunk_dir))
                if success:
                    logger.info(f"Fixed last chunk duration for session {session_id}: {message}")
                else:
                    logger.warning(f"Could not fix last chunk duration for session {session_id}: {message}")
        else:
            logger.warning("ffmpeg not available, skipping last chunk duration fix")

        # Refresh draft and update
        db.session.refresh(draft)
        draft.set_content(final_content)
        draft.streaming_status = 'completed'
        draft.streaming_completed_chunks = len(completed_chunks)
        db.session.commit()

        logger.info(
            f"Draft streaming finalized for session {session_id}: "
            f"{len(completed_chunks)} chunks, {len(full_transcript)} characters"
        )

        return {
            'session_id': session_id,
            'status': 'completed',
            'completed_chunks': len(completed_chunks),
            'failed_chunks': len(failed_chunks),
            'transcript_length': len(full_transcript)
        }
