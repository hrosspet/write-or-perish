"""
Celery task for asynchronous TTS generation.

Supports streaming playback - each chunk's audio URL is stored in TTSChunk
and can be played as soon as it's ready, without waiting for all chunks.
"""
import hashlib
import hmac
import io
import json
import re
import time
import httpx
import openai
import sentry_sdk
from celery import Task
from celery.utils.log import get_task_logger
from flask import current_app
from openai import OpenAI
from pathlib import Path
from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError
import os
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node, UserProfile, ExternalItem, TTSChunk, APICostLog
from backend.extensions import db
from backend.utils.audio_processing import section_aware_chunk_text
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.encryption import decrypt_file, encrypt_file
from backend.utils.cost import calculate_audio_cost_microdollars
from backend.utils.spoken_links import speak_links

logger = get_task_logger(__name__)


def _strip_quote_markers(text):
    """Remove {quote:ID} / {quote_ext:ID} markers from TTS input — they
    render as quote cards visually; spoken aloud they'd be read as literal
    brace-noise. The surrounding prose already introduces the quote."""
    text = re.sub(r'[ \t]*\{quote(?:_ext)?:\d+\}[ \t]*', ' ', text)
    # Collapse the whitespace the removal leaves behind.
    text = re.sub(r' {2,}', ' ', text)
    return re.sub(r'[ \t]+\n', '\n', text).strip()


_SHARE_BLOCK_RE = re.compile(
    r'^:::share(?:[ \t]+\w+)?[ \t]*\r?\n.*?(?:^:::[ \t]*$\n?|\Z)',
    flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)


def _strip_share_blocks(text):
    """Drop fenced :::share blocks: the shareable piece is shown as a card,
    never spoken; the surrounding prose already introduces it. Applies to
    every reply, not only ones with a todo/issue/feedback proposal (#367:
    a reply whose only proposal was a share used to be read out fence and
    all)."""
    return _SHARE_BLOCK_RE.sub('', text).strip()


def _strip_heading_sections(text):
    """Extract spoken parts from a structured Voice response.

    Voice tool-use responses follow a fixed structure: an intro
    sentence, then proposal sections (### Completed / ### New Tasks /
    ### Priority Order / ### Note for todo, ### Issue Title / ### Description /
    ### Category for issues, ### Feedback / ### Feedback category for feedback,
    fenced :::share blocks for shares — legacy ### Share / ### Share type
    headings still occur in old nodes).
    TTS reads the prose — the intro (before the first ### heading), the ### Note
    body, and any trailing commentary the model appends below the structured
    block (after a single-line Category / Feedback category value). The
    structured lists/values are shown visually but not spoken.
    """
    # Drop fenced :::share blocks first — the shareable piece is shown
    # visually, not spoken; the surrounding prose already introduces it.
    text = _strip_share_blocks(text)

    # Extract intro text before the first ### heading
    first_heading = re.search(r'^###\s+', text, flags=re.MULTILINE)
    intro = text[:first_heading.start()].strip() if first_heading else ""

    # Extract the ### Note section body
    note_match = re.search(
        r'^###\s+Note\s*\n(.*)',
        text, flags=re.MULTILINE | re.DOTALL
    )
    note = note_match.group(1).strip() if note_match else ""

    # Extract trailing commentary the model appends after the structured block,
    # below a single-line (Feedback) Category value, up to the next ### or EOF.
    trailing_match = re.search(
        r'^###\s+(?:(?:feedback\s+)?category|share\s+type)\s*\n[^\n]*(.*?)(?=^###\s|\Z)',
        text, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE
    )
    trailing = trailing_match.group(1).strip() if trailing_match else ""

    parts = [p for p in [intro, note, trailing] if p]
    if parts:
        return "\n\n".join(parts)
    # Fallback: return full text if no structure detected
    return text.strip()


_PROPOSAL_TOOLS = {'propose_todo', 'propose_github_issue', 'propose_feedback'}


