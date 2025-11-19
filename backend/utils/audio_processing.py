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
    """Get audio duration in seconds."""
    if logger is None:
        logger = current_app.logger

    try:
        # Use ffprobe to get duration - much faster than pydub for large files
        probe = ffmpeg.probe(str(file_path))
        return float(probe['format']['duration'])
    except (ffmpeg.Error, KeyError, TypeError) as e:
        logger.error(f"Failed to get audio duration with ffprobe: {e}")
        # Fallback to pydub for safety
        try:
            audio = AudioSegment.from_file(str(file_path))
            return len(audio) / 1000.0
        except Exception as e_pydub:
            logger.error(f"Pydub fallback also failed: {e_pydub}")
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
