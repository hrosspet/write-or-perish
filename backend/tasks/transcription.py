"""
Celery task for asynchronous audio transcription.

No monthly spend-cap check here (#341): an upload the server accepted is
transcribed to the end. The upload endpoints refuse a capped user before
any file is stored.
"""
from celery import Task
from celery.utils.log import get_task_logger
from openai import OpenAI
import pathlib
import os
import time
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node, APICostLog
from backend.extensions import db
from backend.utils.audio_processing import (
    SPLIT_TIMEOUT_SECONDS,
    compress_audio_if_needed,
    decode_timeout_seconds,
    decoded_audio_seconds,
    seconds_left,
    split_audio_for_transcription,
    CHUNK_DURATION_SEC,
    OPENAI_MAX_AUDIO_BYTES,
    OPENAI_MAX_DURATION_SEC
)
from backend.utils.recording_limits import (
    MAX_UPLOAD_TRANSCRIBED_SECONDS, UPLOAD_NOTE_PART, with_note,
)
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import decrypt_file_to_temp
from backend.utils.cost import calculate_audio_cost_microdollars
from backend.utils.error_privacy import (
    REPLY_NOT_STARTED_TEXT, TRANSCRIPTION_FAILED_TEXT, describe_exception,
)

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
                    node.transcription_error = TRANSCRIPTION_FAILED_TEXT
                    node.transcription_completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.error(
                        "Transcription failed for node %s: %s", node_id,
                        describe_exception(exc))


# Warning recorded on an uploaded entry whose reply the cap skipped (#342).
UPLOAD_REPLY_SKIPPED_SPEND_CAP = (
    "Your entry is saved. Loore didn't reply because you've reached your "
    "monthly usage limit, which resets at the start of next month."
)
# ...and whose reply AI usage refused (llm_nodes.reply_refusal).
UPLOAD_REPLY_SKIPPED_AI_USAGE = (
    "Your entry is saved. Loore didn't reply because AI usage is set to "
    "None."
)


def _start_upload_reply(node, tip, model_id):
    """The LLM reply a Text-mode upload asked for (#342), under the tip of
    the (possibly split) transcript. Returns the placeholder, or None when
    the reply was skipped: a spend-capped user, a refused placeholder, or
    any other failure. The reason is recorded on the entry, which is
    already committed and always kept."""
    from backend.utils.spend import user_is_capped
    from backend.utils.task_warnings import record_task_warning
    if user_is_capped(node.user_id):
        record_task_warning(node, UPLOAD_REPLY_SKIPPED_SPEND_CAP)
        return None
    from backend.utils.llm_nodes import (
        AIUsageRefused, ReadOnlyModelRefused, auto_generate_refusal,
        create_llm_placeholder,
    )
    # The entry may have been made public (or shared with circles) while
    # it was transcribed: no automatic reply there (Peter, 2026-11-12).
    auto_off = auto_generate_refusal(tip)
    if auto_off is not None:
        record_task_warning(node, f"Your entry is saved. {auto_off.message}")
        return None
    try:
        llm_node, _ = create_llm_placeholder(
            tip.id, model_id, node.user_id,
            ai_usage=node.ai_usage,
            source_mode="textmode",
        )
        return llm_node
    except Exception as e:  # never fail the transcript over its reply
        db.session.rollback()
        logger.warning(
            "Upload reply skipped for node %s: %s", node.id,
            describe_exception(e))
        from backend.utils.thread_size import (
            ThreadTooLong,
        )
        if isinstance(e, AIUsageRefused):
            message = UPLOAD_REPLY_SKIPPED_AI_USAGE
        elif isinstance(e, ReadOnlyModelRefused):
            message = f"Your entry is saved. {e.message}"
        elif isinstance(e, ThreadTooLong):
            # The thread's next prompt would be over the limit (#648).
            message = e.entry_message
        else:
            message = REPLY_NOT_STARTED_TEXT
        record_task_warning(node, message)
        return None


def _log_transcription_cost(user_id, seconds):
    """A cost row for *seconds* of audio sent to the provider (none for 0)."""
    if seconds <= 0:
        return
    db.session.add(APICostLog(
        user_id=user_id,
        model_id="gpt-4o-transcribe",
        request_type="transcription",
        audio_duration_seconds=seconds,
        cost_microdollars=calculate_audio_cost_microdollars(
            "gpt-4o-transcribe", seconds),
    ))