def _node_spoken_text(content, tool_calls_meta=None):
    """The text TTS speaks for a node: quote markers and share blocks
    removed; for a Voice tool-use reply with a proposal card, only its
    prose (``_strip_heading_sections``: otherwise the heading words get
    read aloud); then links spoken as their text and addresses short
    (#461). The stored text is unchanged. A streamed reply's TTS does the
    same in tts_stream_text.SpokenTextProjector."""
    text = _strip_share_blocks(_strip_quote_markers(content or ""))
    if text and tool_calls_meta:
        try:
            tool_names = {m.get('name') for m in json.loads(tool_calls_meta)}
        except (json.JSONDecodeError, TypeError):
            tool_names = set()
        if tool_names & _PROPOSAL_TOOLS:
            text = _strip_heading_sections(text)
    return speak_links(text).strip()


# Silence appended to a chapter's final audio chunk (#145 v3): the
# audible breath between a chapter's last sentence and the next spoken
# title. Belongs to the ENDING chapter so chapter start_times (computed
# from per-chunk durations) stay exact.
CHAPTER_END_SILENCE_MS = 900

TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"

# OpenAI's TTS MP3 is 128 kbps CBR: bytes received = seconds of audio
# received (measured on prod and local chunk files).
TTS_MP3_BYTES_PER_SEC = 16000

# A call still streaming at this point has hit OpenAI's cutoff: since
# 2026-09-27 slow calls end at 300 s with HTTP 200 and the audio cut
# mid-word. Such audio is never stored as if complete.
TTS_CUTOFF_SECS = 290

# Temporary, #382: while OpenAI is slow, a call that is still running
# after TTS_SLOW_CHECK_SECS and has delivered less audio than time
# elapsed (normal calls deliver 3-7x realtime, slow ones 0.08-0.33x), or
# that sends nothing for TTS_SLOW_CHECK_SECS, is cancelled and the same
# text sent again, up to TTS_SLOW_RETRIES more times.
TTS_SLOW_CHECK_SECS = 30
TTS_SLOW_MIN_REALTIME = 1.0
TTS_SLOW_RETRIES = 2

# Temporary, #382: the SDK's own retries are off for the TTS call, so
# each attempt is one request timed from its start; the loop in
# synthesize_to_file resends what the SDK would have retried.
_TTS_RETRIED_SDK_ERRORS = (openai.APIConnectionError,  # incl. timeouts
                           openai.InternalServerError,
                           openai.RateLimitError)

_now = time.monotonic


class TTSCutOff(Exception):
    """The call ran to OpenAI's cutoff, so its audio is incomplete."""


class TTSSlowCall(Exception):
    """Temporary, #382: the call was cancelled for being too slow."""


class TTSCallFailed(Exception):
    """Temporary, #382: every attempt at a TTS call failed."""


class TTSBadAudio(Exception):
    """The call answered, but not with playable audio: an empty body, or
    one that does not decode as MP3 (e.g. an error message sent with
    HTTP 200). Resent like a slow call; unlike the slow-call handling it
    is not temporary, so it stays when #382's workaround goes."""


def _stream_speech(client, text, path, mark=None):
    """One TTS call streamed to *path*; returns its decoded audio. Raises
    TTSCutOff, TTSSlowCall or TTSBadAudio instead of leaving incomplete or
    unplayable audio as the result."""
    started = _now()
    received = 0
    with client.with_options(max_retries=0).audio.speech \
            .with_streaming_response.create(
        model=TTS_MODEL, input=text, voice=TTS_VOICE,
        timeout=httpx.Timeout(TTS_SLOW_CHECK_SECS, connect=5.0),
    ) as resp:
        request_id = resp.headers.get("x-request-id")
        content_type = resp.headers.get("content-type")
        try:
            with open(path, "wb") as f:
                for data in resp.iter_bytes():
                    if mark is not None and received == 0:
                        mark("tts_first_byte")
                    f.write(data)
                    received += len(data)
                    elapsed = _now() - started
                    if (elapsed >= TTS_SLOW_CHECK_SECS
                            and received / TTS_MP3_BYTES_PER_SEC
                            < elapsed * TTS_SLOW_MIN_REALTIME):
                        raise TTSSlowCall(
                            f"request {request_id}: "
                            f"{received / TTS_MP3_BYTES_PER_SEC:.1f} s of "
                            f"audio in {elapsed:.0f} s")
        except httpx.TimeoutException:
            raise TTSSlowCall(
                f"request {request_id}: nothing received for "
                f"{TTS_SLOW_CHECK_SECS} s")
    elapsed = _now() - started
    logger.info("TTS call %s: %s chars, %.1f s of audio in %.1f s",
                request_id, len(text), received / TTS_MP3_BYTES_PER_SEC,
                elapsed)
    if elapsed >= TTS_CUTOFF_SECS:
        raise TTSCutOff(
            f"request {request_id} ran {elapsed:.0f} s; audio is cut")
    if received == 0:
        raise TTSBadAudio(
            f"request {request_id}: empty body ({content_type})")
    if mark is not None:
        mark("tts_last_byte")
    return _decode_mp3(path, request_id, received, content_type)


