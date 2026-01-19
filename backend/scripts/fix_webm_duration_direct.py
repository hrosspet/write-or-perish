#!/usr/bin/env python3
"""
Directly edit WebM duration metadata without changing timestamps.

WebM files use EBML format. The duration is stored in the Segment > Info > Duration element.
This script finds and modifies that specific value while preserving everything else.

Usage:
    python fix_webm_duration_direct.py <input.webm> <duration_seconds>

Example:
    python fix_webm_duration_direct.py chunk_0006.webm 127.905
"""

import struct
import sys
import os
import shutil


# EBML Element IDs (as bytes, variable length)
EBML_ID = b'\x1a\x45\xdf\xa3'
SEGMENT_ID = b'\x18\x53\x80\x67'
INFO_ID = b'\x15\x49\xa9\x66'
DURATION_ID = b'\x44\x89'
TIMECODE_SCALE_ID = b'\x2a\xd7\xb1'


def read_vint(data, pos):
    """Read a variable-length integer (VINT) from EBML data."""
    if pos >= len(data):
        return None, pos

    first_byte = data[pos]

    # Determine length from leading zeros
    if first_byte & 0x80:  # 1 byte
        length = 1
        value = first_byte & 0x7f
    elif first_byte & 0x40:  # 2 bytes
        length = 2
        value = first_byte & 0x3f
    elif first_byte & 0x20:  # 3 bytes
        length = 3
        value = first_byte & 0x1f
    elif first_byte & 0x10:  # 4 bytes
        length = 4
        value = first_byte & 0x0f
    elif first_byte & 0x08:  # 5 bytes
        length = 5
        value = first_byte & 0x07
    elif first_byte & 0x04:  # 6 bytes
        length = 6
        value = first_byte & 0x03
    elif first_byte & 0x02:  # 7 bytes
        length = 7
        value = first_byte & 0x01
    elif first_byte & 0x01:  # 8 bytes
        length = 8
        value = 0
    else:
        return None, pos

    for i in range(1, length):
        if pos + i >= len(data):
            return None, pos
        value = (value << 8) | data[pos + i]

    return value, pos + length


def read_element_id(data, pos):
    """Read an EBML element ID."""
    if pos >= len(data):
        return None, pos

    first_byte = data[pos]

    if first_byte & 0x80:  # 1 byte ID
        length = 1
    elif first_byte & 0x40:  # 2 byte ID
        length = 2
    elif first_byte & 0x20:  # 3 byte ID
        length = 3
    elif first_byte & 0x10:  # 4 byte ID
        length = 4
    else:
        return None, pos

    if pos + length > len(data):
        return None, pos

    return data[pos:pos + length], pos + length


def find_duration_in_info(data, info_start, info_size):
    """Find the Duration element within the Info element."""
    pos = info_start
    end = info_start + info_size
    timecode_scale = 1000000  # Default: 1ms
    duration_pos = None
    duration_size = None

    while pos < end:
        elem_id, pos = read_element_id(data, pos)
        if elem_id is None:
            break

        elem_size, pos = read_vint(data, pos)
        if elem_size is None:
            break

        if elem_id == TIMECODE_SCALE_ID:
            # Read timecode scale value
            timecode_scale = 0
            for i in range(elem_size):
                timecode_scale = (timecode_scale << 8) | data[pos + i]

        if elem_id == DURATION_ID:
            duration_pos = pos
            duration_size = elem_size

        pos += elem_size

    return duration_pos, duration_size, timecode_scale


