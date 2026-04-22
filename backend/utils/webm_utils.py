"""
Utility functions for WebM audio file handling.

Covers:
- Probing duration via ffprobe (`get_webm_duration`).
- Extracting a MediaRecorder session's init segment (EBML header +
  Segment header + Info + Tracks, everything before the first Cluster)
  via a proper EBML element walk (`extract_webm_init_segment`,
  `find_first_cluster_offset`, `find_all_cluster_offsets`).
- Persisting that init segment alongside a streaming session so later
  batches that don't include chunk 0 can still be remuxed into valid
  WebM (`persist_init_segment`).
- Concatenating MediaRecorder fragments into a single valid WebM via
  binary append + a single ffmpeg remux (`concat_webm_fragments`), and
  concatenating multiple standalone WebM files via the ffmpeg concat
  demuxer (`concat_audio_files`).
"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

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


# Matroska element IDs we care about for init-segment extraction.
_EBML_ID_SEGMENT = 0x18538067
_EBML_ID_CLUSTER = 0x1F43B675
_EBML_ID_TRACKS = 0x1654AE6B


def _ebml_read_vint(data: bytes, offset: int, keep_marker: bool):
    """Read an EBML variable-length integer at `offset`.

    With `keep_marker=True` the full VINT bytes are returned verbatim as the
    value — used for element IDs, whose canonical form keeps the length
    marker bit. With `keep_marker=False` the marker is stripped — used for
    element sizes.

    Returns (value, bytes_consumed, is_unknown_size). `is_unknown_size` is
    only meaningful for size fields (keep_marker=False) and is True when all
    data bits are set, which the spec reserves for "size unknown" (e.g.
    MediaRecorder emits this for its open-ended Segment element).
    """
    if offset >= len(data):
        raise ValueError(f"VINT read past end of buffer at offset {offset}")
    first = data[offset]
    if first == 0:
        # Would imply a VINT longer than 8 bytes; invalid for our purposes.
        raise ValueError(f"Invalid VINT at offset {offset}: first byte is 0")

    length = 1
    mask = 0x80
    while (first & mask) == 0:
        length += 1
        mask >>= 1
        if length > 8:
            raise ValueError(f"VINT too long at offset {offset}")
    if offset + length > len(data):
        raise ValueError(f"VINT truncated at offset {offset}")

    if keep_marker:
        value = 0
        for i in range(length):
            value = (value << 8) | data[offset + i]
        is_unknown = False
    else:
        value = first & (mask - 1)
        for i in range(1, length):
            value = (value << 8) | data[offset + i]
        # Unknown-size marker: all data bits set (2^(7N) - 1 for length N)
        is_unknown = value == ((1 << (7 * length)) - 1)

    return value, length, is_unknown


def persist_init_segment(chunk_path, chunk_dir) -> None:
    """Extract the init segment from a chunk-0 file on disk and persist it
    at `chunk_dir/init.webm.enc` (or `init.webm` if encryption is disabled).

    Raises ValueError if the chunk's WebM bytes can't be parsed or are
    missing a Tracks element. The caller is responsible for deleting the
    offending chunk and surfacing an error — retaining a chunk 0 we can't
    extract the init from would let batch 1 succeed and batch 2 fail
    silently for want of an init segment.
    """
    # Local import avoids circular imports; encryption depends on this
    # module's sibling files.
    from backend.utils.encryption import encrypt_file

    with open(chunk_path, 'rb') as f:
        init_bytes = extract_webm_init_segment(f.read())

    init_path = chunk_dir / "init.webm"
    with open(init_path, 'wb') as f:
        f.write(init_bytes)
    # encrypt_file is a no-op returning the original path when encryption
    # is disabled; no return value to track.
    encrypt_file(str(init_path))


def _walk_segment_children(data: bytes):
    """Yield (child_id, child_start_offset) for each direct child of the first
    Segment element.

    Walks the EBML element tree properly — top-level elements, then Segment's
    children — so magic bytes appearing inside non-master elements
    (CodecPrivate, track UIDs) are skipped by their declared size rather than
    misread as element IDs.

    Raises ValueError on malformed EBML or if no Segment element is found.
    Stops after yielding a child with unknown size, since the sibling
    boundary can't be computed without parsing into the child.
    """
    offset = 0
    while offset < len(data):
        id_val, id_len, _ = _ebml_read_vint(data, offset, keep_marker=True)
        offset += id_len
        size_val, size_len, size_unknown = _ebml_read_vint(
            data, offset, keep_marker=False,
        )
        offset += size_len

        if id_val == _EBML_ID_SEGMENT:
            # MediaRecorder emits the Segment with unknown size (it's still
            # recording when the header is written). Parse children until
            # we run out of bytes or hit the declared end.
            seg_end = len(data) if size_unknown else offset + size_val
            while offset < seg_end:
                child_start = offset
                child_id, child_id_len, _ = _ebml_read_vint(
                    data, offset, keep_marker=True,
                )
                offset += child_id_len
                child_size, child_size_len, child_unknown = _ebml_read_vint(
                    data, offset, keep_marker=False,
                )
                offset += child_size_len
                yield child_id, child_start
                if child_unknown:
                    return
                offset += child_size
            return

        # Top-level non-Segment element (typically just the EBML header);
        # skip its body entirely.
        if size_unknown:
            raise ValueError(
                "Unexpected unknown-size top-level element before Segment"
            )
        offset += size_val

    raise ValueError("No Segment element found in buffer")


def find_first_cluster_offset(data: bytes) -> int:
    """Return the byte offset of the first Matroska Cluster element.

    Raises ValueError on malformed EBML or if no Cluster is found.
    """
    for child_id, start in _walk_segment_children(data):
        if child_id == _EBML_ID_CLUSTER:
            return start
    raise ValueError("No Cluster element found inside Segment")


def find_all_cluster_offsets(data: bytes) -> list:
    """Return byte offsets of every Cluster element in the first Segment.

    Useful for splitting a complete MediaRecorder WebM into per-cluster
    fragment files (e.g. for integration tests that want to simulate a
    batch of 20 raw-cluster chunks).
    """
    return [
        start for child_id, start in _walk_segment_children(data)
        if child_id == _EBML_ID_CLUSTER
    ]


def extract_webm_init_segment(data: bytes) -> bytes:
    """Return the init segment of a MediaRecorder WebM blob.

    Everything up to the first Matroska Cluster is the init segment
    (EBML header + Segment header + SeekHead? + Info + Tracks). Those bytes
    never change over the lifetime of a single MediaRecorder recording, so
    we persist them once and prepend to future batches that don't start at
    chunk 0 — otherwise those batches would be header-less cluster data
    that ffmpeg can't remux.

    Verifies a Tracks element is present in the extracted prefix. Without
    it the WebM has no decodable stream description; returning such an init
    would let chunk-0 upload succeed and batch 2 silently fail at ffmpeg
    remux time.
    """
    cluster_offset = None
    saw_tracks = False
    for child_id, start in _walk_segment_children(data):
        if child_id == _EBML_ID_TRACKS:
            saw_tracks = True
        elif child_id == _EBML_ID_CLUSTER:
            cluster_offset = start
            break
    if cluster_offset is None:
        raise ValueError("No Cluster element found inside Segment")
    if not saw_tracks:
        raise ValueError(
            "Init segment is missing a Tracks element — "
            "chunk 0 is structurally malformed"
        )
    return data[:cluster_offset]


def concat_webm_fragments(paths: list,
                          init_segment_path: Optional[str] = None,
                          output_suffix: str = '.webm') -> str:
    """Concatenate MediaRecorder timeslice fragments into one valid WebM.

    MediaRecorder with a timeslice emits Matroska *fragments* of a single
    continuous stream — only the first blob carries the EBML/Segment/Tracks
    header; subsequent blobs are header-less cluster data with timestamps
    absolute to the original recording. Per the MSE byte-stream format, those
    fragments concatenated in order as raw bytes form exactly one valid
    Matroska file. This function does that append, then runs a single ffmpeg
    remux pass to rewrite the container's Duration/Cues so the output is
    seekable and reports the correct length.

    For batches that don't include the first fragment of the recording,
    pass `init_segment_path` pointing at a previously-extracted init segment
    (see `extract_webm_init_segment`). Its bytes are binary-prepended so the
    resulting file has a valid EBML/Segment/Tracks prefix.
    """
    if not paths:
        raise ValueError("No fragments to concatenate")

    # Step 1: binary-append init segment (if any) + fragments in order
    fd, raw_path = tempfile.mkstemp(suffix=output_suffix, prefix='frag_raw_')
    os.close(fd)
    try:
        with open(raw_path, 'wb') as out:
            if init_segment_path:
                with open(init_segment_path, 'rb') as src:
                    shutil.copyfileobj(src, out)
            for p in paths:
                with open(p, 'rb') as src:
                    shutil.copyfileobj(src, out)

        # Step 2: remux so Duration/Cues reflect all clusters, not just the
        # first (MediaRecorder never writes a final Duration element, and
        # browsers/Whisper rely on it).
        fd, out_path = tempfile.mkstemp(suffix=output_suffix, prefix='merged_')
        os.close(fd)
        result = subprocess.run(
            ['ffmpeg', '-y', '-fflags', '+genpts',
             '-i', raw_path, '-c', 'copy', out_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise RuntimeError(
                f"ffmpeg remux failed: {result.stderr[:500]}"
            )
        return out_path
    finally:
        try:
            os.unlink(raw_path)
        except OSError:
            pass


def concat_audio_files(paths: list, output_suffix: str = '.webm') -> str:
    """Concatenate multiple audio files using ffmpeg concat demuxer.

    Args:
        paths: List of file paths to concatenate (in order).
        output_suffix: Extension for the output temp file.

    Returns:
        Path to the merged temp file.  Caller is responsible for cleanup.

    Raises:
        RuntimeError: If ffmpeg fails.
        ValueError: If paths is empty.
    """
    if not paths:
        raise ValueError("No audio files to concatenate")

    if len(paths) == 1:
        # Nothing to merge — return a copy so the caller can always unlink
        import tempfile
        fd, out_path = tempfile.mkstemp(suffix=output_suffix, prefix='merged_')
        os.close(fd)
        shutil.copy2(paths[0], out_path)
        return out_path

    import subprocess
    import tempfile

    concat_fd, concat_path = tempfile.mkstemp(suffix='.txt', prefix='concat_')
    try:
        with os.fdopen(concat_fd, 'w') as f:
            for p in paths:
                escaped = p.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        fd, out_path = tempfile.mkstemp(suffix=output_suffix, prefix='merged_')
        os.close(fd)

        result = subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
             '-i', concat_path, '-c', 'copy', out_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

        return out_path
    finally:
        try:
            os.unlink(concat_path)
        except OSError:
            pass


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