def _decode_mp3(path, request_id, received, content_type):
    """The answer saved at *path* as audio; TTSBadAudio when it isn't MP3.
    Any other decoder failure (e.g. no ffmpeg) is raised as it is: a
    resend would fail the same way."""
    try:
        return AudioSegment.from_file(str(path), format="mp3")
    except CouldntDecodeError as e:
        raise TTSBadAudio(
            f"request {request_id}: {received} bytes ({content_type}) "
            f"that do not decode as MP3") from e


def synthesize_to_file(client, text, path, section_end=False, mark=None):
    """One TTS call written to *path* as MP3. Returns the AudioSegment; a
    chunk that closes a chapter gets the chapter-end silence, re-exported
    so chunked playback (which streams the file directly) has it too.
    *mark(stage)*, when given, is called at the first and the last byte
    of the audio (#371 timing).

    A call that is too slow, ran to OpenAI's cutoff, failed with an error
    the SDK would retry (#382), or answered without playable audio (an
    empty body, or one that doesn't decode as MP3) is sent again, to the
    same model; when every attempt fails, the file is removed and
    TTSCallFailed raised, so the chunk fails rather than playing cut or
    broken audio. Only slow calls are reported to Sentry: that count
    tells #382 when OpenAI is fast again. The warning for a failed
    attempt names the request id and the byte count, never the text."""
    attempts = TTS_SLOW_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            segment = _stream_speech(client, text, path, mark=mark)
            break
        except ((TTSSlowCall, TTSCutOff, TTSBadAudio)
                + _TTS_RETRIED_SDK_ERRORS) as e:
            if attempt == attempts:
                Path(path).unlink(missing_ok=True)
                raise TTSCallFailed(
                    f"TTS call failed after {attempts} attempts; "
                    f"last: {e!r}") from e
            slow = isinstance(
                e, (TTSSlowCall, TTSCutOff, openai.APITimeoutError))
            logger.warning("%s (attempt %s of %s, %s chars): %r",
                           "Slow TTS call cancelled" if slow
                           else "TTS call failed, resending",
                           attempt, attempts, len(text), e)
            if slow:
                sentry_sdk.capture_message("Slow TTS call cancelled",
                                           level="warning")
    if section_end:
        segment = segment + AudioSegment.silent(
            duration=CHAPTER_END_SILENCE_MS)
        segment.export(str(path), format="mp3")
    return segment


def log_tts_cost(user_id, seconds):
    """Record the cost of *seconds* of audio made by one TTS call, and
    commit it at once: the call is billed whether or not the rest of the
    job succeeds, and a row left pending would go with a later rollback
    (#526). One row per chunk; every reader sums cost_microdollars."""
    if seconds <= 0:
        return
    db.session.add(APICostLog(
        user_id=user_id,
        model_id=TTS_MODEL,
        request_type="tts",
        audio_duration_seconds=seconds,
        cost_microdollars=calculate_audio_cost_microdollars(
            TTS_MODEL, seconds),
    ))
    db.session.commit()


