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
from backend.models import Node, NodeTranscriptChunk, Draft, APICostLog
from backend.extensions import db
from backend.utils.audio_processing import compress_audio_if_needed, get_audio_duration
from backend.utils.webm_utils import concat_webm_fragments
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import decrypt_file_to_temp
from backend.utils.cost import calculate_audio_cost_microdollars

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

            # Measure audio duration for cost tracking
            chunk_duration_sec = get_audio_duration(processed_path, logger)

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

            # Log transcription cost
            node = Node.query.get(node_id)
            if node and chunk_duration_sec > 0:
                transcription_cost = calculate_audio_cost_microdollars(
                    "gpt-4o-transcribe", chunk_duration_sec
                )
                cost_log = APICostLog(
                    user_id=node.user_id,
                    model_id="gpt-4o-transcribe",
                    request_type="transcription",
                    audio_duration_seconds=chunk_duration_sec,
                    cost_microdollars=transcription_cost,
                )
                db.session.add(cost_log)
            db.session.commit()

            # Update node's completed chunk count
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

        # Format final content - only add title for top-level nodes
        if node.parent_id is None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if failed_chunks:
                failed_indices = [c.chunk_index for c in failed_chunks]
                final_content = (
                    f"# {timestamp} Voice note\n\n"
                    f"*Note: Chunks {failed_indices} failed to transcribe*\n\n"
                    f"{full_transcript}"
                )
            else:
                final_content = f"# {timestamp} Voice note\n\n{full_transcript}"
        else:
            if failed_chunks:
                failed_indices = [c.chunk_index for c in failed_chunks]
                final_content = (
                    f"*Note: Chunks {failed_indices} failed to transcribe*\n\n"
                    f"{full_transcript}"
                )
            else:
                final_content = full_transcript

        # Update node
        from backend.utils.tokens import approximate_token_count
        node.set_content(final_content)
        node.token_count = approximate_token_count(final_content)
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

            # Measure audio duration for cost tracking
            draft_chunk_duration_sec = get_audio_duration(processed_path, logger)

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

            # Log transcription cost
            draft = Draft.query.filter_by(session_id=session_id).first()
            if draft and draft_chunk_duration_sec > 0:
                transcription_cost = calculate_audio_cost_microdollars(
                    "gpt-4o-transcribe", draft_chunk_duration_sec
                )
                cost_log = APICostLog(
                    user_id=draft.user_id,
                    model_id="gpt-4o-transcribe",
                    request_type="transcription",
                    audio_duration_seconds=draft_chunk_duration_sec,
                    cost_microdollars=transcription_cost,
                )
                db.session.add(cost_log)
            db.session.commit()

            # Update draft content with the new chunk
            # We append to content in order, but need to handle out-of-order completion
            if not draft:
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


