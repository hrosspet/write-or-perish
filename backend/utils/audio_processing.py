"""
Audio processing utilities for voice transcription.
Extracted from routes/nodes.py to be shared between API and Celery tasks.
"""
import math
import os
import pathlib
import re
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


def adaptive_chunk_text(text: str, first_chunk_gen_secs: float = 3.0) -> list:
    """
    Split text into chunks for streaming TTS with a small first chunk.

    The first chunk is small enough to generate quickly so playback starts
    fast.  Subsequent chunks are sized so each one generates within the
    playback time of the previous chunk (gapless streaming).

    Based on benchmarked rates for gpt-4o-mini-tts:
      - Generation: ~106 chars/s pure API (~2s fixed overhead for
        encryption/DB/SSE)
      - Audio output: ~0.062s of audio per char
      - First chunk of ~318 chars: ~3s API + ~2s overhead = ~5s total,
        produces ~20s of audio
      - Second chunk must generate within ~20s of first-chunk playback,
        so capped at ~1908 chars (~118s of audio)
      - Third+ chunks generate within the ~118s window of the second
        chunk, so they can safely use the full model limit (4096 chars)

    All splits happen at sentence boundaries — never mid-sentence.

    Args:
        text: Full text to split
        first_chunk_gen_secs: Target API generation time for the first chunk (seconds).
                              Does not include fixed overhead (~2s for encryption/DB/SSE).
    """
    gen_chars_per_sec = 106.0   # benchmarked for gpt-4o-mini-tts
    audio_secs_per_char = 0.062  # benchmarked audio output rate
    overhead_secs = 2.0          # encryption + DB + SSE poll

    # First chunk: sized to generate within target time, capped at model limit
    first_chunk_chars = min(
        int(first_chunk_gen_secs * gen_chars_per_sec), TTS_MAX_CHARS
    )  # ~318

    if len(text) <= first_chunk_chars:
        return [text]

    # Second chunk: must generate within the first chunk's playback time
    first_play_secs = first_chunk_chars * audio_secs_per_char
    next_budget_secs = max(first_play_secs - overhead_secs, 2.0)
    second_chunk_chars = min(
        int(next_budget_secs * gen_chars_per_sec), TTS_MAX_CHARS
    )  # ~1908

    chunks = []
    remaining = text

    # First chunk: small for quick start
    split_point = _split_at_sentence(remaining, first_chunk_chars)
    chunks.append(remaining[:split_point].strip())
    remaining = remaining[split_point:].strip()

    if not remaining:
        return chunks

    # Second chunk: intermediate size for gapless transition
    if len(remaining) <= second_chunk_chars:
        chunks.append(remaining)
        return chunks

    split_point = _split_at_sentence(remaining, second_chunk_chars)
    chunks.append(remaining[:split_point].strip())
    remaining = remaining[split_point:].strip()

    # Remaining chunks: full model limit (they generate well within
    # the second chunk's ~118s playback window)
    while remaining:
        if len(remaining) <= TTS_MAX_CHARS:
            chunks.append(remaining)
            break

        split_point = _split_at_sentence(remaining, TTS_MAX_CHARS)
        chunks.append(remaining[:split_point].strip())
        remaining = remaining[split_point:].strip()

    return chunks


# Streaming TTS pipeline model (#140). Generation rate is the OpenAI API
# streaming rate; overhead is the per-chunk fixed cost AROUND the API call:
# pydub decode of the returned MP3, section-end silence re-export, DB
# status commits, encryption, and the SSE poll interval. Calibrated from
# live staging timings 2026-06-10 (solved from chunk-ready wall times:
# rate ≈ 104 chars/s, fixed ≈ 7.3s — the old 2.0s only modeled the API).
TTS_GEN_CHARS_PER_SEC = 106.0
TTS_AUDIO_SECS_PER_CHAR = 0.062
TTS_CHUNK_OVERHEAD_SECS = 7.0

# Minimum sensible first chunk (#140): a tiny first sentence ("Okay.")
# produces a sub-second clip — an audible stitch artifact and a wasted
# API round-trip. Below this size we split word-aware at the target
# instead of at the sentence boundary.
MIN_FIRST_CHUNK_CHARS = 80

_CHAPTER_HEADING_RE = re.compile(r'^(#{1,2})\s+(.+?)\s*$', re.MULTILINE)
_ANY_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*$', re.MULTILINE)

# Heuristic: a heading level only counts as the text's chapter level when
# it appears at least this many times (a lone heading is a title, not a
# chapter structure).
MIN_CHAPTER_HEADINGS = 2