def tts_chunk_digest(text, section_end):
    """What a chunk's audio is made from, as an HMAC-SHA256 keyed with the
    app's SECRET_KEY: the text, the model, the voice and whether the
    chapter-end pause is appended. An equal digest means the same request,
    so a retry may keep that chunk's audio (#526). Keyed, so the stored
    value does not stand for the text by itself (as the PDF import's
    duplicate key, #529). None when no secret is set: nothing is kept."""
    secret = current_app.config.get("SECRET_KEY")
    if not secret:
        return None
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = "\x00".join(["tts-chunk", TTS_MODEL, TTS_VOICE,
                       "chapter-end" if section_end else "", text])
    return hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _stored_part(path):
    """The file holding a chunk's audio written to *path*: the plain file
    (left when its encryption failed; encrypted now, as a new part is) or
    its .enc sibling. None when neither exists."""
    if path.is_file():
        return Path(encrypt_file(str(path)))
    enc = path.with_name(path.name + ".enc")
    return enc if enc.is_file() else None


def _kept_parts(rows, digests, part_path, audio_root, entity_label):
    """The audio an earlier run already made for this text (#526), as the
    segments of chunks 0..k-1: the longest run from the first chunk whose
    rows are completed, were made from the same input (*digests*) and
    name the file this run would write. A run that stopped at a failed
    chunk resumes there, so no chunk is made, or paid for, twice. A part
    that can't be read back is made again, and so is everything after it.
    *rows* maps chunk index to the entity's TTSChunk row."""
    kept = []
    for i, digest in enumerate(digests):
        row = rows.get(i)
        path = part_path(i)
        url_path = f"/media/{path.relative_to(audio_root).as_posix()}"
        if (row is None or row.status != 'completed' or digest is None
                or row.text_digest != digest
                or (row.audio_url or "").split("?", 1)[0] != url_path):
            break
        stored = _stored_part(path)
        if stored is None:
            break
        try:
            kept.append(AudioSegment.from_file(
                io.BytesIO(decrypt_file(str(stored))), format="mp3"))
        except Exception as e:
            logger.warning("TTS for %s: chunk %s's audio could not be read "
                           "back, so it is made again (%s)",
                           entity_label, i, type(e).__name__)
            break
    return kept


def _prepare_chunk_rows(chunk_fk, chunk_specs, digests, part_path,
                        audio_root, entity_label):
    """The entity's TTSChunk records for streaming playback (with chapter
    metadata, #145): the chunks an earlier run of this text made are kept
    (#526), and a pending row is added for each other chunk. Every other
    row goes, since it would collide on the chunk index: rows of a run
    for other text, the rest of a run that failed, or a streamed voice
    reply's TTS (#367), whose chunks are cut differently.

    Returns the kept chunks' audio segments and the cache-busting token
    for this run's /media URLs."""
    rows = {r.chunk_index: r for r in TTSChunk.query.filter_by(**chunk_fk)}
    kept = _kept_parts(rows, digests, part_path, audio_root, entity_label)
    keep = len(kept)
    for i, row in rows.items():
        if i >= keep:
            db.session.expunge(row)
    TTSChunk.query.filter_by(**chunk_fk).filter(
        TTSChunk.chunk_index >= keep).delete(synchronize_session=False)
    for i in range(keep):
        rows[i].section_title = chunk_specs[i][1]
        rows[i].section_index = chunk_specs[i][2]
    created = []
    for i in range(keep, len(chunk_specs)):
        _chunk, section_title, section_index = chunk_specs[i]
        tts_chunk = TTSChunk(
            chunk_index=i, status='pending',
            section_index=section_index,
            section_title=section_title,
            text_digest=digests[i],
            **chunk_fk)
        db.session.add(tts_chunk)
        created.append(tts_chunk)
    db.session.commit()
    if keep:
        logger.info(f"Keeping {keep} of {len(chunk_specs)} TTS chunks an "
                    f"earlier run made for {entity_label}")

    # Cache-busting token for the emitted /media URLs. The media path is
    # fixed per entity (…/node/<id>/tts.mp3) and nginx serves /media with a
    # 24h Cache-Control, so after a regenerate the browser would replay the
    # OLD cached file at the identical URL — i.e. the pre-edit text (#66).
    # The first row this run inserts has an id unique to this run, so a
    # `?v=<id>` suffix makes every regeneration a distinct URL. Kept rows
    # keep their URLs: their files don't change. When every chunk is kept,
    # the newest kept row's id serves: the run that inserted it stopped
    # before tts.mp3 was made (a finished run's rows go only with its
    # audio, utils/audio_storage), so no tts.mp3 URL carried it, except a
    # single chunk's own URL, which names the same file.
    if created:
        return kept, created[0].id
    return kept, max((rows[i].id for i in range(keep)), default=None)


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


