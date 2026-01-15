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
from backend.models import Node, NodeTranscriptChunk
from backend.extensions import db

logger = get_task_logger(__name__)


def convert_chunk_to_mp3(chunk_path: pathlib.Path, logger) -> pathlib.Path:
    """
    Force-convert an audio chunk to MP3 format.

    This is necessary because when uploading chunked files, the chunks are
    raw byte slices that may not be valid audio containers. FFmpeg can often
    recover audio from partial/corrupt streams by re-encoding.

    Returns path to the converted MP3 file.
    """
    import subprocess

    mp3_path = chunk_path.with_suffix('.mp3')

    try:
        logger.info(f"Converting chunk {chunk_path.name} to MP3 for transcription")

        # Use ffmpeg to convert - it handles corrupt/partial streams better
        result = subprocess.run(
            [
                'ffmpeg', '-y',  # Overwrite output
                '-i', str(chunk_path),
                '-acodec', 'libmp3lame',
                '-ab', '128k',
                '-ar', '16000',  # 16kHz is good for speech
                '-ac', '1',  # Mono
                str(mp3_path)
            ],
            capture_output=True,
            timeout=120  # 2 minute timeout
        )

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace')
            logger.error(f"FFmpeg conversion failed: {stderr}")
            raise RuntimeError(f"FFmpeg conversion failed: {stderr[:500]}")

        # Verify the output file exists and has content
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced empty or no output file")

        logger.info(f"Converted {chunk_path.name} -> {mp3_path.name} ({mp3_path.stat().st_size / 1024:.1f} KB)")
        return mp3_path

    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg conversion timed out for {chunk_path.name}")
        raise RuntimeError("FFmpeg conversion timed out")
    except Exception as e:
        logger.error(f"Failed to convert chunk to MP3: {e}")
        raise


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
            # Get OpenAI API key
            api_key = flask_app.config.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")

            client = OpenAI(api_key=api_key)
            file_path = pathlib.Path(chunk_path)

            if not file_path.exists():
                raise FileNotFoundError(f"Audio chunk file not found: {chunk_path}")

            # Force convert to MP3 - streaming chunks are raw byte slices
            # that need to be re-encoded to be valid audio files
            processed_path = convert_chunk_to_mp3(file_path, logger)

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

            # Update chunk record with transcript
            chunk_record.text = transcript
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
            if chunk.text:
                transcripts.append(chunk.text)

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
        node.content = final_content
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


def split_audio_into_chunks(file_path: pathlib.Path, chunk_duration_sec: int = 300, logger=None) -> list:
    """
    Split an audio file into proper audio chunks using ffmpeg.

    Unlike raw byte slicing, this creates valid audio files that can be transcribed.
    Each chunk has proper headers and can be independently decoded.

    Args:
        file_path: Path to the audio file
        chunk_duration_sec: Duration of each chunk in seconds (default 5 minutes)
        logger: Logger instance

    Returns:
        List of paths to chunk files
    """
    import subprocess
    import tempfile

    if logger is None:
        logger = get_task_logger(__name__)

    try:
        # Get audio duration using ffprobe
        probe_result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', str(file_path)],
            capture_output=True,
            timeout=60
        )
        duration = float(probe_result.stdout.decode().strip())
        logger.info(f"Audio duration: {duration:.1f} seconds")

        num_chunks = max(1, int(duration / chunk_duration_sec) + (1 if duration % chunk_duration_sec > 0 else 0))
        chunk_paths = []

        # Create temp directory for chunks
        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix='streaming_chunks_'))

        for i in range(num_chunks):
            start_time = i * chunk_duration_sec
            chunk_path = temp_dir / f"chunk_{i:04d}.mp3"

            # Use ffmpeg to extract chunk as proper MP3
            result = subprocess.run(
                [
                    'ffmpeg', '-y',
                    '-ss', str(start_time),
                    '-i', str(file_path),
                    '-t', str(chunk_duration_sec),
                    '-acodec', 'libmp3lame',
                    '-ab', '128k',
                    '-ar', '16000',
                    '-ac', '1',
                    str(chunk_path)
                ],
                capture_output=True,
                timeout=120
            )

            if result.returncode != 0:
                stderr = result.stderr.decode('utf-8', errors='replace')
                logger.error(f"FFmpeg chunk {i} failed: {stderr[:500]}")
                continue

            if chunk_path.exists() and chunk_path.stat().st_size > 0:
                chunk_paths.append(str(chunk_path))
                logger.info(f"Created chunk {i + 1}/{num_chunks}: {chunk_path.name} ({chunk_path.stat().st_size / 1024:.1f} KB)")
            else:
                logger.warning(f"Chunk {i} was empty or not created")

        return chunk_paths

    except Exception as e:
        logger.error(f"Failed to split audio: {e}", exc_info=True)
        return []


