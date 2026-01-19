#!/usr/bin/env python3
"""
Fix WebM duration metadata for chunked audio files.

WebM files recorded via MediaRecorder with timeslice often lack proper duration
metadata in their headers. This script:
1. Remuxes with ffmpeg to get duration metadata (even if wrong)
2. Calculates actual_duration = ffmpeg_duration - start_time
3. Directly edits the EBML Duration to set the correct value

Usage:
    python fix_webm_duration.py /path/to/chunks/directory
    python fix_webm_duration.py /path/to/chunks/directory --all  # Fix all chunks, not just last
    python fix_webm_duration.py /path/to/specific/chunk.webm     # Fix a single file

Requirements:
    - ffmpeg must be installed and available in PATH
"""

import argparse
import glob
import os
import shutil
import struct
import subprocess
import sys
import tempfile


# =============================================================================
# EBML Direct Duration Editing
# =============================================================================

EBML_ID = b'\x1a\x45\xdf\xa3'
SEGMENT_ID = b'\x18\x53\x80\x67'
INFO_ID = b'\x15\x49\xa9\x66'
DURATION_ID = b'\x44\x89'
TIMECODE_SCALE_ID = b'\x2a\xd7\xb1'


def _read_vint(data, pos):
    if pos >= len(data):
        return None, pos
    first_byte = data[pos]
    if first_byte & 0x80:
        length, value = 1, first_byte & 0x7f
    elif first_byte & 0x40:
        length, value = 2, first_byte & 0x3f
    elif first_byte & 0x20:
        length, value = 3, first_byte & 0x1f
    elif first_byte & 0x10:
        length, value = 4, first_byte & 0x0f
    elif first_byte & 0x08:
        length, value = 5, first_byte & 0x07
    elif first_byte & 0x04:
        length, value = 6, first_byte & 0x03
    elif first_byte & 0x02:
        length, value = 7, first_byte & 0x01
    elif first_byte & 0x01:
        length, value = 8, 0
    else:
        return None, pos
    for i in range(1, length):
        if pos + i >= len(data):
            return None, pos
        value = (value << 8) | data[pos + i]
    return value, pos + length


def _read_element_id(data, pos):
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


def _find_info_element(data):
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


def _find_duration_in_info(data, info_start, info_size):
    pos = info_start
    end = info_start + info_size
    timecode_scale = 1000000
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


def set_ebml_duration(filepath, duration_seconds):
    """Directly set the Duration metadata in a WebM file's EBML structure."""
    try:
        with open(filepath, 'rb') as f:
            data = bytearray(f.read())
        info_start, info_size = _find_info_element(data)
        if info_start is None:
            print(f"  Warning: Could not find Info element in {filepath}")
            return False
        duration_pos, duration_size, timecode_scale = _find_duration_in_info(
            data, info_start, info_size
        )
        if duration_pos is None:
            print(f"  Warning: Could not find Duration element in {filepath}")
            return False
        new_duration_ns = duration_seconds * 1e9
        new_duration_value = new_duration_ns / timecode_scale
        if duration_size == 8:
            new_duration_bytes = struct.pack('>d', new_duration_value)
        elif duration_size == 4:
            new_duration_bytes = struct.pack('>f', new_duration_value)
        else:
            print(f"  Warning: Unexpected duration size: {duration_size}")
            return False
        data[duration_pos:duration_pos + duration_size] = new_duration_bytes
        with open(filepath, 'wb') as f:
            f.write(data)
        # Ensure file is readable by web server (644 = rw-r--r--)
        os.chmod(filepath, 0o644)
        return True
    except Exception as e:
        print(f"  Warning: Error setting EBML duration: {e}")
        return False


# =============================================================================
# FFprobe helpers
# =============================================================================

def get_webm_duration(filepath):
    """Get the duration of a WebM file using ffprobe. Returns None if unavailable."""
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
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return None


def get_webm_start_time(filepath):
    """Get the start_time of a WebM file using ffprobe."""
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
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return None


# =============================================================================
# Main fix function
# =============================================================================