class EntityTTSTask(Task):
    """Task base for entities other than nodes: marks the row failed when
    the task dies. Subclasses name the model."""
    entity_cls = None
    entity_label = "entity"

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        entity_id = args[0] if args else None
        if entity_id:
            with flask_app.app_context():
                entity = self.entity_cls.query.get(entity_id)
                if entity:
                    entity.tts_task_status = 'failed'
                    db.session.commit()
                    logger.error(
                        f"TTS generation failed for {self.entity_label} "
                        f"{entity_id}: {exc}")


class ProfileTTSTask(EntityTTSTask):
    entity_cls = UserProfile
    entity_label = "profile"


class ItemTTSTask(EntityTTSTask):
    entity_cls = ExternalItem
    entity_label = "reference"


def _hidden_for_deletion(kind, row_id):
    """A waiting "Delete all my writing" hid this row (#268)."""
    from backend.models import UserDataPurgeHidden
    return db.session.query(UserDataPurgeHidden.id).filter(
        UserDataPurgeHidden.kind == kind,
        UserDataPurgeHidden.row_id == row_id).first() is not None


def _run_entity_tts(task, entity_cls, entity_id, text_of, subdir,
                    chunk_fk_attr, label, audio_storage_root,
                    requesting_user_id):
    """The whole TTS task for an entity that has no original recording
    (profiles, saved references): spend gate, status bookkeeping, the
    shared chunk generator, and failure marking. ``text_of(entity)``
    returns the text to speak; ``subdir(entity)`` the path under
    ``user/<uid>/``."""
    logger.info(f"Starting TTS generation task for {label} {entity_id}")
    with flask_app.app_context():
        entity = entity_cls.query.get(entity_id)
        if not entity and _hidden_for_deletion(entity_cls.__tablename__,
                                               entity_id):
            # "Delete all my writing" hid it after the TTS was queued
            # (#268): nothing is spoken. After a restore it offers its
            # speaker again, as after any failed TTS.
            logger.info(f"{label} {entity_id} is hidden for deletion; no TTS")
            from backend.utils.hidden_rows import including_hidden_rows
            with including_hidden_rows():
                hidden = entity_cls.query.get(entity_id)
                if hidden is not None:
                    hidden.tts_task_status = 'failed'
                    db.session.commit()
            return None
        if not entity:
            raise ValueError(f"{entity_cls.__name__} {entity_id} not found")

        from backend.utils.spend import user_is_capped
        if user_is_capped(requesting_user_id or entity.user_id):
            logger.warning(
                "User %s is spend-capped; skipping %s TTS",
                requesting_user_id or entity.user_id, label)
            entity.tts_task_status = 'failed'
            db.session.commit()
            return

        # Checked when the job runs, as for nodes (a saved reference has
        # no ai_usage and always passes).
        from backend.utils.privacy import speech_allowed
        if not entity.audio_tts_url and not speech_allowed(entity):
            logger.info("%s %s: ai_usage is none; no speech", label, entity_id)
            entity.tts_task_status = 'failed'
            db.session.commit()
            return {'status': 'refused'}

        entity.tts_task_status = 'processing'
        entity.tts_task_progress = 10
        db.session.commit()

        try:
            if entity.audio_tts_url:
                logger.info(f"TTS already available for {label} {entity_id}")
                entity.tts_task_status = 'completed'
                entity.tts_task_progress = 100
                db.session.commit()
                return {'status': 'completed', 'tts_url': entity.audio_tts_url}

            # Links spoken as their text, addresses short (#461): the same
            # step as for nodes. The stored text is unchanged.
            text = speak_links(text_of(entity) or "").strip()
            if not text:
                raise ValueError("No content to generate TTS for")

            target_dir = (Path(audio_storage_root)
                          / f"user/{entity.user_id}/{subdir(entity)}")
            url = _generate_tts_chunks(
                task, entity, text, target_dir, audio_storage_root,
                chunk_fk_attr, f"{label} {entity_id}",
                requesting_user_id=requesting_user_id)

            entity.audio_tts_url = url
            entity.tts_task_status = 'completed'
            entity.tts_task_progress = 100
            db.session.commit()
            logger.info(f"TTS generation successful for {label} {entity_id}")
            return {'status': 'completed', 'tts_url': url}

        except Exception as e:
            logger.error(
                f"TTS generation error for {label} {entity_id}: {e}",
                exc_info=True)
            entity.tts_task_status = 'failed'
            db.session.commit()
            raise