def _chapter_heading_matches(text, adaptive=True):
    """Heading matches that start a chapter.

    Normally the h1/h2 headings. When the text has fewer than
    MIN_CHAPTER_HEADINGS of those (a generated profile uses ### sections
    under at most one # title), the chapter level is the highest level
    among h3-h6 that appears at least MIN_CHAPTER_HEADINGS times, and
    headings of that level or higher are chapters. Texts that already
    split at h1/h2 are unaffected.
    """
    top = list(_CHAPTER_HEADING_RE.finditer(text))
    if not adaptive or len(top) >= MIN_CHAPTER_HEADINGS:
        return top
    all_matches = list(_ANY_HEADING_RE.finditer(text))
    counts = {}
    for m in all_matches:
        counts[len(m.group(1))] = counts.get(len(m.group(1)), 0) + 1
    for level in range(3, 7):
        if counts.get(level, 0) >= MIN_CHAPTER_HEADINGS:
            return [m for m in all_matches if len(m.group(1)) <= level]
    return top


def split_sections(text, adaptive=True):
    """Split markdown into chapter sections at headings (#145).

    Returns [(title_or_None, body)]. Content before the first heading
    becomes a section with title None. Heading lines themselves are NOT
    part of the body (the title is spoken separately, see
    section_aware_chunk_text). Chapters are h1/h2 headings; if the text
    has fewer than two of those, the most prominent repeated h3-h6 level
    is used instead (see _chapter_heading_matches). Deeper headings stay
    inside their section's body.

    adaptive=False keeps h1/h2 only: the streamed-reply planner
    (tts_stream_text.ChunkPlanner) cuts chapters while the reply is still
    arriving, so it cannot know whether h1/h2 will turn out to be rare.
    """
    matches = _chapter_heading_matches(text or "", adaptive)
    if not matches:
        return [(None, (text or "").strip())] if (text or "").strip() else []

    sections = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        sections.append((m.group(2).strip(), body))
    return sections


def section_aware_chunk_text(text, first_chunk_gen_secs: float = 3.0,
                             adaptive_level=True):
    """Chunk text for streaming TTS without crossing chapter boundaries.

    Returns [(chunk_text, section_title, section_index)]. The adaptive
    size schedule (small first chunk → medium second → full-size rest)
    applies GLOBALLY across sections so playback still starts fast, while
    every chunk belongs to exactly one section — the prerequisite for
    chapter-accurate jumping in the player (#145).

    Section titles ARE spoken (maintainer decision, v2 of #145): each
    section's text starts with the bare title (no markdown markers,
    sentence-terminated) separated from the body by a blank line, which
    gives the TTS voice an audible chapter pause. Chapter start_times
    stay exact — the title audio belongs to its own section's first
    chunk.

    adaptive_level=False splits at h1/h2 only (see split_sections).

    Sections whose body is empty (e.g. a heading directly followed by
    another heading) are skipped — they'd produce zero-length audio.
    """
    gen_chars_per_sec = TTS_GEN_CHARS_PER_SEC
    audio_secs_per_char = TTS_AUDIO_SECS_PER_CHAR
    overhead_secs = TTS_CHUNK_OVERHEAD_SECS

    first_chunk_chars = min(
        int(first_chunk_gen_secs * gen_chars_per_sec), TTS_MAX_CHARS)

    # Gapless-pipeline budgets (#140 v2): chunk N must finish generating
    # before playback reaches it. With playback starting when chunk 1 is
    # ready, that means gen-time of chunks 2..N must fit inside the
    # playback time of chunks 1..N-1. Budgets therefore derive from the
    # ACTUAL emitted chunks — a short first chunk (sentence or section
    # boundary) gets a proportionally smaller second chunk (~40-50s of
    # audio instead of the old static ~2min, which stalled playback).
    emitted_playback_secs = [0.0]   # cumulative playback of chunks 1..N
    committed_gen_secs = [0.0]      # cumulative gen time of chunks 2..N

    def _budget():
        if not out:
            return first_chunk_chars
        window = (emitted_playback_secs[0] - committed_gen_secs[0]
                  - overhead_secs)
        return max(min(int(max(window, 2.0) * gen_chars_per_sec),
                       TTS_MAX_CHARS), 1)

    def _account(chunk_len):
        if out:  # chunks 2..N consume the generation window
            committed_gen_secs[0] += (chunk_len / gen_chars_per_sec
                                      + overhead_secs)
        emitted_playback_secs[0] += chunk_len * audio_secs_per_char

    out = []
    for section_index, (title, body) in enumerate(split_sections(text, adaptive_level)):
        if title and body:
            spoken = title if title[-1] in '.!?' else f"{title}."
            remaining = f"{spoken}\n\n{body}"
        else:
            remaining = body
        while remaining:
            budget = _budget()
            if len(remaining) <= budget:
                _account(len(remaining))
                out.append((remaining, title, section_index))
                break
            split_point = _split_at_sentence(remaining, budget)
            # #140: a tiny first chunk (short opening sentence) falls
            # back to a word-aware split at the full budget.
            if len(out) == 0 and split_point < MIN_FIRST_CHUNK_CHARS:
                split_point = _split_at_word(remaining, budget)
            chunk = remaining[:split_point].strip()
            if chunk:
                _account(len(chunk))
                out.append((chunk, title, section_index))
            remaining = remaining[split_point:].strip()
    return out


