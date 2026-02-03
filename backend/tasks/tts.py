"""
Celery task for asynchronous TTS generation.

Supports streaming playback - each chunk's audio URL is stored in TTSChunk
and can be played as soon as it's ready, without waiting for all chunks.
"""
from celery import Task
from celery.utils.log import get_task_logger
from openai import OpenAI
from pathlib import Path
from pydub import AudioSegment
import os
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node, UserProfile, TTSChunk
from backend.extensions import db
from backend.utils.audio_processing import chunk_text
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import encrypt_file

logger = get_task_logger(__name__)

# Audio storage root path (matches the one in routes/nodes.py)
import pathlib
AUDIO_STORAGE_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()


class TTSTask(Task):
    """Custom task class with error handling for Nodes."""

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


class ProfileTTSTask(Task):
    """Custom task class with error handling for UserProfiles."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        profile_id = args[0] if args else None
        if profile_id:
            with flask_app.app_context():
                profile = UserProfile.query.get(profile_id)
                if profile:
                    profile.tts_task_status = 'failed'
                    db.session.commit()
                    logger.error(f"TTS generation failed for profile {profile_id}: {exc}")


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

            # Get OpenAI API key (always use CHAT key for audio operations)
            api_key = get_openai_chat_key(flask_app.config)
            if not api_key:
                raise ValueError("OpenAI API key not configured (set OPENAI_API_KEY_CHAT or OPENAI_API_KEY)")

            text = node.get_content() or ""
            if not text:
                raise ValueError("No content to generate TTS for")

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
            # Create TTSChunk records for streaming playback
            for i in range(len(chunks)):
                tts_chunk = TTSChunk(
                    node_id=node.id,
                    chunk_index=i,
                    status='pending'
                )
                db.session.add(tts_chunk)
            db.session.commit()

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

                # Encrypt the audio file at rest
                encrypt_file(str(final_path))

                # Update the single TTSChunk record
                tts_chunk = TTSChunk.query.filter_by(node_id=node.id, chunk_index=0).first()
                if tts_chunk:
                    rel_path = final_path.relative_to(AUDIO_STORAGE_ROOT)
                    tts_chunk.audio_url = f"/media/{rel_path.as_posix()}"
                    tts_chunk.status = 'completed'
                    tts_chunk.completed_at = datetime.utcnow()
                    db.session.commit()

            else:
                # Multiple chunks: generate parts for streaming playback
                # Each chunk is immediately available for streaming as soon as it's generated
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

                    # Update TTSChunk status to processing
                    tts_chunk = TTSChunk.query.filter_by(node_id=node.id, chunk_index=i).first()
                    if tts_chunk:
                        tts_chunk.status = 'processing'
                    db.session.commit()

                    # Store each chunk separately for streaming playback
                    part_path = target_dir / f"tts_chunk_{i}.mp3"

                    with client.audio.speech.with_streaming_response.create(
                        model="gpt-4o-mini-tts",
                        input=chunk,
                        voice="alloy"
                    ) as resp:
                        resp.stream_to_file(part_path)

                    # Update TTSChunk with URL immediately (for streaming playback)
                    if tts_chunk:
                        rel_path = part_path.relative_to(AUDIO_STORAGE_ROOT)
                        tts_chunk.audio_url = f"/media/{rel_path.as_posix()}"
                        tts_chunk.status = 'completed'
                        tts_chunk.completed_at = datetime.utcnow()
                        db.session.commit()

                    # Load into AudioSegment for final concatenation BEFORE encrypting
                    segment = AudioSegment.from_file(str(part_path), format="mp3")
                    audio_parts.append((i, segment, part_path))

                    # Encrypt the chunk file at rest (for streaming playback)
                    encrypt_file(str(part_path))

                # Concatenate all segments into final file
                self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Combining audio'})
                node.tts_task_progress = 90
                db.session.commit()

                combined = sum([part[1] for part in audio_parts])
                combined.export(final_path, format="mp3")

                # Encrypt the combined final file at rest
                encrypt_file(str(final_path))

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


@celery.task(base=ProfileTTSTask, bind=True)
def generate_tts_audio_for_profile(self, profile_id: int, audio_storage_root: str):
    """
    Asynchronously generate TTS audio for a user profile.

    Args:
        profile_id: Database ID of the user profile
        audio_storage_root: Root directory for audio storage
    """
    logger.info(f"Starting TTS generation task for profile {profile_id}")

    with flask_app.app_context():
        profile = UserProfile.query.get(profile_id)
        if not profile:
            raise ValueError(f"UserProfile {profile_id} not found")

        profile.tts_task_status = 'processing'
        profile.tts_task_progress = 10
        db.session.commit()

        try:
            if profile.audio_tts_url:
                logger.info(f"TTS already available for profile {profile_id}")
                profile.tts_task_status = 'completed'
                profile.tts_task_progress = 100
                db.session.commit()
                return {
                    'profile_id': profile_id,
                    'status': 'completed',
                    'tts_url': profile.audio_tts_url
                }

            # Get OpenAI API key (always use CHAT key for audio operations)
            api_key = get_openai_chat_key(flask_app.config)
            if not api_key:
                raise ValueError("OpenAI API key not configured (set OPENAI_API_KEY_CHAT or OPENAI_API_KEY)")

            text = profile.get_content() or ""
            if not text:
                raise ValueError("No content to generate TTS for")

            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Preparing'})
            profile.tts_task_progress = 20
            db.session.commit()

            client = OpenAI(api_key=api_key)
            AUDIO_STORAGE_ROOT = Path(audio_storage_root)
            target_dir = AUDIO_STORAGE_ROOT / f"user/{profile.user_id}/profile/{profile.id}"
            target_dir.mkdir(parents=True, exist_ok=True)
            final_path = target_dir / "tts.mp3"

            self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Processing text'})
            profile.tts_task_progress = 30
            db.session.commit()

            chunks = chunk_text(text, max_chars=4096)
            logger.info(f"Text split into {len(chunks)} chunks for TTS for profile {profile_id}")

            if len(chunks) == 1:
                self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating audio'})
                profile.tts_task_progress = 40
                db.session.commit()

                with client.audio.speech.with_streaming_response.create(
                    model="gpt-4o-mini-tts",
                    input=chunks[0],
                    voice="alloy"
                ) as resp:
                    resp.stream_to_file(final_path)

                # Encrypt the audio file at rest
                encrypt_file(str(final_path))
            else:
                audio_parts = []
                chunk_progress_step = 50 / len(chunks)

                for i, chunk in enumerate(chunks):
                    progress = 40 + int((i + 1) * chunk_progress_step)
                    self.update_state(
                        state='PROGRESS',
                        meta={
                            'progress': progress,
                            'status': f'Generating audio chunk {i+1}/{len(chunks)}'
                        }
                    )
                    profile.tts_task_progress = progress
                    db.session.commit()

                    part_path = target_dir / f"tts_part{i}.mp3"

                    with client.audio.speech.with_streaming_response.create(
                        model="gpt-4o-mini-tts",
                        input=chunk,
                        voice="alloy"
                    ) as resp:
                        resp.stream_to_file(part_path)

                    segment = AudioSegment.from_file(str(part_path), format="mp3")
                    audio_parts.append(segment)

                    # Encrypt or delete the part file
                    try:
                        part_path.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to delete part file: {e}")

                self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Combining audio'})
                profile.tts_task_progress = 90
                db.session.commit()

                combined = sum(audio_parts)
                combined.export(final_path, format="mp3")

                # Encrypt the combined final file at rest
                encrypt_file(str(final_path))

            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'})
            profile.tts_task_progress = 95
            db.session.commit()

            rel_path = final_path.relative_to(AUDIO_STORAGE_ROOT)
            url = f"/media/{rel_path.as_posix()}"
            profile.audio_tts_url = url
            profile.tts_task_status = 'completed'
            profile.tts_task_progress = 100
            db.session.commit()

            logger.info(f"TTS generation successful for profile {profile_id}")

            return {
                'profile_id': profile_id,
                'status': 'completed',
                'tts_url': url
            }

        except Exception as e:
            logger.error(f"TTS generation error for profile {profile_id}: {e}", exc_info=True)
            profile.tts_task_status = 'failed'
            db.session.commit()
            raise
