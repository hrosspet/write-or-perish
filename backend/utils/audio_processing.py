"""
Audio processing utilities for voice transcription.
Extracted from routes/nodes.py to be shared between API and Celery tasks.
"""
import os
import pathlib
import tempfile
from pydub import AudioSegment
from flask import current_app

# OpenAI API limits
OPENAI_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
OPENAI_MAX_DURATION_SEC = 1500  # 25 minutes
CHUNK_DURATION_SEC = 20 * 60  # 20 minutes per chunk


def compress_audio_if_needed(file_path: pathlib.Path, logger=None) -> pathlib.Path:
    """
    Compress audio file to MP3 if it's uncompressed or exceeds size limit.
    Returns path to compressed file, or original if no compression needed.
    """
    if logger is None:
        logger = current_app.logger

    file_size = file_path.stat().st_size
    ext = file_path.suffix.lower()

    needs_compression = ext in {".wav", ".flac"} or file_size > OPENAI_MAX_AUDIO_BYTES

    if not needs_compression:
        return file_path

    try:
        logger.info(f"Compressing audio file {file_path.name} (size: {file_size / 1024 / 1024:.1f} MB)")

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
    """Get audio duration in seconds."""
    if logger is None:
        logger = current_app.logger

    try:
        audio = AudioSegment.from_file(str(file_path))
        return len(audio) / 1000.0
    except Exception as e:
        logger.error(f"Failed to get audio duration: {e}")
        return 0.0


def chunk_audio(file_path: pathlib.Path, chunk_duration_sec: int = CHUNK_DURATION_SEC, logger=None) -> list:
    """
    Split audio file into chunks.
    Returns list of temporary file paths.
    """
    if logger is None:
        logger = current_app.logger

    try:
        audio = AudioSegment.from_file(str(file_path))
        chunk_duration_ms = chunk_duration_sec * 1000
        chunks = []

        for i, start_ms in enumerate(range(0, len(audio), chunk_duration_ms)):
            chunk = audio[start_ms:start_ms + chunk_duration_ms]

            temp_file = tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.mp3',
                prefix=f'chunk_{i}_'
            )
            chunk.export(temp_file.name, format="mp3", bitrate="128k")
            chunks.append(temp_file.name)

            logger.info(
                f"Created chunk {i + 1}: {start_ms / 1000:.0f}s - {(start_ms + len(chunk)) / 1000:.0f}s"
            )

        return chunks

    except Exception as e:
        logger.error(f"Audio chunking failed: {e}")
        return []


def chunk_text(text: str, max_chars: int = 4096) -> list:
    """
    Split text into chunks for TTS generation.
    Tries to split on sentence boundaries when possible.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        # Try to find a sentence boundary within the limit
        chunk = remaining[:max_chars]

        # Look for sentence endings (period, question mark, exclamation)
        last_period = max(chunk.rfind('. '), chunk.rfind('.\n'))
        last_question = max(chunk.rfind('? '), chunk.rfind('?\n'))
        last_exclaim = max(chunk.rfind('! '), chunk.rfind('!\n'))

        split_point = max(last_period, last_question, last_exclaim)

        if split_point > max_chars * 0.5:  # Only split on sentence if it's not too early
            split_point += 2  # Include the punctuation and space/newline
        else:
            # Fall back to word boundary
            split_point = chunk.rfind(' ')
            if split_point == -1:
                split_point = max_chars

        chunks.append(remaining[:split_point].strip())
        remaining = remaining[split_point:].strip()

    return chunks