def _split_at_word(text, max_chars):
    """Last word boundary within max_chars (never mid-word)."""
    if len(text) <= max_chars:
        return len(text)
    chunk = text[:max_chars]
    best = chunk.rfind(' ')
    for sep in ('\n', '\t'):
        best = max(best, chunk.rfind(sep))
    return best + 1 if best > 0 else max_chars


def tts_chapters(chunks):
    """Chapter list for generated TTS audio (#145), from an entity's
    TTSChunk rows in chunk order: the first chunk of each markdown section
    with its cumulative start time (sum of prior chunk durations), so the
    player can seek within the merged file or the chunked queue alike.
    A single untitled section is not a chapter list — returns []."""
    chapters, elapsed, seen = [], 0.0, set()
    for chunk in chunks:
        key = chunk.section_index
        if key is not None and key not in seen:
            seen.add(key)
            chapters.append({
                "section_index": key,
                "title": chunk.section_title or "Introduction",
                "chunk_index": chunk.chunk_index,
                "start_time": round(elapsed, 2),
            })
        elapsed += chunk.duration or 0.0
    return chapters if len(chapters) > 1 else []


# The rate decoded audio is counted at: 8 kHz mono 16-bit PCM, 16,000 bytes
# a second. Low, so a 5-minute batch is under 5 MB through the pipe; the
# count is the same at any rate.
DECODE_COUNT_RATE = 8000
# Heuristic: how long counting may take: 60 s plus 3 s a megabyte, at most
# 10 minutes. Decoding runs at hundreds of times real time (an hour of Opus
# or AAC, 25-30 MB, decodes in 2-3 s on a laptop), so audio the apps send is
# counted well within it, also on a slower server.
DECODE_TIMEOUT_MIN_SECONDS = 60
DECODE_TIMEOUT_SECONDS_PER_MB = 3
DECODE_TIMEOUT_MAX_SECONDS = 600


def decode_timeout_seconds(file_path):
    """How long counting *file_path*'s audio may take (see above)."""
    try:
        megabytes = pathlib.Path(file_path).stat().st_size / 1_000_000
    except OSError:
        megabytes = 0
    return min(DECODE_TIMEOUT_MAX_SECONDS,
               DECODE_TIMEOUT_MIN_SECONDS
               + DECODE_TIMEOUT_SECONDS_PER_MB * megabytes)


def decoded_audio_seconds(file_path, timeout_seconds=None, logger=None,
                          stop_after=None):
    """Seconds of audio in *file_path*, counted from its decoded samples, not
    from its container's timestamps (which the recording client writes): the
    first audio stream is decoded to 8 kHz mono 16-bit PCM on a pipe and the
    bytes are counted as they come, never held. Used for the length bounds
    and for the transcription cost (utils/recording_limits).

    None when ffmpeg is missing, the file cannot be decoded, or decoding
    takes longer than *timeout_seconds* (default decode_timeout_seconds;
    the process is killed). Callers then re-encode the audio on the server
    (split_audio_for_transcription) rather than estimate its length.

    *stop_after*: the decode stops once it has more than this many seconds
    of audio (the process is killed) and math.inf is returned, so a file
    that decodes to hours costs no more to measure than this."""
    import subprocess
    import threading
    if timeout_seconds is None:
        timeout_seconds = decode_timeout_seconds(file_path)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(file_path),
           "-map", "0:a:0", "-ac", "1", "-ar", str(DECODE_COUNT_RATE),
           "-f", "s16le", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        if logger:
            logger.warning(f"Could not start ffmpeg to measure audio: {e}")
        return None
    timer = threading.Timer(timeout_seconds, proc.kill)
    timer.start()
    total = 0
    most = (None if stop_after is None
            else int(stop_after * 2 * DECODE_COUNT_RATE))
    reached = False
    try:
        while True:
            data = proc.stdout.read(1 << 16)
            if not data:
                break
            total += len(data)
            if most is not None and total > most:
                reached = True
                proc.kill()
                break
        returncode = proc.wait()
    finally:
        timed_out = not timer.is_alive()
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    if reached:
        return math.inf
    if timed_out or returncode != 0 or total == 0:
        if logger:
            logger.warning(
                "Could not count decoded audio of %s (exit %s, %s bytes%s)",
                pathlib.Path(file_path).name, returncode, total,
                ", timed out" if timed_out else "")
        return None
    return total / (2 * DECODE_COUNT_RATE)


