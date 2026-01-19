"""
Utility functions for WebM audio file handling.

WebM files recorded via MediaRecorder with timeslice often lack proper duration
metadata in their headers. These utilities help fix that issue.

The problem: MediaRecorder with timeslice creates chunks with continuous timestamps
(chunk 0: 0-300s, chunk 1: 300-600s, etc.) but no duration metadata.

When ffmpeg remuxes these files, it sets duration = start_time + actual_duration,
which is incorrect. We need to:
1. Remux with ffmpeg to get the (wrong) duration
2. Calculate actual_duration = ffmpeg_duration - start_time
3. Directly edit the EBML Duration metadata with the correct value
"""

import logging
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# EBML Direct Duration Editing
# =============================================================================

# EBML Element IDs (as bytes, variable length)
EBML_ID = b'\x1a\x45\xdf\xa3'
SEGMENT_ID = b'\x18\x53\x80\x67'
INFO_ID = b'\x15\x49\xa9\x66'
DURATION_ID = b'\x44\x89'
TIMECODE_SCALE_ID = b'\x2a\xd7\xb1'


def _read_vint(data, pos):
    """Read a variable-length integer (VINT) from EBML data."""
    if pos >= len(data):
        return None, pos

    first_byte = data[pos]

    if first_byte & 0x80:
        length = 1
        value = first_byte & 0x7f
    elif first_byte & 0x40:
        length = 2
        value = first_byte & 0x3f
    elif first_byte & 0x20:
        length = 3
        value = first_byte & 0x1f
    elif first_byte & 0x10:
        length = 4
        value = first_byte & 0x0f
    elif first_byte & 0x08:
        length = 5
        value = first_byte & 0x07
    elif first_byte & 0x04:
        length = 6
        value = first_byte & 0x03
    elif first_byte & 0x02:
        length = 7
        value = first_byte & 0x01
    elif first_byte & 0x01:
        length = 8
        value = 0
    else:
        return None, pos

    for i in range(1, length):
        if pos + i >= len(data):
            return None, pos
        value = (value << 8) | data[pos + i]

    return value, pos + length


def _read_element_id(data, pos):
    """Read an EBML element ID."""
    if pos >= len(data):
        return None, pos

    first_byte = data[pos]

    if first_byte & 0x80:
        length = 1
    elif first_byte & 0x40:
        length = 2
    elif first_byte & 0x20:
        length = 3
    elif first_byte & 0x10:
        length = 4
    else:
        return None, pos

    if pos + length > len(data):
        return None, pos

    return data[pos:pos + length], pos + length


def _find_duration_in_info(data, info_start, info_size):
    """Find the Duration element within the Info element."""
    pos = info_start
    end = info_start + info_size
    timecode_scale = 1000000  # Default: 1ms
    duration_pos = None
    duration_size = None

    while pos < end:
        elem_id, pos = _read_element_id(data, pos)
        if elem_id is None:
            break

        elem_size, pos = _read_vint(data, pos)
        if elem_size is None:
            break

        if elem_id == TIMECODE_SCALE_ID:
            timecode_scale = 0
            for i in range(elem_size):
                timecode_scale = (timecode_scale << 8) | data[pos + i]

        if elem_id == DURATION_ID:
            duration_pos = pos
            duration_size = elem_size

        pos += elem_size

    return duration_pos, duration_size, timecode_scale


def _find_info_element(data):
    """Find the Info element in the WebM file."""
    pos = 0

    elem_id, pos = _read_element_id(data, pos)
    if elem_id != EBML_ID:
        return None, None

    elem_size, pos = _read_vint(data, pos)
    pos += elem_size

    elem_id, pos = _read_element_id(data, pos)
    if elem_id != SEGMENT_ID:
        return None, None

    segment_size, pos = _read_vint(data, pos)
    search_limit = min(pos + 100000, len(data))

    while pos < search_limit:
        elem_id, new_pos = _read_element_id(data, pos)
        if elem_id is None:
            break

        elem_size, new_pos = _read_vint(data, new_pos)
        if elem_size is None:
            break

        if elem_id == INFO_ID:
            return new_pos, elem_size

        if elem_size == 0xffffffffffffff:
            pos = new_pos
            continue

        pos = new_pos + elem_size

    return None, None


def _set_ebml_duration(filepath: str, duration_seconds: float) -> bool:
    """
    Directly set the Duration metadata in a WebM file's EBML structure.

    Args:
        filepath: Path to the WebM file
        duration_seconds: The correct duration in seconds

    Returns:
        True if successful, False otherwise
    """
    try:
        with open(filepath, 'rb') as f:
            data = bytearray(f.read())

        info_start, info_size = _find_info_element(data)
        if info_start is None:
            logger.error(f"Could not find Info element in {filepath}")
            return False

        duration_pos, duration_size, timecode_scale = _find_duration_in_info(
            data, info_start, info_size
        )

        if duration_pos is None:
            logger.error(f"Could not find Duration element in {filepath}")
            return False

        # Calculate new duration value (duration * timecode_scale = nanoseconds)
        new_duration_ns = duration_seconds * 1e9
        new_duration_value = new_duration_ns / timecode_scale

        # Encode new duration
        if duration_size == 8:
            new_duration_bytes = struct.pack('>d', new_duration_value)
        elif duration_size == 4:
            new_duration_bytes = struct.pack('>f', new_duration_value)
        else:
            logger.error(f"Unexpected duration size: {duration_size}")
            return False

        # Replace duration in data
        data[duration_pos:duration_pos + duration_size] = new_duration_bytes

        # Write back
        with open(filepath, 'wb') as f:
            f.write(data)

        return True

    except Exception as e:
        logger.error(f"Error setting EBML duration for {filepath}: {e}")
        return False


