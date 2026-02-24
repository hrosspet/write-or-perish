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
from backend.models import Node, UserProfile, TTSChunk, APICostLog
from backend.extensions import db
from backend.utils.audio_processing import adaptive_chunk_text
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import encrypt_file
from backend.utils.cost import calculate_audio_cost_microdollars

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


def _generate_tts_chunks(task, entity, text, target_dir, audio_storage_root,
                         chunk_fk_attr, entity_label,
                         requesting_user_id=None):
    """
    Shared TTS generation logic for both nodes and profiles.

    Handles text chunking, TTSChunk record creation, audio generation,
    encryption, concatenation, and cost logging.

    Both Node and UserProfile expose the same interface:
    tts_task_progress, audio_tts_url, user_id.

    Args:
        task: Celery task instance (for update_state)
        entity: Node or UserProfile model instance
        text: Text content to generate TTS for
        target_dir: Path to store audio files
        audio_storage_root: Root path for computing relative media URLs
        chunk_fk_attr: TTSChunk FK column name ('node_id' or 'profile_id')
        entity_label: Human-readable label for logging

    Returns:
        Final media URL string (e.g. '/media/user/1/node/2/tts.mp3')
    """
    AUDIO_ROOT = Path(audio_storage_root)

    # Get OpenAI API key
    api_key = get_openai_chat_key(flask_app.config)
    if not api_key:
        raise ValueError(
            "OpenAI API key not configured "
            "(set OPENAI_API_KEY_CHAT or OPENAI_API_KEY)"
        )

    task.update_state(
        state='PROGRESS', meta={'progress': 20, 'status': 'Preparing'}
    )
    entity.tts_task_progress = 20
    db.session.commit()

    client = OpenAI(api_key=api_key)
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / "tts.mp3"

    # Chunk text
    task.update_state(
        state='PROGRESS', meta={'progress': 30, 'status': 'Processing text'}
    )
    entity.tts_task_progress = 30
    db.session.commit()

    chunks = adaptive_chunk_text(text)
    logger.info(
        f"Text split into {len(chunks)} chunks for TTS "
        f"for {entity_label} (sizes: {[len(c) for c in chunks]})"
    )

    # Create TTSChunk records for streaming playback
    chunk_fk = {chunk_fk_attr: entity.id}
    for i in range(len(chunks)):
        tts_chunk = TTSChunk(chunk_index=i, status='pending', **chunk_fk)
        db.session.add(tts_chunk)
    db.session.commit()

    if len(chunks) == 1:
        # Single chunk: direct streaming to MP3
        task.update_state(
            state='PROGRESS',
            meta={'progress': 40, 'status': 'Generating audio'}
        )
        entity.tts_task_progress = 40
        db.session.commit()

        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts", input=chunks[0], voice="alloy"
        ) as resp:
            resp.stream_to_file(final_path)

        segment = AudioSegment.from_file(str(final_path), format="mp3")
        chunk_duration = len(segment) / 1000.0
        encrypt_file(str(final_path))

        tts_chunk = TTSChunk.query.filter_by(
            chunk_index=0, **chunk_fk
        ).first()
        if tts_chunk:
            rel_path = final_path.relative_to(AUDIO_ROOT)
            tts_chunk.audio_url = f"/media/{rel_path.as_posix()}"
            tts_chunk.duration = chunk_duration
            tts_chunk.status = 'completed'
            tts_chunk.completed_at = datetime.utcnow()
            db.session.commit()

    else:
        # Multiple chunks: generate parts for streaming playback
        audio_parts = []
        chunk_progress_step = 50 / len(chunks)

        for i, chunk in enumerate(chunks):
            progress = 40 + int((i + 1) * chunk_progress_step)
            task.update_state(
                state='PROGRESS',
                meta={
                    'progress': progress,
                    'status': f'Generating audio chunk {i+1}/{len(chunks)}'
                }
            )
            entity.tts_task_progress = progress

            tts_chunk = TTSChunk.query.filter_by(
                chunk_index=i, **chunk_fk
            ).first()
            if tts_chunk:
                tts_chunk.status = 'processing'
            db.session.commit()

            part_path = target_dir / f"tts_chunk_{i}.mp3"

            with client.audio.speech.with_streaming_response.create(
                model="gpt-4o-mini-tts", input=chunk, voice="alloy"
            ) as resp:
                resp.stream_to_file(part_path)

            segment = AudioSegment.from_file(str(part_path), format="mp3")
            chunk_duration = len(segment) / 1000.0
            audio_parts.append((i, segment, part_path))

            if tts_chunk:
                rel_path = part_path.relative_to(AUDIO_ROOT)
                tts_chunk.audio_url = f"/media/{rel_path.as_posix()}"
                tts_chunk.duration = chunk_duration
                tts_chunk.status = 'completed'
                tts_chunk.completed_at = datetime.utcnow()
                db.session.commit()

            encrypt_file(str(part_path))

        # Concatenate all segments into final file
        task.update_state(
            state='PROGRESS',
            meta={'progress': 90, 'status': 'Combining audio'}
        )
        entity.tts_task_progress = 90
        db.session.commit()

        combined = sum([part[1] for part in audio_parts])
        combined.export(final_path, format="mp3")
        encrypt_file(str(final_path))

    # Log TTS cost based on total audio duration
    total_duration = 0.0
    for tc in TTSChunk.query.filter_by(**chunk_fk).all():
        if tc.duration:
            total_duration += tc.duration
    if total_duration > 0:
        tts_cost = calculate_audio_cost_microdollars(
            "gpt-4o-mini-tts", total_duration
        )
        cost_log = APICostLog(
            user_id=requesting_user_id or entity.user_id,
            model_id="gpt-4o-mini-tts",
            request_type="tts",
            audio_duration_seconds=total_duration,
            cost_microdollars=tts_cost,
        )
        db.session.add(cost_log)

    # Finalize
    task.update_state(
        state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'}
    )
    entity.tts_task_progress = 95
    db.session.commit()

    rel_path = final_path.relative_to(AUDIO_ROOT)
    return f"/media/{rel_path.as_posix()}"