# Heuristic: how long the audio work for one uploaded file may take in all
# (measuring it, cutting it into pieces and measuring those): half the
# task's hard limit (celery_app.TASK_TIME_LIMIT). The cutting re-encodes the
# audio as the earlier chunking did; an hour of audio takes about 12 s on a
# laptop.
SPLIT_TIMEOUT_SECONDS = 1800
# The pieces' bitrate, as the earlier chunking used.
PIECE_BITRATE = 128_000


def seconds_left(deadline, cap):
    """*cap*, cut to the time left until *deadline* (a time.monotonic()
    value; None: no deadline). Zero or less when it has passed."""
    import time
    if deadline is None:
        return cap
    return min(cap, deadline - time.monotonic())


def split_audio_for_transcription(file_path, piece_seconds, limit_seconds,
                                  timeout_seconds=SPLIT_TIMEOUT_SECONDS,
                                  logger=None, deadline=None):
    """(pieces, cut): the audio of *file_path* to send to the provider, as
    [(path, seconds)]: MP3 pieces of *piece_seconds* in a new temporary
    folder, up to *limit_seconds*. Its first audio stream is decoded and
    re-timed from its samples, so the cut and the pieces follow the audio
    itself, whatever the container's timestamps say; at most
    ceil(limit / piece) pieces. *seconds*: the piece's decoded length, or,
    when it cannot be decoded (or no time is left to), *piece_seconds* (the
    most a piece holds). A last piece under a second's bytes that cannot be
    decoded (the frames left at the end of the audio) is left out.
    *cut*: the file holds audio past *limit_seconds* (in no piece).

    All of its ffmpeg work (the re-encode, then measuring the pieces) ends
    by *deadline* (a time.monotonic() value) when given, the re-encode also
    within *timeout_seconds*. ([], False) when ffmpeg fails or runs out of
    time."""
    import glob
    import shutil
    import subprocess
    timeout = seconds_left(deadline, timeout_seconds)
    if timeout <= 0:
        return [], False
    # Piece boundaries, the last one at the limit; one second more is
    # decoded, so audio past the limit shows as one more piece.
    bounds = []
    edge = piece_seconds
    while edge < limit_seconds:
        bounds.append(edge)
        edge += piece_seconds
    bounds.append(limit_seconds)
    out_dir = tempfile.mkdtemp(prefix="pieces_")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(file_path),
           "-map", "0:a:0", "-vn", "-af", "asetpts=N/SR/TB",
           "-t", str(limit_seconds + 1),
           "-c:a", "libmp3lame", "-b:a", str(PIECE_BITRATE),
           "-f", "segment",
           "-segment_times", ",".join(str(b) for b in bounds),
           "-reset_timestamps", "1",
           os.path.join(out_dir, "piece_%03d.mp3")]
    try:
        subprocess.run(cmd, check=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        if logger:
            logger.error(f"Cutting audio into pieces failed: "
                         f"{type(e).__name__}")
        shutil.rmtree(out_dir, ignore_errors=True)
        return [], False

    def measure(path):
        left = seconds_left(deadline, decode_timeout_seconds(path))
        if left <= 0:
            return None
        return decoded_audio_seconds(path, timeout_seconds=left,
                                     logger=logger)

    paths = sorted(glob.glob(os.path.join(out_dir, "piece_*.mp3")))
    kept, extra = paths[:len(bounds)], paths[len(bounds):]
    cut = any(measure(path) for path in extra)
    for path in extra:
        os.unlink(path)
    pieces = []
    for n, path in enumerate(kept):
        size = pathlib.Path(path).stat().st_size
        seconds = measure(path)
        if seconds is None:
            if n == len(kept) - 1 and size < PIECE_BITRATE // 8:
                os.unlink(path)
                continue
            seconds = piece_seconds
        pieces.append((path, seconds))
    if not pieces:
        shutil.rmtree(out_dir, ignore_errors=True)
    return pieces, cut
