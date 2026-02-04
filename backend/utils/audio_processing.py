"""
Audio processing utilities for voice transcription.
Extracted from routes/nodes.py to be shared between API and Celery tasks.
"""
import os
import pathlib
import tempfile
import ffmpeg
from pydub import AudioSegment
from flask import current_app

# OpenAI API limits
OPENAI_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
OPENAI_MAX_DURATION_SEC = 1400  # ~23 minutes (OpenAI's actual limit)
CHUNK_DURATION_SEC = 20 * 60  # 20 minutes per chunk


def compress_audio_if_needed(file_path: pathlib.Path, logger=None) -> pathlib.Path:
    """
    Compress audio file to MP3 if it's uncompressed (WAV/FLAC).
    Never re-compress already compressed formats (MP3, M4A, WebM, etc.) as this causes quality loss.
    For large compressed files, skip compression and let chunking handle them.
    Returns path to compressed file, or original if no compression needed.
    """
    if logger is None:
        logger = current_app.logger

    file_size = file_path.stat().st_size
    ext = file_path.suffix.lower()

    # Only compress uncompressed formats - never re-compress lossy formats
    needs_compression = ext in {".wav", ".flac"}

    if not needs_compression:
        logger.info(f"Skipping compression for {file_path.name} ({ext}, {file_size / 1024 / 1024:.1f} MB) - already compressed or will be chunked")
        return file_path

    try:
        logger.info(f"Compressing uncompressed audio file {file_path.name} (size: {file_size / 1024 / 1024:.1f} MB)")

        audio = AudioSegment.from_file(str(file_path))
        compressed_path = file_path.with_suffix('.mp3')

        audio.export(
            str(compressed_path),
            format="mp3",
            bitrate="128k",
            parameters=["-q:a", "2"]
        )

        compressed_size = compressed_path.stat().st_size
        logger.info(
            f"Compressed {file_path.name}: {file_size / 1024 / 1024:.1f} MB -> "
            f"{compressed_size / 1024 / 1024:.1f} MB"
        )

        return compressed_path

    except Exception as e:
        logger.error(f"Audio compression failed: {e}")
        return file_path