def fix_webm_duration(filepath, dry_run=False):
    """
    Fix the duration metadata of a WebM file.

    1. Remux with ffmpeg to get duration (even if wrong: duration = start_time + actual)
    2. Calculate actual_duration = ffmpeg_duration - start_time
    3. Directly edit EBML Duration to set correct value

    Returns:
        tuple: (success: bool, message: str, old_duration, new_duration)
    """
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}", None, None

    if not filepath.endswith('.webm'):
        return False, f"Not a WebM file: {filepath}", None, None

    old_duration = get_webm_duration(filepath)

    if dry_run:
        if old_duration is None:
            return True, f"Would fix: {filepath} (current duration: N/A)", None, None
        else:
            return True, f"Would fix: {filepath} (current duration: {old_duration:.2f}s)", old_duration, None

    fd, temp_path = tempfile.mkstemp(suffix='.webm')
    os.close(fd)

    try:
        # Step 1: Remux with ffmpeg
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
            os.unlink(temp_path)
            return False, f"ffmpeg failed: {result.stderr}", old_duration, None

        # Step 2: Get (wrong) duration and start_time
        ffmpeg_duration = get_webm_duration(temp_path)
        start_time = get_webm_start_time(temp_path)

        if ffmpeg_duration is None:
            os.unlink(temp_path)
            return False, "Remuxed file still has no duration", old_duration, None

        # Step 3: Calculate actual duration
        if start_time is not None and start_time > 0:
            actual_duration = ffmpeg_duration - start_time
        else:
            actual_duration = ffmpeg_duration

        # Step 4: Replace original with remuxed file
        shutil.move(temp_path, filepath)

        # Ensure file is readable by web server (644 = rw-r--r--)
        os.chmod(filepath, 0o644)

        # Step 5: Set correct EBML duration if needed
        if start_time is not None and start_time > 0:
            set_ebml_duration(filepath, actual_duration)

        # Verify final duration
        final_duration = get_webm_duration(filepath)

        return True, f"Fixed: {filepath}", old_duration, final_duration

    except subprocess.TimeoutExpired:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        return False, "ffmpeg timed out", old_duration, None
    except Exception as e:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        return False, f"Error: {str(e)}", old_duration, None


def find_chunk_files(directory):
    """Find all chunk WebM files in a directory, sorted by name."""
    pattern = os.path.join(directory, 'chunk_*.webm')
    files = glob.glob(pattern)
    return sorted(files)


def fix_directory(directory, fix_all=False, dry_run=False):
    """
    Fix WebM duration for chunks in a directory.

    Args:
        directory: Path to directory containing chunk files
        fix_all: If True, fix all chunks. If False, only fix the last chunk.
        dry_run: If True, only report what would be done
    """
    if not os.path.isdir(directory):
        print(f"Error: Not a directory: {directory}")
        return False

    chunks = find_chunk_files(directory)

    if not chunks:
        print(f"No chunk files found in {directory}")
        return False

    print(f"Found {len(chunks)} chunk(s) in {directory}")

    # Determine which files to fix
    if fix_all:
        files_to_fix = chunks
    else:
        files_to_fix = [chunks[-1]]  # Only the last chunk
        print(f"Fixing only the last chunk: {os.path.basename(files_to_fix[0])}")

    # Check current status
    print("\nCurrent duration status:")
    for chunk in chunks:
        duration = get_webm_duration(chunk)
        status = f"{duration:.2f}s" if duration else "N/A"
        marker = " <- will fix" if chunk in files_to_fix else ""
        print(f"  {os.path.basename(chunk)}: {status}{marker}")

    if dry_run:
        print("\n[DRY RUN] No changes made")
        return True

    # Fix the files
    print("\nFixing files...")
    success_count = 0
    for filepath in files_to_fix:
        success, message, old_dur, new_dur = fix_webm_duration(filepath)

        if success:
            old_str = f"{old_dur:.2f}s" if old_dur else "N/A"
            new_str = f"{new_dur:.2f}s" if new_dur else "N/A"
            print(f"  {os.path.basename(filepath)}: {old_str} -> {new_str}")
            success_count += 1
        else:
            print(f"  {os.path.basename(filepath)}: FAILED - {message}")

    print(f"\nFixed {success_count}/{len(files_to_fix)} file(s)")

    # Show final status
    print("\nFinal duration status:")
    for chunk in chunks:
        duration = get_webm_duration(chunk)
        status = f"{duration:.2f}s" if duration else "N/A"
        print(f"  {os.path.basename(chunk)}: {status}")

    return success_count == len(files_to_fix)


def main():
    parser = argparse.ArgumentParser(
        description='Fix WebM duration metadata for chunked audio files'
    )
    parser.add_argument(
        'path',
        help='Path to a directory containing chunk files, or a single WebM file'
    )
    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Fix all chunks, not just the last one'
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Show what would be done without making changes'
    )

    args = parser.parse_args()

    # Check if ffmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("Error: ffmpeg not found. Please install ffmpeg.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: ffmpeg timed out")
        sys.exit(1)

    path = args.path

    if os.path.isfile(path):
        # Single file mode
        success, message, old_dur, new_dur = fix_webm_duration(path, dry_run=args.dry_run)
        if success:
            if args.dry_run:
                print(message)
            else:
                old_str = f"{old_dur:.2f}s" if old_dur else "N/A"
                new_str = f"{new_dur:.2f}s" if new_dur else "N/A"
                print(f"Fixed: {old_str} -> {new_str}")
        else:
            print(f"Failed: {message}")
            sys.exit(1)
    elif os.path.isdir(path):
        # Directory mode
        success = fix_directory(path, fix_all=args.all, dry_run=args.dry_run)
        if not success:
            sys.exit(1)
    else:
        print(f"Error: Path not found: {path}")
        sys.exit(1)


if __name__ == '__main__':
    main()