class BatchTranscriptionTask(Task):
    """Custom task class with error handling for batch chunk transcription."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        session_id = kwargs.get('session_id') or (args[0] if args else None)
        chunk_indices = kwargs.get('chunk_indices') or (args[1] if len(args) > 1 else None)

        if session_id and chunk_indices:
            with flask_app.app_context():
                NodeTranscriptChunk.query.filter(
                    NodeTranscriptChunk.session_id == session_id,
                    NodeTranscriptChunk.chunk_index.in_(chunk_indices)
                ).update({
                    'status': 'failed',
                    'error': str(exc)[:500],
                    'completed_at': datetime.utcnow()
                }, synchronize_session='fetch')
                db.session.commit()
                logger.error(
                    f"Batch transcription failed for session {session_id}, "
                    f"chunks {chunk_indices}: {exc}"
                )


@celery.task(base=BatchTranscriptionTask, bind=True)
def transcribe_chunk_batch(self, session_id: str, chunk_indices: list):
    """
    Transcribe a batch of audio chunks by merging them and sending to Whisper.

    Instead of transcribing each 15s chunk individually, this task:
    1. Decrypts all chunk files in the batch
    2. Merges MediaRecorder fragments into one valid WebM
    3. Sends merged audio to Whisper (gpt-4o-transcribe)
    4. Stores transcript, marks all chunks as completed
    5. Reassembles draft.content from all completed chunks
    6. Encrypts the merged audio and deletes individual chunk files
    """
    logger.info(
        f"Starting batch transcription for session {session_id}, "
        f"chunks {chunk_indices}"
    )

    with flask_app.app_context():
        audio_storage_root = pathlib.Path(
            os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
        ).resolve()

        draft = Draft.query.filter_by(session_id=session_id).first()
        if not draft:
            raise ValueError(f"Draft not found for session {session_id}")

        chunk_dir = audio_storage_root / f"drafts/{draft.user_id}/{session_id}"

        # Collect and decrypt chunk files
        temp_files = []
        decrypted_paths = []

        try:
            for idx in sorted(chunk_indices):
                chunk_filename = f"chunk_{idx:04d}.webm"
                chunk_path = chunk_dir / chunk_filename
                enc_path = chunk_dir / f"{chunk_filename}.enc"

                if enc_path.exists():
                    temp_path = decrypt_file_to_temp(str(enc_path))
                    temp_files.append(temp_path)
                    decrypted_paths.append(temp_path)
                elif chunk_path.exists():
                    decrypted_paths.append(str(chunk_path))
                else:
                    logger.warning(
                        f"Chunk file not found for session {session_id}, "
                        f"index {idx}: {chunk_path}"
                    )

            if not decrypted_paths:
                raise FileNotFoundError(
                    f"No chunk files found for session {session_id}, "
                    f"indices {chunk_indices}"
                )

            # Merge MediaRecorder fragments into a single valid WebM.
            # Binary-append the fragments and remux — see
            # concat_webm_fragments for the full rationale.
            merged_path = concat_webm_fragments(decrypted_paths)
            merge_input = merged_path

            # Compress if needed (webm -> mp3)
            merge_input_path = pathlib.Path(merge_input)
            processed_path = compress_audio_if_needed(merge_input_path, logger)

            # Measure duration for cost tracking
            batch_duration_sec = get_audio_duration(processed_path, logger)

            try:
                # Transcribe via Whisper
                api_key = get_openai_chat_key(flask_app.config)
                if not api_key:
                    raise ValueError(
                        "OpenAI API key not configured"
                    )

                client = OpenAI(api_key=api_key)
                with open(processed_path, "rb") as audio_file:
                    resp = client.audio.transcriptions.create(
                        model="gpt-4o-transcribe",
                        file=audio_file,
                        response_format="text"
                    )

                    if hasattr(resp, "text"):
                        transcript = resp.text
                    elif isinstance(resp, dict):
                        transcript = (
                            resp.get("text") or resp.get("transcript") or ""
                        )
                    else:
                        transcript = str(resp)

            finally:
                if processed_path != merge_input_path:
                    try:
                        os.unlink(processed_path)
                    except Exception:
                        pass

            # Store transcript in the first chunk of the batch;
            # mark all batch chunks as completed
            first_idx = min(chunk_indices)
            for idx in chunk_indices:
                chunk_record = NodeTranscriptChunk.query.filter_by(
                    session_id=session_id,
                    chunk_index=idx
                ).first()
                if chunk_record:
                    if idx == first_idx:
                        chunk_record.set_text(transcript)
                    else:
                        # Other chunks in batch don't carry text —
                        # their audio is merged into the first chunk
                        chunk_record.set_text("")
                    chunk_record.status = 'completed'
                    chunk_record.completed_at = datetime.utcnow()

            # Log transcription cost
            if draft and batch_duration_sec > 0:
                transcription_cost = calculate_audio_cost_microdollars(
                    "gpt-4o-transcribe", batch_duration_sec
                )
                cost_log = APICostLog(
                    user_id=draft.user_id,
                    model_id="gpt-4o-transcribe",
                    request_type="transcription",
                    audio_duration_seconds=batch_duration_sec,
                    cost_microdollars=transcription_cost,
                )
                db.session.add(cost_log)

            # Encrypt the merged audio and store as a batch file;
            # delete individual chunk .enc files.
            # IMPORTANT: do this BEFORE committing chunk status to DB,
            # because finalize_draft_streaming polls for completed chunks
            # and may move/delete the directory once all are done.
            if merged_path and os.path.exists(merged_path):
                batch_filename = (
                    f"batch_{min(chunk_indices):04d}-"
                    f"{max(chunk_indices):04d}.webm"
                )
                batch_dest = chunk_dir / batch_filename
                shutil.move(merged_path, str(batch_dest))
                from backend.utils.encryption import encrypt_file
                encrypt_file(str(batch_dest))

                # Delete individual encrypted chunk files in the batch
                for idx in chunk_indices:
                    for ext in [
                        f"chunk_{idx:04d}.webm.enc",
                        f"chunk_{idx:04d}.webm"
                    ]:
                        p = chunk_dir / ext
                        if p.exists():
                            try:
                                p.unlink()
                            except Exception as e:
                                logger.warning(
                                    f"Failed to delete {p}: {e}"
                                )

            db.session.commit()

            # Reassemble draft.content from all completed chunks
            completed_chunks = NodeTranscriptChunk.query.filter_by(
                session_id=session_id,
                status='completed'
            ).order_by(NodeTranscriptChunk.chunk_index).all()

            transcripts = [
                c.get_text() for c in completed_chunks if c.get_text()
            ]
            draft.set_content("\n\n".join(transcripts))
            draft.streaming_completed_chunks = len(completed_chunks)
            db.session.commit()

            logger.info(
                f"Batch transcription successful for session {session_id}, "
                f"chunks {chunk_indices}: {len(transcript)} characters"
            )

            return {
                'session_id': session_id,
                'chunk_indices': chunk_indices,
                'status': 'completed',
                'transcript_length': len(transcript)
            }

        except Exception as e:
            logger.error(
                f"Batch transcription error for session {session_id}, "
                f"chunks {chunk_indices}: {e}",
                exc_info=True
            )
            # Mark all chunks in batch as failed
            for idx in chunk_indices:
                chunk_record = NodeTranscriptChunk.query.filter_by(
                    session_id=session_id,
                    chunk_index=idx
                ).first()
                if chunk_record and chunk_record.status != 'completed':
                    chunk_record.status = 'failed'
                    chunk_record.error = str(e)[:500]
                    chunk_record.completed_at = datetime.utcnow()
            db.session.commit()
            raise

        finally:
            # Clean up temp decrypted files
            for tf in temp_files:
                try:
                    os.unlink(tf)
                except Exception:
                    pass
            # Clean up merged file if still around
            if merged_path and os.path.exists(merged_path):
                try:
                    os.unlink(merged_path)
                except Exception:
                    pass


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
def finalize_draft_streaming(self, session_id: str, total_chunks: int,
                             label: str = None, user_id: int = None,
                             parent_id: int = None, model: str = None):
    """
    Finalize streaming transcription for a draft.

    Waits for all chunks to complete, then marks the draft as completed.
    The transcript text is already assembled incrementally in Draft.content
    as each chunk completes.

    If user_id, parent_id, and model are provided and the label is a
    workflow type (Voice), server-side LLM + TTS generation is
    kicked off so the response is ready when the user returns to the app.

    Args:
        session_id: UUID of the streaming session
        total_chunks: Total number of chunks expected
        label: Optional title label (e.g. "Voice").
               Defaults to "Voice note" when None.
        user_id: ID of the user (for server-side LLM chain)
        parent_id: Thread parent node ID (for server-side LLM chain)
        model: LLM model ID (for server-side LLM chain)
    """
    logger.info(f"Finalizing draft streaming for session {session_id}, {total_chunks} chunks")

    with flask_app.app_context():
        draft = Draft.query.filter_by(session_id=session_id).first()
        if not draft:
            raise ValueError(f"Draft not found for session {session_id}")

        # Trigger transcription of any remaining stored chunks (< 20, so
        # they didn't hit the batch threshold during recording). This is a
        # synchronous call to the batch task so we can wait for completion.
        stored_chunks = NodeTranscriptChunk.query.filter_by(
            session_id=session_id,
            status='stored'
        ).order_by(NodeTranscriptChunk.chunk_index).all()

        if stored_chunks:
            remaining_indices = [c.chunk_index for c in stored_chunks]
            logger.info(
                f"Session {session_id}: triggering transcription of "
                f"{len(remaining_indices)} remaining stored chunks: "
                f"{remaining_indices}"
            )
            # Queue async and let the polling loop below wait for completion
            batch_task = transcribe_chunk_batch.delay(
                session_id=session_id,
                chunk_indices=remaining_indices,
            )
            for c in stored_chunks:
                c.status = 'processing'
                c.task_id = batch_task.id
            db.session.commit()

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
            pending = [c for c in chunks if c.status in ['pending', 'processing', 'stored']]

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

        # Format final content - only add title for top-level drafts
        title_label = label or "Voice note"
        if draft.parent_id is None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if failed_chunks:
                failed_indices = [c.chunk_index for c in failed_chunks]
                final_content = (
                    f"# {timestamp} {title_label}\n\n"
                    f"*Note: Chunks {failed_indices} failed to transcribe*\n\n"
                    f"{full_transcript}"
                )
            else:
                final_content = f"# {timestamp} {title_label}\n\n{full_transcript}"
        else:
            if failed_chunks:
                failed_indices = [c.chunk_index for c in failed_chunks]
                final_content = (
                    f"*Note: Chunks {failed_indices} failed to transcribe*\n\n"
                    f"{full_transcript}"
                )
            else:
                final_content = full_transcript

        # Refresh draft and update content (but don't mark completed yet
        # if we're about to start a server-side LLM chain — we need to set
        # llm_node_id in the same commit so the SSE all_complete event
        # includes it).
        db.session.refresh(draft)
        draft.set_content(final_content)
        draft.streaming_completed_chunks = len(completed_chunks)

        should_chain = (user_id and model and full_transcript.strip()
                        and label == 'Voice')

        if should_chain:
            try:
                _start_server_side_llm_chain(
                    draft, session_id, full_transcript,
                    user_id, parent_id, model, label,
                )
            except Exception as e:
                logger.error(
                    f"Server-side LLM chain failed for session "
                    f"{session_id}: {e}", exc_info=True
                )
                # Non-fatal: mark completed without llm_node_id;
                # frontend can still trigger LLM the old way
                draft.streaming_status = 'completed'
                db.session.commit()
        else:
            draft.streaming_status = 'completed'
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


def _start_server_side_llm_chain(draft, session_id, transcript,
                                 user_id, parent_id, model, label):
    """
    Create nodes and kick off LLM + TTS generation server-side.

    Mirrors the logic in voice.py POST handler:
    1. If no parent_id → create system node (with workflow prompt)
    2. Create user node with transcript
    3. Move streaming audio to user node (without deleting draft)
    4. Create LLM placeholder node (enqueue=False)
    5. Set draft.llm_node_id AND draft.streaming_status='completed'
       in one commit so the SSE all_complete event includes llm_node_id
    6. Chain: generate_llm_response → generate_tts_audio
    """
    from celery import chain as celery_chain
    from backend.models import Node, NodeTranscriptChunk
    from backend.utils.prompts import get_user_prompt_record
    from backend.utils.llm_nodes import create_llm_placeholder
    from backend.utils.context_artifacts import attach_context_artifacts
    from backend.tasks.llm_completion import generate_llm_response
    from backend.tasks.tts import generate_tts_audio

    prompt_key = label.lower()  # 'voice'

    if parent_id:
        # Inherit ai_usage from parent node in the thread
        parent_node = Node.query.get(parent_id)
        ai_usage = (parent_node.ai_usage if parent_node
                    else draft.ai_usage) or "none"
        user_parent_id = parent_id
    else:
        # New thread — create system node with workflow prompt
        prompt_record = get_user_prompt_record(user_id, prompt_key)
        ai_usage = draft.ai_usage or "none"
        system_node = Node(
            user_id=user_id,
            human_owner_id=user_id,
            parent_id=None,
            node_type="user",
            privacy_level="private",
            ai_usage=ai_usage,

        )
        db.session.add(system_node)
        db.session.flush()
        attach_context_artifacts(
            system_node.id, user_id, prompt_record=prompt_record,
        )
        user_parent_id = system_node.id

    # User node with transcript
    from backend.utils.tokens import approximate_token_count
    user_node = Node(
        user_id=user_id,
        human_owner_id=user_id,
        parent_id=user_parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
        token_count=approximate_token_count(transcript),
    )
    user_node.set_content(transcript)
    db.session.add(user_node)
    db.session.flush()

    # Move streaming audio to user node — inline version of
    # attach_streaming_audio_to_node that does NOT delete the draft
    # (we still need it for the SSE all_complete event).
    audio_storage_root = pathlib.Path(
        os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
    ).resolve()
    draft_audio_dir = audio_storage_root / f"drafts/{user_id}/{session_id}"

    if draft_audio_dir.exists():
        node_audio_dir = (
            audio_storage_root / f"nodes/{user_id}/{user_node.id}"
        )
        try:
            node_audio_dir.mkdir(parents=True, exist_ok=True)
            for fp in draft_audio_dir.iterdir():
                shutil.move(str(fp), str(node_audio_dir / fp.name))
            draft_audio_dir.rmdir()
        except Exception as e:
            logger.warning(f"Failed to move audio files: {e}")

    # Point transcript-chunk rows at the node
    NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
    ).update({"node_id": user_node.id})
    user_node.streaming_transcription = True

    # LLM placeholder (don't enqueue yet — we'll chain it)
    llm_node, _ = create_llm_placeholder(
        user_node.id, model, user_id, enqueue=False,
        ai_usage=ai_usage,
    )

    # Mark TTS as pending now so the frontend's POST /tts endpoint
    # detects the in-progress chain and skips duplicate enqueue.
    llm_node.tts_task_status = 'pending'

    # CRITICAL: set llm_node_id AND streaming_status in one commit so
    # the SSE all_complete event includes the llm_node_id.
    draft.llm_node_id = llm_node.id
    draft.streaming_status = 'completed'
    db.session.commit()

    # Chain: LLM generation → TTS generation
    celery_chain(
        generate_llm_response.si(
            user_node.id, llm_node.id, model, user_id,
            source_mode='voice',
        ),
        generate_tts_audio.si(
            llm_node.id, str(audio_storage_root),
            requesting_user_id=user_id
        ),
    ).apply_async()

    logger.info(
        f"Server-side LLM chain started for session {session_id}: "
        f"user_node={user_node.id}, llm_node={llm_node.id}, model={model}"
    )