# =============================================================================
# Public API
# =============================================================================


def get_webm_duration(filepath: str) -> Optional[float]:
    """
    Get the duration of a WebM file using ffprobe.

    Args:
        filepath: Path to the WebM file

    Returns:
        Duration in seconds, or None if unavailable
    """
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'csv=p=0',
                filepath
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        duration_str = result.stdout.strip()
        if duration_str and duration_str != 'N/A':
            return float(duration_str)
        return None
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError) as e:
        logger.warning(f"Could not get duration for {filepath}: {e}")
        return None


def _get_webm_start_time(filepath: str) -> Optional[float]:
    """
    Get the start_time of a WebM file using ffprobe.

    Args:
        filepath: Path to the WebM file

    Returns:
        Start time in seconds, or None if unavailable
    """
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=start_time',
                '-of', 'csv=p=0',
                filepath
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        start_str = result.stdout.strip()
        if start_str and start_str != 'N/A':
            return float(start_str)
        return None
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError) as e:
        logger.warning(f"Could not get start_time for {filepath}: {e}")
        return None


def fix_webm_duration(filepath: str) -> Tuple[bool, str, Optional[float]]:
    """
    Fix the duration metadata of a WebM file.

    WebM files from MediaRecorder with timeslice have continuous timestamps
    but no duration metadata. When ffmpeg remuxes them, it incorrectly sets
    duration = start_time + actual_duration.

    This function:
    1. Remuxes with ffmpeg to get duration metadata (even if wrong)
    2. Calculates actual_duration = ffmpeg_duration - start_time
    3. Directly edits the EBML Duration to set the correct value

    Args:
        filepath: Path to the WebM file

    Returns:
        tuple: (success: bool, message: str, new_duration: Optional[float])
    """
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}", None

    if not filepath.endswith('.webm'):
        return False, f"Not a WebM file: {filepath}", None

    # Get current duration for logging
    old_duration = get_webm_duration(filepath)
    old_str = f"{old_duration:.2f}s" if old_duration else "N/A"

    # Create a temporary file for the remuxed output
    fd, temp_path = tempfile.mkstemp(suffix='.webm')
    os.close(fd)

    try:
        # Step 1: Remux the file with ffmpeg to get duration metadata
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-copyts',
                '-i', filepath,
                '-c', 'copy',
                temp_path
            ],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            logger.error(f"ffmpeg failed for {filepath}: {result.stderr}")
            os.unlink(temp_path)
            return False, f"ffmpeg failed: {result.stderr[:200]}", None

        # Step 2: Get the (wrong) duration and start_time from the remuxed file
        ffmpeg_duration = get_webm_duration(temp_path)
        start_time = _get_webm_start_time(temp_path)

        if ffmpeg_duration is None:
            os.unlink(temp_path)
            return False, "Remuxed file has no duration", None

        # Step 3: Calculate the actual duration
        # ffmpeg sets: duration = start_time + actual_duration
        # So: actual_duration = duration - start_time
        if start_time is not None and start_time > 0:
            actual_duration = ffmpeg_duration - start_time
            logger.info(
                f"Calculating actual duration: {ffmpeg_duration:.2f} - {start_time:.2f} = {actual_duration:.2f}"
            )
        else:
            # No start_time offset, use ffmpeg duration as-is
            actual_duration = ffmpeg_duration

        # Step 4: Replace original with remuxed file
        shutil.move(temp_path, filepath)

        # Step 5: Directly set the correct duration in EBML metadata
        if start_time is not None and start_time > 0:
            if _set_ebml_duration(filepath, actual_duration):
                logger.info(f"Set correct EBML duration: {actual_duration:.2f}s")
            else:
                logger.warning(f"Could not set EBML duration, file may have wrong duration")

        # Verify final duration
        final_duration = get_webm_duration(filepath)
        new_str = f"{final_duration:.2f}s" if final_duration else "N/A"
        logger.info(f"Fixed WebM duration: {filepath} ({old_str} -> {new_str})")

        return True, f"Fixed: {old_str} -> {new_str}", final_duration

    except subprocess.TimeoutExpired:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        logger.error(f"ffmpeg timed out for {filepath}")
        return False, "ffmpeg timed out", None
    except Exception as e:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        logger.error(f"Error fixing {filepath}: {e}")
        return False, f"Error: {str(e)}", None


def fix_last_chunk_duration(chunk_dir: str) -> Tuple[bool, str]:
    """
    Fix the duration metadata of the last chunk file in a directory.

    Args:
        chunk_dir: Path to directory containing chunk_*.webm files

    Returns:
        tuple: (success: bool, message: str)
    """
    chunk_path = Path(chunk_dir)

    if not chunk_path.exists():
        return False, f"Directory not found: {chunk_dir}"

    # Find all chunk files
    chunk_files = sorted(chunk_path.glob("chunk_*.webm"))

    if not chunk_files:
        return False, f"No chunk files found in {chunk_dir}"

    # Fix the last chunk
    last_chunk = str(chunk_files[-1])
    logger.info(f"Fixing last chunk duration: {last_chunk}")

    success, message, new_duration = fix_webm_duration(last_chunk)

    if success:
        return True, f"Fixed {os.path.basename(last_chunk)}: {message}"
    else:
        return False, f"Failed to fix {os.path.basename(last_chunk)}: {message}"


def is_ffmpeg_available() -> bool:
    """Check if ffmpeg is available in PATH."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