def find_info_element(data):
    """Find the Info element in the WebM file."""
    pos = 0

    # Skip EBML header
    elem_id, pos = read_element_id(data, pos)
    if elem_id != EBML_ID:
        return None, None

    elem_size, pos = read_vint(data, pos)
    pos += elem_size  # Skip EBML header content

    # Find Segment
    elem_id, pos = read_element_id(data, pos)
    if elem_id != SEGMENT_ID:
        return None, None

    segment_size, pos = read_vint(data, pos)
    segment_start = pos

    # Search for Info element within Segment
    # (Segment can have unknown size, so we search until we find Info)
    search_limit = min(pos + 100000, len(data))  # Search first 100KB

    while pos < search_limit:
        elem_id, new_pos = read_element_id(data, pos)
        if elem_id is None:
            break

        elem_size, new_pos = read_vint(data, new_pos)
        if elem_size is None:
            break

        if elem_id == INFO_ID:
            return new_pos, elem_size

        # Skip this element and continue searching
        # For unknown-size elements, we can't skip properly
        if elem_size == 0xffffffffffffff:  # Unknown size marker
            pos = new_pos
            continue

        pos = new_pos + elem_size

    return None, None


def encode_float64(value):
    """Encode a float as 8-byte big-endian IEEE 754."""
    return struct.pack('>d', value)


def fix_webm_duration(input_path, new_duration_seconds, output_path=None):
    """
    Fix the duration metadata in a WebM file.

    Args:
        input_path: Path to the WebM file
        new_duration_seconds: The correct duration in seconds
        output_path: Output path (defaults to overwriting input)
    """
    if output_path is None:
        output_path = input_path

    # Read the file
    with open(input_path, 'rb') as f:
        data = bytearray(f.read())

    # Find Info element
    info_start, info_size = find_info_element(data)
    if info_start is None:
        print("Error: Could not find Info element in WebM file")
        return False

    print(f"Found Info element at position {info_start}, size {info_size}")

    # Find Duration within Info
    duration_pos, duration_size, timecode_scale = find_duration_in_info(
        data, info_start, info_size
    )

    if duration_pos is None:
        print("Error: Could not find Duration element in Info")
        return False

    print(f"Found Duration at position {duration_pos}, size {duration_size}")
    print(f"Timecode scale: {timecode_scale} ns")

    # Read current duration
    if duration_size == 8:
        current_duration = struct.unpack('>d', bytes(data[duration_pos:duration_pos + 8]))[0]
    elif duration_size == 4:
        current_duration = struct.unpack('>f', bytes(data[duration_pos:duration_pos + 4]))[0]
    else:
        print(f"Error: Unexpected duration size: {duration_size}")
        return False

    # Duration in WebM is stored as: duration_value * timecode_scale = duration_in_nanoseconds
    current_duration_seconds = (current_duration * timecode_scale) / 1e9
    print(f"Current duration value: {current_duration}")
    print(f"Current duration in seconds: {current_duration_seconds:.3f}")

    # Calculate new duration value
    new_duration_ns = new_duration_seconds * 1e9
    new_duration_value = new_duration_ns / timecode_scale

    print(f"New duration value: {new_duration_value}")
    print(f"New duration in seconds: {new_duration_seconds:.3f}")

    # Encode new duration
    if duration_size == 8:
        new_duration_bytes = struct.pack('>d', new_duration_value)
    else:
        new_duration_bytes = struct.pack('>f', new_duration_value)

    # Replace duration in data
    data[duration_pos:duration_pos + duration_size] = new_duration_bytes

    # Write output
    if output_path != input_path:
        # Write to new file
        with open(output_path, 'wb') as f:
            f.write(data)
    else:
        # Backup and overwrite
        backup_path = input_path + '.bak'
        shutil.copy(input_path, backup_path)
        with open(input_path, 'wb') as f:
            f.write(data)
        print(f"Backup saved to: {backup_path}")

    print(f"Duration updated successfully in: {output_path}")
    return True


def main():
    if len(sys.argv) < 3:
        print("Usage: python fix_webm_duration_direct.py <input.webm> <duration_seconds> [output.webm]")
        print("\nExample:")
        print("  python fix_webm_duration_direct.py chunk_0006.webm 127.905")
        sys.exit(1)

    input_path = sys.argv[1]
    new_duration = float(sys.argv[2])
    output_path = sys.argv[3] if len(sys.argv) > 3 else None

    if not os.path.exists(input_path):
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    success = fix_webm_duration(input_path, new_duration, output_path)
    if not success:
        sys.exit(1)


if __name__ == '__main__':
    main()