@celery.task(base=TranscriptionTask, bind=True)
def transcribe_audio(self, node_id: int, audio_file_path: str, filename: str = None,
                     auto_reply_model: str = None):
    """
    Asynchronously transcribe an audio file.

    Args:
        node_id: Database ID of the node
        audio_file_path: Absolute path to the audio file
        filename: Original filename of the audio file
        auto_reply_model: When set (a Text-mode upload with auto-generate
            on, #342), create the LLM reply once the transcript is written.
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

        # Seconds of audio sent to the provider: the cost record, also when
        # a request fails.
        sent_seconds = 0.0
        cost_logged = False

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

            # The file's length, counted from its decoded audio (not its
            # container's timestamps; utils/recording_limits). It is sent
            # whole only when that length is known and fits one request;
            # otherwise it is cut at MAX_UPLOAD_TRANSCRIBED_SECONDS and split
            # into pieces re-timed from their samples, so the bound holds
            # whether or not the length could be measured.
            # One deadline for the file's audio work (measuring, cutting,
            # measuring the pieces): SPLIT_TIMEOUT_SECONDS.
            deadline = time.monotonic() + SPLIT_TIMEOUT_SECONDS
            decoded_sec = decoded_audio_seconds(
                processed_path,
                timeout_seconds=seconds_left(
                    deadline, decode_timeout_seconds(processed_path)),
                logger=logger)
            needs_chunking = (decoded_sec is None
                              or file_size > OPENAI_MAX_AUDIO_BYTES
                              or decoded_sec > OPENAI_MAX_DURATION_SEC)
            logger.info(
                f"Transcribing audio: {file_size / 1024 / 1024:.1f} MB, "
                + (f"{decoded_sec:.0f} seconds" if decoded_sec is not None
                   else "length unknown")
                + (", in pieces" if needs_chunking else ""))
            # The file holds audio past the bound (not transcribed).
            cut = False

            transcript = None

            if not needs_chunking:
                # Simple case: transcribe whole file (30% -> 90% progress)
                self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Transcribing'})
                node.transcription_progress = 30
                db.session.commit()

                sent_seconds = decoded_sec
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

                pieces, cut = split_audio_for_transcription(
                    processed_path, CHUNK_DURATION_SEC,
                    MAX_UPLOAD_TRANSCRIBED_SECONDS, logger=logger,
                    deadline=deadline)
                chunk_paths = [path for path, _ in pieces]

                if not chunk_paths:
                    raise Exception("Failed to create audio chunks")

                transcripts = []
                chunk_progress_step = 60 / len(chunk_paths)  # 30% -> 90% split across chunks

                try:
                    for i, (chunk_path, piece_seconds) in enumerate(pieces):
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

                        # What is sent: decoded, or the most a piece of
                        # this size can hold (never less); counted also if
                        # the request fails.
                        sent_seconds += piece_seconds
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
                    try:
                        os.rmdir(os.path.dirname(chunk_paths[0]))
                    except OSError:
                        pass

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

            # Cut at the bound: the entry says the rest is audio only.
            if cut:
                transcript = with_note(transcript, UPLOAD_NOTE_PART)

            # Log transcription cost: the audio sent to the provider
            _log_transcription_cost(node.user_id, sent_seconds)
            cost_logged = True

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

            final_content = final_content or node.get_content()
            node.set_content(final_content)
            from backend.utils.tokens import approximate_token_count
            node.token_count = approximate_token_count(final_content)
            # Per-node cap: very long recordings (~2h+ of speech) can
            # exceed it; split into a serial chain. Audio stays on this
            # head node; the transcript continues across the chain.
            from backend.utils.node_split import split_node_into_chain
            _split_parts = split_node_into_chain(node)
            node.transcription_progress = 100
            node.transcription_error = None
            # No reply to an empty transcript: the entry would still hold
            # its "[Voice note – transcription pending]" placeholder.
            if auto_reply_model and (transcript or "").strip():
                # Commit the transcript before the reply is attempted, and
                # flip the status only after it: the frontend's first
                # 'completed' poll then finds the reply (#342).
                db.session.commit()
                tip = _split_parts[-1] if _split_parts else node
                _start_upload_reply(node, tip, auto_reply_model)
            node.transcription_status = 'completed'
            node.transcription_completed_at = datetime.utcnow()
            db.session.commit()

            logger.info(f"Transcription successful for node {node_id}: {len(transcript)} characters")

            return {
                'node_id': node_id,
                'status': 'completed',
                'transcript_length': len(transcript)
            }

        except Exception as e:
            logger.error(
                "Transcription error for node %s: %s", node_id,
                describe_exception(e), exc_info=True)
            node.transcription_status = 'failed'
            node.transcription_error = TRANSCRIPTION_FAILED_TEXT
            node.transcription_completed_at = datetime.utcnow()
            if not cost_logged:
                # Pieces sent before the failure.
                _log_transcription_cost(node.user_id, sent_seconds)
            db.session.commit()
            raise