@celery.task(base=FinalizationTask, bind=True)
def transcribe_uploaded_file_streaming(self, node_id: int, file_path: str, session_id: str):
    """
    Transcribe an uploaded audio file using streaming approach.

    This task:
    1. Splits the audio file into proper chunks using ffmpeg
    2. Creates NodeTranscriptChunk records for each
    3. Transcribes each chunk sequentially
    4. Updates the node with the final assembled transcript

    Unlike live recording chunks, uploaded files need server-side splitting
    because frontend file.slice() produces raw bytes without audio headers.

    Args:
        node_id: Database ID of the node
        file_path: Path to the reassembled audio file
        session_id: Session ID for SSE updates
    """
    logger.info(f"Starting streaming transcription for uploaded file, node {node_id}")

    with flask_app.app_context():
        node = Node.query.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        file_path = pathlib.Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {file_path}")

        # Split audio into proper chunks
        logger.info(f"Splitting audio file: {file_path}")
        chunk_paths = split_audio_into_chunks(file_path, chunk_duration_sec=300, logger=logger)

        if not chunk_paths:
            raise RuntimeError("Failed to split audio file into chunks")

        total_chunks = len(chunk_paths)
        node.streaming_total_chunks = total_chunks
        db.session.commit()

        logger.info(f"Split into {total_chunks} chunks, starting transcription")

        # Get OpenAI client
        api_key = flask_app.config.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        client = OpenAI(api_key=api_key)

        completed_chunks = []
        failed_chunks = []

        for chunk_index, chunk_path in enumerate(chunk_paths):
            chunk_path = pathlib.Path(chunk_path)

            # Create transcript chunk record
            transcript_chunk = NodeTranscriptChunk(
                node_id=node_id,
                chunk_index=chunk_index,
                status='processing'
            )
            db.session.add(transcript_chunk)
            db.session.commit()

            try:
                logger.info(f"Transcribing chunk {chunk_index + 1}/{total_chunks}")

                with open(chunk_path, "rb") as audio_file:
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

                # Update chunk record
                transcript_chunk.text = transcript
                transcript_chunk.status = 'completed'
                transcript_chunk.completed_at = datetime.utcnow()
                db.session.commit()

                completed_chunks.append(transcript_chunk)
                node.streaming_completed_chunks = len(completed_chunks)
                db.session.commit()

                logger.info(
                    f"Chunk {chunk_index + 1}/{total_chunks} completed: "
                    f"{len(transcript)} characters"
                )

            except Exception as e:
                logger.error(f"Chunk {chunk_index} transcription failed: {e}")
                transcript_chunk.status = 'failed'
                transcript_chunk.error = str(e)[:500]
                transcript_chunk.completed_at = datetime.utcnow()
                db.session.commit()
                failed_chunks.append(chunk_index)

            finally:
                # Clean up chunk file
                try:
                    chunk_path.unlink()
                except Exception:
                    pass

        # Clean up temp directory
        if chunk_paths:
            temp_dir = pathlib.Path(chunk_paths[0]).parent
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.warning(f"Failed to clean up temp dir: {e}")

        # Assemble final transcript
        transcripts = []
        for chunk in sorted(completed_chunks, key=lambda c: c.chunk_index):
            if chunk.text:
                transcripts.append(chunk.text)

        full_transcript = "\n\n".join(transcripts)

        # Format final content
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if failed_chunks:
            final_content = (
                f"# {timestamp} Voice note\n\n"
                f"*Note: Chunks {failed_chunks} failed to transcribe*\n\n"
                f"{full_transcript}"
            )
        else:
            final_content = f"# {timestamp} Voice note\n\n{full_transcript}"

        # Update node
        node.content = final_content
        node.transcription_status = 'completed'
        node.transcription_completed_at = datetime.utcnow()
        node.transcription_progress = 100
        db.session.commit()

        logger.info(
            f"Streaming transcription completed for node {node_id}: "
            f"{len(completed_chunks)} chunks, {len(full_transcript)} characters"
        )

        return {
            'node_id': node_id,
            'status': 'completed',
            'completed_chunks': len(completed_chunks),
            'failed_chunks': len(failed_chunks),
            'transcript_length': len(full_transcript)
        }