@celery.task(base=TTSTask, bind=True)
def generate_tts_audio(self, node_id: int, audio_storage_root: str,
                       requesting_user_id: int = None):
    """
    Asynchronously generate TTS audio for a node.

    Args:
        node_id: Database ID of the node
        audio_storage_root: Root directory for audio storage
        requesting_user_id: ID of the user who requested TTS (for cost attribution)
    """
    logger.info(f"Starting TTS generation task for node {node_id}")

    with flask_app.app_context():
        node = Node.query.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        node.tts_task_status = 'processing'
        node.tts_task_progress = 10
        db.session.commit()

        try:
            if node.audio_original_url:
                raise ValueError("Original audio exists – TTS not required")

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

            text = node.get_content() or ""
            if not text:
                raise ValueError("No content to generate TTS for")

            target_dir = (
                Path(audio_storage_root)
                / f"user/{node.user_id}/node/{node.id}"
            )

            url = _generate_tts_chunks(
                self, node, text, target_dir, audio_storage_root,
                'node_id', f"node {node_id}",
                requesting_user_id=requesting_user_id
            )

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
            logger.error(
                f"TTS generation error for node {node_id}: {e}",
                exc_info=True
            )
            node.tts_task_status = 'failed'
            db.session.commit()
            raise


@celery.task(base=ProfileTTSTask, bind=True)
def generate_tts_audio_for_profile(self, profile_id: int,
                                   audio_storage_root: str,
                                   requesting_user_id: int = None):
    """
    Asynchronously generate TTS audio for a user profile.

    Args:
        profile_id: Database ID of the user profile
        audio_storage_root: Root directory for audio storage
        requesting_user_id: ID of the user who requested TTS (for cost attribution)
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
                logger.info(
                    f"TTS already available for profile {profile_id}"
                )
                profile.tts_task_status = 'completed'
                profile.tts_task_progress = 100
                db.session.commit()
                return {
                    'profile_id': profile_id,
                    'status': 'completed',
                    'tts_url': profile.audio_tts_url
                }

            text = profile.get_content() or ""
            if not text:
                raise ValueError("No content to generate TTS for")

            target_dir = (
                Path(audio_storage_root)
                / f"user/{profile.user_id}/profile/{profile.id}"
            )

            url = _generate_tts_chunks(
                self, profile, text, target_dir, audio_storage_root,
                'profile_id', f"profile {profile_id}",
                requesting_user_id=requesting_user_id
            )

            profile.audio_tts_url = url
            profile.tts_task_status = 'completed'
            profile.tts_task_progress = 100
            db.session.commit()

            logger.info(
                f"TTS generation successful for profile {profile_id}"
            )
            return {
                'profile_id': profile_id,
                'status': 'completed',
                'tts_url': url
            }

        except Exception as e:
            logger.error(
                f"TTS generation error for profile {profile_id}: {e}",
                exc_info=True
            )
            profile.tts_task_status = 'failed'
            db.session.commit()
            raise
