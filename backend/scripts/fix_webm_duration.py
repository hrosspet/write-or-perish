#!/usr/bin/env python3
"""
Fix WebM duration metadata for chunked audio files.

WebM files recorded via MediaRecorder with timeslice often lack proper duration
metadata in their headers. This script uses ffmpeg to remux the files, which
calculates and writes the correct duration.

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
import subprocess
import sys
import tempfile


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


def fix_webm_duration(filepath, dry_run=False):
    """
    Fix the duration metadata of a WebM file by remuxing it with ffmpeg.

    Args:
        filepath: Path to the WebM file
        dry_run: If True, only report what would be done

    Returns:
        tuple: (success: bool, message: str, old_duration, new_duration)
    """
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}", None, None

    if not filepath.endswith('.webm'):
        return False, f"Not a WebM file: {filepath}", None, None

    # Get current duration
    old_duration = get_webm_duration(filepath)

    if dry_run:
        if old_duration is None:
            return True, f"Would fix: {filepath} (current duration: N/A)", None, None
        else:
            return True, f"Would fix: {filepath} (current duration: {old_duration:.2f}s)", old_duration, None

    # Create a temporary file for the remuxed output
    fd, temp_path = tempfile.mkstemp(suffix='.webm')
    os.close(fd)

    try:
        # Remux the file with ffmpeg
        # -i: input file
        # -c copy: copy streams without re-encoding (fast)
        # -y: overwrite output file
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', filepath,
                '-c', 'copy',
                temp_path
            ],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            return False, f"ffmpeg failed: {result.stderr}", old_duration, None

        # Verify the new file has duration
        new_duration = get_webm_duration(temp_path)

        if new_duration is None:
            os.unlink(temp_path)
            return False, "Remuxed file still has no duration", old_duration, None

        # Replace the original file with the fixed one
        shutil.move(temp_path, filepath)

        return True, f"Fixed: {filepath}", old_duration, new_duration

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