def _generate_tts_chunks(task, entity, text, target_dir, audio_storage_root,
                         chunk_fk_attr, entity_label,
                         requesting_user_id=None):
    """
    Shared TTS generation logic for both nodes and profiles.

    Handles text chunking, TTSChunk record creation, audio generation,
    encryption, concatenation, and cost logging (one row per chunk, as
    each is made). Chunks an earlier run of the same text already made
    are kept, not made again (#526).

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

    chunk_specs = section_aware_chunk_text(text)
    chunks = [c for c, _title, _idx in chunk_specs]
    logger.info(
        f"Text split into {len(chunks)} chunks for TTS "
        f"for {entity_label} (sizes: {[len(c) for c in chunks]}, "
        f"sections: {len({s for _, _, s in chunk_specs})})"
    )

    # Chunks that close a chapter (a different section follows) get
    # trailing silence appended (#145 v3).
    section_end_indices = {
        i for i in range(len(chunk_specs) - 1)
        if chunk_specs[i][2] != chunk_specs[i + 1][2]
    }
    single = len(chunks) == 1

    def part_path(i):
        # A single chunk is written straight to the final file.
        return final_path if single else target_dir / f"tts_chunk_{i}.mp3"

    digests = [tts_chunk_digest(chunk, i in section_end_indices)
               for i, chunk in enumerate(chunks)]
    cost_user_id = requesting_user_id or entity.user_id

    chunk_fk = {chunk_fk_attr: entity.id}
    kept_segments, cache_bust = _prepare_chunk_rows(
        chunk_fk, chunk_specs, digests, part_path, AUDIO_ROOT, entity_label)
    keep = len(kept_segments)
    _bust = (lambda url: f"{url}?v={cache_bust}") if cache_bust else (lambda url: url)

    if single and keep:
        logger.info(f"TTS audio for {entity_label} was already made")

    elif single:
        # Single chunk: direct streaming to MP3
        task.update_state(
            state='PROGRESS',
            meta={'progress': 40, 'status': 'Generating audio'}
        )
        entity.tts_task_progress = 40
        db.session.commit()

        segment = synthesize_to_file(client, chunks[0], final_path)
        chunk_duration = len(segment) / 1000.0
        log_tts_cost(cost_user_id, chunk_duration)
        encrypt_file(str(final_path))

        tts_chunk = TTSChunk.query.filter_by(
            chunk_index=0, **chunk_fk
        ).first()
        if tts_chunk:
            rel_path = final_path.relative_to(AUDIO_ROOT)
            tts_chunk.audio_url = _bust(f"/media/{rel_path.as_posix()}")
            tts_chunk.duration = chunk_duration
            tts_chunk.status = 'completed'
            tts_chunk.completed_at = datetime.utcnow()
            db.session.commit()

    else:
        # Multiple chunks: generate parts for streaming playback, after
        # the kept ones
        segments = list(kept_segments)
        chunk_progress_step = 50 / len(chunks)

        for i, chunk in enumerate(chunks):
            if i < keep:
                continue
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

            path = part_path(i)

            segment = synthesize_to_file(
                client, chunk, path,
                section_end=i in section_end_indices)
            chunk_duration = len(segment) / 1000.0
            log_tts_cost(cost_user_id, chunk_duration)
            segments.append(segment)

            if tts_chunk:
                rel_path = path.relative_to(AUDIO_ROOT)
                tts_chunk.audio_url = _bust(f"/media/{rel_path.as_posix()}")
                tts_chunk.duration = chunk_duration
                tts_chunk.status = 'completed'
                tts_chunk.completed_at = datetime.utcnow()
                db.session.commit()

            encrypt_file(str(path))

        # Concatenate all segments into final file
        task.update_state(
            state='PROGRESS',
            meta={'progress': 90, 'status': 'Combining audio'}
        )
        entity.tts_task_progress = 90
        db.session.commit()

        combined = sum(segments)
        combined.export(final_path, format="mp3")
        encrypt_file(str(final_path))

    # Finalize
    task.update_state(
        state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'}
    )
    entity.tts_task_progress = 95
    db.session.commit()

    rel_path = final_path.relative_to(AUDIO_ROOT)
    return _bust(f"/media/{rel_path.as_posix()}")


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
        if _hidden_for_deletion("node", node_id):
            # "Delete all my writing" hid it after the TTS was queued
            # (#268): nothing is spoken. After a restore the node offers
            # its speaker again, as after any failed TTS.
            logger.info("Node %s is hidden for deletion; no TTS", node_id)
            node.tts_task_status = 'failed'
            db.session.commit()
            return {'node_id': node_id, 'status': 'on_hold'}

        from backend.utils.spend import user_is_capped
        if user_is_capped(requesting_user_id or node.user_id):
            logger.warning(
                "User %s is spend-capped; skipping node TTS",
                requesting_user_id or node.user_id)
            node.tts_task_status = 'failed'
            db.session.commit()
            return

        # Checked when the job runs: the node's ai_usage may have changed
        # since it was queued, and not every caller goes through the route.
        from backend.utils.privacy import speech_allowed
        if not node.audio_tts_url and not speech_allowed(node):
            logger.info("Node %s: ai_usage is none; no speech", node_id)
            node.tts_task_status = 'failed'
            db.session.commit()
            return {'node_id': node_id, 'status': 'refused'}

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

            text = _node_spoken_text(node.get_content(), node.tool_calls_meta)
            if not text:
                logger.info(f"No text to speak for node {node_id}, skipping TTS")
                node.tts_task_status = 'completed'
                node.tts_task_progress = 100
                db.session.commit()
                return {
                    'node_id': node_id,
                    'status': 'completed',
                    'tts_url': None,
                    'skipped': True,
                }

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
    """Asynchronously generate TTS audio for a user profile."""
    result = _run_entity_tts(
        self, UserProfile, profile_id,
        text_of=lambda p: p.get_content() or "",
        subdir=lambda p: f"profile/{p.id}",
        chunk_fk_attr='profile_id', label="profile",
        audio_storage_root=audio_storage_root,
        requesting_user_id=requesting_user_id)
    return {'profile_id': profile_id, **result} if result else None


@celery.task(base=ItemTTSTask, bind=True)
def generate_tts_audio_for_item(self, item_id: int, audio_storage_root: str,
                                requesting_user_id: int = None):
    """Generate TTS for a saved reference (#232), read as title then text."""
    def text_of(item):
        text = (item.get_content() or "").strip()
        title = (item.title or "").strip()
        if title and not text.lower().startswith(title.lower()):
            # Speak the title first unless the text already opens with it
            # (Readability keeps it as the first heading).
            text = f"# {title}\n\n{text}"
        return text

    result = _run_entity_tts(
        self, ExternalItem, item_id, text_of=text_of,
        subdir=lambda i: f"item/{i.id}",
        chunk_fk_attr='item_id', label="reference",
        audio_storage_root=audio_storage_root,
        requesting_user_id=requesting_user_id)
    return {'item_id': item_id, **result} if result else None
