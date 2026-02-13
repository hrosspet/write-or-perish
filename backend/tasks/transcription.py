"""
Celery task for asynchronous audio transcription.
"""
from celery import Task
from celery.utils.log import get_task_logger
from openai import OpenAI
import pathlib
import os
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node
from backend.extensions import db
from backend.utils.audio_processing import (
    compress_audio_if_needed,
    get_audio_duration,
    chunk_audio,
    OPENAI_MAX_AUDIO_BYTES,
    OPENAI_MAX_DURATION_SEC
)
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import decrypt_file_to_temp

logger = get_task_logger(__name__)


class TranscriptionTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        node_id = args[0] if args else None
        if node_id:
            with flask_app.app_context():
                node = Node.query.get(node_id)
                if node:
                    node.transcription_status = 'failed'
                    node.transcription_error = str(exc)[:500]
                    node.transcription_completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.error(f"Transcription failed for node {node_id}: {exc}")


@celery.task(base=TranscriptionTask, bind=True)
def transcribe_audio(self, node_id: int, audio_file_path: str, filename: str = None):
    """
    Asynchronously transcribe an audio file.

    Args:
        node_id: Database ID of the node
        audio_file_path: Absolute path to the audio file
        filename: Original filename of the audio file
    """
    logger.info(f"Starting transcription task for node {node_id}")

    with flask_app.app_context():
        # Get node from database
        node = Node.query.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        # Update status to processing
        node.transcription_status = 'processing'
        node.transcription_started_at = datetime.utcnow()
        node.transcription_progress = 0
        db.session.commit()

        try:
            # Get OpenAI API key (always use CHAT key for audio operations)
            api_key = get_openai_chat_key(flask_app.config)
            if not api_key:
                raise ValueError("OpenAI API key not configured (set OPENAI_API_KEY_CHAT or OPENAI_API_KEY)")

            # No timeout - let Celery handle task limits
            client = OpenAI(api_key=api_key)
            file_path = pathlib.Path(audio_file_path)

            # Check for encrypted version (.enc) if plain file doesn't exist
            temp_decrypted = None
            if not file_path.exists() and pathlib.Path(audio_file_path + '.enc').exists():
                logger.info(f"Found encrypted file, decrypting: {audio_file_path}.enc")
                temp_decrypted = decrypt_file_to_temp(audio_file_path + '.enc')
                file_path = pathlib.Path(temp_decrypted)
            elif not file_path.exists():
                raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

            # Step 1: Compress if needed (10% progress)
            self.update_state(state='PROGRESS', meta={'progress': 10, 'status': 'Compressing audio'})
            node.transcription_progress = 10
            db.session.commit()

            processed_path = compress_audio_if_needed(file_path, logger)

            # Step 2: Check size (20% progress)
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Analyzing audio'})
            node.transcription_progress = 20
            db.session.commit()

            file_size = processed_path.stat().st_size

            # For large files, skip expensive duration check - we know they need chunking
            # This avoids loading 190MB+ files into memory just to check duration
            if file_size > OPENAI_MAX_AUDIO_BYTES:
                needs_chunking = True
                duration_sec = 0  # Unknown, but doesn't matter - file size already requires chunking
                logger.info(f"Large audio file: {file_size / 1024 / 1024:.1f} MB - will use chunked transcription")
            else:
                duration_sec = get_audio_duration(processed_path, logger)
                logger.info(f"Transcribing audio: {file_size / 1024 / 1024:.1f} MB, {duration_sec:.0f} seconds")
                needs_chunking = duration_sec > OPENAI_MAX_DURATION_SEC

            transcript = None

            if not needs_chunking:
                # Simple case: transcribe whole file (30% -> 90% progress)
                self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Transcribing'})
                node.transcription_progress = 30
                db.session.commit()

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

                self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Finalizing'})
                node.transcription_progress = 90
                db.session.commit()

            else:
                # Complex case: chunk and transcribe
                logger.info(f"File exceeds limits, using chunked transcription")

                self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Creating chunks'})
                node.transcription_progress = 30
                db.session.commit()

                chunk_paths = chunk_audio(processed_path, logger=logger)

                if not chunk_paths:
                    raise Exception("Failed to create audio chunks")

                transcripts = []
                chunk_progress_step = 60 / len(chunk_paths)  # 30% -> 90% split across chunks

                try:
                    for i, chunk_path in enumerate(chunk_paths):
                        progress = 30 + int((i + 1) * chunk_progress_step)
                        self.update_state(
                            state='PROGRESS',
                            meta={
                                'progress': progress,
                                'status': f'Transcribing chunk {i+1}/{len(chunk_paths)}'
                            }
                        )
                        node.transcription_progress = progress
                        db.session.commit()

                        logger.info(f"Transcribing chunk {i + 1}/{len(chunk_paths)}")

                        with open(chunk_path, "rb") as audio_file:
                            resp = client.audio.transcriptions.create(
                                model="gpt-4o-transcribe",
                                file=audio_file,
                                response_format="text"
                            )

                            if hasattr(resp, "text"):
                                chunk_text = resp.text
                            elif isinstance(resp, dict):
                                chunk_text = resp.get("text") or resp.get("transcript") or ""
                            else:
                                chunk_text = str(resp)

                            transcripts.append(chunk_text)

                    transcript = "\n\n".join(transcripts)
                    logger.info(f"Chunked transcription complete: {len(chunk_paths)} chunks")

                finally:
                    # Clean up chunk files
                    for chunk_path in chunk_paths:
                        try:
                            os.unlink(chunk_path)
                        except Exception as e:
                            logger.warning(f"Failed to delete chunk: {e}")

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

            # Step 4: Update node with transcript (100% progress)
            self.update_state(state='PROGRESS', meta={'progress': 100, 'status': 'Complete'})

            # Only add title for top-level nodes, not replies
            if node.parent_id is None and filename:
                header, _ = os.path.splitext(filename)
                # For recordings made via the app, use date/time instead of "recording"
                if header == "recording":
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    final_content = f"# {timestamp} Voice note\n\n{transcript}"
                else:
                    final_content = f"# {header}\n\n{transcript}"
            else:
                final_content = transcript

            node.set_content(final_content or node.get_content())
            node.transcription_status = 'completed'
            node.transcription_progress = 100
            node.transcription_completed_at = datetime.utcnow()
            node.transcription_error = None
            db.session.commit()

            logger.info(f"Transcription successful for node {node_id}: {len(transcript)} characters")

            return {
                'node_id': node_id,
                'status': 'completed',
                'transcript_length': len(transcript)
            }

        except Exception as e:
            logger.error(f"Transcription error for node {node_id}: {e}", exc_info=True)
            node.transcription_status = 'failed'
            node.transcription_error = str(e)[:500]
            node.transcription_completed_at = datetime.utcnow()
            db.session.commit()
            raise
