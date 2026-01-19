"""
Utility functions for WebM audio file handling.

WebM files recorded via MediaRecorder with timeslice often lack proper duration
metadata in their headers. These utilities help fix that issue.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


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


def fix_webm_duration(filepath: str) -> Tuple[bool, str, Optional[float]]:
    """
    Fix the duration metadata of a WebM file by remuxing it with ffmpeg.

    This creates a temporary file, remuxes the audio (without re-encoding),
    and replaces the original file with the fixed version.

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
        # Remux the file with ffmpeg
        # -i: input file
        # -c copy: copy streams without re-encoding (fast)
        # -copyts: preserve original timestamps (important for chunked recordings!)
        # -y: overwrite output file
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
            return False, f"ffmpeg failed: {result.stderr[:200]}", None

        # Verify the new file has duration
        new_duration = get_webm_duration(temp_path)

        if new_duration is None:
            os.unlink(temp_path)
            return False, "Remuxed file still has no duration", None

        # Replace the original file with the fixed one
        shutil.move(temp_path, filepath)

        new_str = f"{new_duration:.2f}s"
        logger.info(f"Fixed WebM duration: {filepath} ({old_str} -> {new_str})")

        return True, f"Fixed: {old_str} -> {new_str}", new_duration

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