def get_audio_duration(file_path: pathlib.Path, logger=None) -> float:
    """Get audio duration in seconds.

    Tries ffprobe directly first. If that fails (common with WebM files from
    browser MediaRecorder that lack duration metadata), remuxes to a temp file
    with ``ffmpeg -c copy`` and reads the duration from the remuxed output.
    This avoids the old pydub fallback which loaded the entire file into memory
    and could OOM-kill the Celery worker on large files.
    """
    if logger is None:
        logger = current_app.logger

    try:
        # Use ffprobe to get duration - much faster than pydub for large files
        probe = ffmpeg.probe(str(file_path))
        return float(probe['format']['duration'])
    except (ffmpeg.Error, KeyError, TypeError) as e:
        logger.warning(f"ffprobe failed to get duration directly: {e}")

    # Fallback: remux with ffmpeg (stream copy, no re-encoding) to produce
    # a file with correct duration metadata, then read it with ffprobe.
    # Same approach used in webm_utils.fix_webm_duration().
    from backend.utils.webm_utils import get_webm_duration
    try:
        fd, temp_path = tempfile.mkstemp(suffix=file_path.suffix)
        os.close(fd)
        try:
            import subprocess
            result = subprocess.run(
                ['ffmpeg', '-y', '-i', str(file_path), '-c', 'copy', temp_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                duration = get_webm_duration(temp_path)
                if duration is not None:
                    logger.info(f"Got duration via remux fallback: {duration:.1f}s")
                    return duration
            logger.error(f"Remux fallback failed: {result.stderr[:200] if result.stderr else 'unknown'}")
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except Exception as e_remux:
        logger.error(f"Remux fallback also failed: {e_remux}")

    return 0.0


def chunk_audio(file_path: pathlib.Path, chunk_duration_sec: int = CHUNK_DURATION_SEC, logger=None) -> list:
    """
    Split audio file into chunks using ffmpeg-python.
    This is memory-efficient and does not load the whole file.
    Returns list of temporary file paths.
    """
    if logger is None:
        logger = current_app.logger

    try:
        # Get total duration to calculate number of chunks
        total_duration = get_audio_duration(file_path, logger)
        if total_duration == 0:
            raise ValueError("Could not determine audio duration.")

        chunk_paths = []
        num_chunks = int(total_duration // chunk_duration_sec) + 1

        for i in range(num_chunks):
            start_time = i * chunk_duration_sec
            
            # Create a temporary file for the chunk
            temp_file = tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.mp3',
                prefix=f'chunk_{i}_'
            )
            temp_file.close()  # Close the file so ffmpeg can write to it
            
            try:
                (
                    ffmpeg
                    .input(str(file_path), ss=start_time, t=chunk_duration_sec)
                    .output(temp_file.name, acodec='libmp3lame', audio_bitrate='128k', format='mp3', q='2')
                    .overwrite_output()
                    .run(capture_stdout=True, capture_stderr=True, quiet=True)
                )
                chunk_paths.append(temp_file.name)
                logger.info(f"Created chunk {i + 1}/{num_chunks} at {temp_file.name}")

            except ffmpeg.Error as e:
                logger.error(f"FFmpeg error on chunk {i+1}: {e.stderr.decode('utf-8') if e.stderr else 'Unknown error'}")
                # Clean up failed chunk file
                if os.path.exists(temp_file.name):
                    os.unlink(temp_file.name)
                raise  # Re-raise the exception to fail the task

        return chunk_paths

    except Exception as e:
        logger.error(f"Audio chunking failed: {e}", exc_info=True)
        return []


def _split_at_sentence(text: str, max_chars: int) -> int:
    """
    Find the last sentence boundary within max_chars.
    Never splits mid-sentence. Returns the number of characters to include.

    Looks for sentence-ending punctuation (.!?) followed by whitespace or
    end-of-string. Always finds a sentence boundary — only falls back to
    max_chars if the entire text within the limit is a single sentence
    (no sentence boundary found at all).
    """
    if len(text) <= max_chars:
        return len(text)

    chunk = text[:max_chars]

    # Find the last sentence boundary: punctuation followed by space or newline
    # Search from the end backwards for the best split point
    best = -1
    for i in range(len(chunk) - 1, 0, -1):
        if chunk[i] in '.!?' and (i + 1 >= len(chunk) or chunk[i + 1] in ' \n\r\t'):
            best = i + 1  # Include the punctuation
            break

    if best > 0:
        # Skip any trailing whitespace after the punctuation
        while best < len(chunk) and chunk[best] in ' \n\r\t':
            best += 1
        return best

    # No sentence boundary found — this is one very long sentence.
    # Fall back to word boundary as last resort to avoid splitting a word.
    split_point = chunk.rfind(' ')
    if split_point > 0:
        return split_point

    return max_chars


def chunk_text(text: str, max_chars: int = 4096) -> list:
    """
    Split text into chunks for TTS generation.
    Always splits at sentence boundaries — never mid-sentence.
    max_chars is the TTS model's input limit (4096 for gpt-4o-mini-tts).
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        split_point = _split_at_sentence(remaining, max_chars)
        chunks.append(remaining[:split_point].strip())
        remaining = remaining[split_point:].strip()

    return chunks


# TTS model input limit
TTS_MAX_CHARS = 4096


def adaptive_chunk_text(text: str, first_chunk_gen_secs: float = 7.5) -> list:
    """
    Split text into chunks for streaming TTS with a small first chunk.

    The first chunk is small enough to generate quickly so playback starts
    fast. All subsequent chunks use the full model limit (4096 chars) to
    minimize the number of stitches.

    Based on benchmarked rates for gpt-4o-mini-tts:
      - Generation: ~106 chars/s pure API (~4s fixed overhead for encryption/DB/SSE)
      - Audio output: ~0.062s of audio per char
      - First chunk of ~800 chars: ~7.5s API + ~4s overhead = ~12s total,
        produces ~50s of audio
      - Second chunk of 4096 chars: ~39s to generate — fits within the
        50s the first chunk plays

    All splits happen at sentence boundaries — never mid-sentence.

    Args:
        text: Full text to split
        first_chunk_gen_secs: Target API generation time for the first chunk (seconds).
                              Does not include fixed overhead (~4s for encryption/DB/SSE).
    """
    gen_chars_per_sec = 106.0  # benchmarked for gpt-4o-mini-tts

    # First chunk: sized to generate within target time, capped at model limit
    first_chunk_chars = min(int(first_chunk_gen_secs * gen_chars_per_sec),
                           TTS_MAX_CHARS)  # ~795

    if len(text) <= first_chunk_chars:
        return [text]

    chunks = []
    remaining = text

    # First chunk: small for quick start
    split_point = _split_at_sentence(remaining, first_chunk_chars)
    chunks.append(remaining[:split_point].strip())
    remaining = remaining[split_point:].strip()

    # Remaining chunks: use full model limit
    while remaining:
        if len(remaining) <= TTS_MAX_CHARS:
            chunks.append(remaining)
            break

        split_point = _split_at_sentence(remaining, TTS_MAX_CHARS)
        chunks.append(remaining[:split_point].strip())
        remaining = remaining[split_point:].strip()

    return chunks
