"""
Celery task for asynchronous TTS generation.
"""
from celery import Task
from celery.utils.log import get_task_logger
from openai import OpenAI
from pathlib import Path
from pydub import AudioSegment
import os

from backend.celery_app import celery, flask_app
from backend.models import Node
from backend.extensions import db
from backend.utils.audio_processing import chunk_text

logger = get_task_logger(__name__)


class TTSTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        node_id = args[0] if args else None
        if node_id:
            with flask_app.app_context():
                node = Node.query.get(node_id)
                if node:
                    node.tts_task_status = 'failed'
                    db.session.commit()
                    logger.error(f"TTS generation failed for node {node_id}: {exc}")


@celery.task(base=TTSTask, bind=True)
def generate_tts_audio(self, node_id: int, audio_storage_root: str):
    """
    Asynchronously generate TTS audio for a node.

    Args:
        node_id: Database ID of the node
        audio_storage_root: Root directory for audio storage
    """
    logger.info(f"Starting TTS generation task for node {node_id}")

    with flask_app.app_context():
        # Get node from database
        node = Node.query.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        # Update status to processing
        node.tts_task_status = 'processing'
        node.tts_task_progress = 10
        db.session.commit()

        try:
            # Check if original audio exists
            if node.audio_original_url:
                raise ValueError("Original audio exists – TTS not required")

            # Check if already generated
            if node.audio_tts_url:
                logger.info(f"TTS already available for node {node_id}")
                node.tts_task_status = 'completed'
                node.tts_task_progress = 100
                db.session.commit()
                return {
                    'node_id': node_id,
                    'status': 'completed',
                    'tts_url': node.audio_tts_url
                }

            # Get OpenAI API key
            api_key = flask_app.config.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")

            text = node.content or ""
            if len(text) > 10000:
                raise ValueError("Content too long for TTS (max 10,000 characters)")

            # Step 1: Initialize client and prepare storage (20% progress)
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Preparing'})
            node.tts_task_progress = 20
            db.session.commit()

            client = OpenAI(api_key=api_key)
            AUDIO_STORAGE_ROOT = Path(audio_storage_root)
            target_dir = AUDIO_STORAGE_ROOT / f"user/{node.user_id}/node/{node.id}"
            target_dir.mkdir(parents=True, exist_ok=True)
            final_path = target_dir / "tts.mp3"

            # Step 2: Chunk text (30% progress)
            self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Processing text'})
            node.tts_task_progress = 30
            db.session.commit()

            chunks = chunk_text(text, max_chars=4096)
            logger.info(f"Text split into {len(chunks)} chunks for TTS")

            # Step 3: Generate TTS (40% -> 90% progress)
            if len(chunks) == 1:
                # Single chunk: direct streaming to MP3
                self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating audio'})
                node.tts_task_progress = 40
                db.session.commit()

                with client.audio.speech.with_streaming_response.create(
                    model="gpt-4o-mini-tts",
                    input=chunks[0],
                    voice="alloy"
                ) as resp:
                    resp.stream_to_file(final_path)

            else:
                # Multiple chunks: generate parts and concatenate
                audio_parts = []
                chunk_progress_step = 50 / len(chunks)  # 40% -> 90% split across chunks

                for i, chunk in enumerate(chunks):
                    progress = 40 + int((i + 1) * chunk_progress_step)
                    self.update_state(
                        state='PROGRESS',
                        meta={
                            'progress': progress,
                            'status': f'Generating audio chunk {i+1}/{len(chunks)}'
                        }
                    )
                    node.tts_task_progress = progress
                    db.session.commit()

                    part_path = target_dir / f"tts_part{i}.mp3"

                    with client.audio.speech.with_streaming_response.create(
                        model="gpt-4o-mini-tts",
                        input=chunk,
                        voice="alloy"
                    ) as resp:
                        resp.stream_to_file(part_path)

                    # Load into AudioSegment
                    segment = AudioSegment.from_file(str(part_path), format="mp3")
                    audio_parts.append(segment)

                    # Cleanup part file
                    try:
                        part_path.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to delete part file: {e}")

                # Concatenate all segments
                self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Combining audio'})
                node.tts_task_progress = 90
                db.session.commit()

                combined = sum(audio_parts)
                combined.export(final_path, format="mp3")

            # Step 4: Update node record (95% progress)
            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'})
            node.tts_task_progress = 95
            db.session.commit()

            rel_path = final_path.relative_to(AUDIO_STORAGE_ROOT)
            url = f"/media/{rel_path.as_posix()}"
            node.audio_tts_url = url
            node.audio_mime_type = "audio/mpeg"
            node.tts_task_status = 'completed'
            node.tts_task_progress = 100
            db.session.commit()

            logger.info(f"TTS generation successful for node {node_id}")

            return {
                'node_id': node_id,
                'status': 'completed',
                'tts_url': url
            }

        except Exception as e:
            logger.error(f"TTS generation error for node {node_id}: {e}", exc_info=True)
            node.tts_task_status = 'failed'
            db.session.commit()
            raise
