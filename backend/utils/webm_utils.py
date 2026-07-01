"""
Utility functions for fragmented-media (Matroska/WebM and fMP4) audio
file handling.

Covers:
- Probing duration via ffprobe (`get_webm_duration`).
- Extracting a MediaRecorder session's init segment via a proper EBML
  element walk for WebM (`extract_webm_init_segment`,
  `find_first_cluster_offset`, `find_all_cluster_offsets`) or a flat
  ISOBMFF box walk for fMP4 (`extract_mp4_init_segment`).
- Persisting that init segment alongside a streaming session so later
  batches that don't include chunk 0 can still be remuxed into a valid
  file (`persist_init_segment` — format-aware, dispatches on file suffix).
- Concatenating MediaRecorder fragments (Matroska or fMP4) into a single
  valid file via binary append + a single ffmpeg remux
  (`concat_fragmented_media`), and concatenating multiple standalone
  audio files via the ffmpeg concat demuxer (`concat_audio_files`).
"""

import logging
import os
import pathlib
import shutil
import struct
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
        # A first byte of 0x00 would indicate a VINT of 9+ bytes (per the
        # spec, the length is derived from the leading zeros before the
        # first set bit). EBML caps VINT length at 8, so 0x00 is invalid.
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


WEBM_MAGIC = b'\x1aE\xdf\xa3'  # EBML header


def chunk_is_init_bearing(chunk_path: pathlib.Path) -> bool:
    """True if the chunk starts its own media stream (#124).

    A chunk N>0 that begins with an fMP4 `ftyp` box or a WebM EBML
    header came from a fresh MediaRecorder instance — i.e. the user
    resumed a recovered recording. Such chunks open a new "subsession"
    that must be transcribed separately from the chunks before it.
    """
    try:
        with open(chunk_path, 'rb') as f:
            head = f.read(8)
    except OSError:
        return False
    if len(head) < 8:
        return False
    return head[4:8] == b'ftyp' or head[:4] == WEBM_MAGIC


def init_segment_name(ext: str, index: int = None) -> str:
    """Filename for a persisted init segment.

    Chunk 0's init keeps the legacy name `init{ext}`; a resumed
    subsession starting at chunk N persists as `init.{N}{ext}` (#124).
    """
    return f"init{ext}" if index is None else f"init.{index}{ext}"


def persist_init_segment(
    chunk_path: pathlib.Path, chunk_dir: pathlib.Path,
    index: int = None,
) -> None:
    """Extract the init segment from an init-bearing chunk on disk and
    persist it at `chunk_dir/init{ext}.enc` (chunk 0) or
    `chunk_dir/init.{index}{ext}.enc` (a resumed subsession's first chunk,
    #124) — without `.enc` when encryption is disabled. `ext` matches the
    chunk's file extension (`.webm` or `.mp4`).

    Dispatches on the chunk's suffix: WebM goes through the EBML walker,
    MP4 through the ISOBMFF box walker. Both raise ValueError on malformed
    input, OSError on disk failure. The caller is responsible for deleting
    the offending chunk and surfacing an error — retaining a chunk 0 we
    can't extract the init from would let batch 1 succeed and batch 2 fail
    silently for want of an init segment.
    """
    # Local import avoids circular imports; encryption depends on this
    # module's sibling files.
    from backend.utils.encryption import encrypt_file

    ext = chunk_path.suffix.lower()
    with open(chunk_path, 'rb') as f:
        raw = f.read()
    if ext == '.mp4':
        init_bytes = extract_mp4_init_segment(raw)
    elif ext == '.webm':
        init_bytes = extract_webm_init_segment(raw)
    else:
        raise ValueError(f"Unsupported chunk extension: {ext}")

    init_path = chunk_dir / init_segment_name(ext, index)
    with open(init_path, 'wb') as f:
        f.write(init_bytes)
    try:
        # encrypt_file is a no-op returning the original path when
        # encryption is disabled; no return value to track. On success it
        # removes the plaintext init file and writes init{ext}.enc.
        encrypt_file(str(init_path))
    except Exception:
        # KMS outage or any other encrypt failure — don't leave a
        # plaintext init file on disk for a subsequent batch to pick up
        # and mistakenly treat as its own (unencrypted) init segment.
        try:
            init_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


def extract_mp4_init_segment(data: bytes) -> bytes:
    """Return the init segment of a Safari/WebKit MediaRecorder fMP4 blob.

    MediaRecorder on Safari with `audio/mp4` and a timeslice emits
    fragmented MP4: the first blob carries `ftyp` + `moov` (the init
    segment, ~652 bytes for typical AAC) followed by the first
    `moof`+`mdat` fragment; later blobs are bare `moof`+`mdat` fragments
    with timestamps absolute to the original recording. Like WebM, the
    init bytes never change over the lifetime of a single MediaRecorder
    recording, so we persist them once and prepend to future batches that
    don't start at chunk 0.

    Hardened to raise ValueError (never struct.error) on every parse
    failure, so the route handler at drafts.py upload_streaming_chunk
    catches and returns a clean 400 + `init_parse_failed`. Validates that
    a `moov` box was seen before the first `moof`/`mdat` (parallel to the
    WebM Tracks check) so a malformed chunk where the first box is `moof`
    can't yield an empty init segment that would silently corrupt batch 2.
    Rejects pathological box sizes (< 8, or < 16 for largesize) that would
    otherwise misalign subsequent reads.
    """
    off = 0
    saw_moov = False
    while off + 8 <= len(data):
        try:
            size = struct.unpack('>I', data[off:off+4])[0]
        except struct.error as e:
            raise ValueError(f"Truncated MP4 box header at offset {off}") from e
        box = data[off+4:off+8]
        if box in (b'moof', b'mdat'):
            if not saw_moov:
                raise ValueError(
                    "Init segment is missing a moov element — "
                    "chunk 0 is structurally malformed"
                )
            return data[:off]
        if box == b'moov':
            saw_moov = True
        if size == 1:  # 64-bit largesize
            if off + 16 > len(data):
                raise ValueError(f"Truncated 64-bit largesize at offset {off}")
            try:
                size = struct.unpack('>Q', data[off+8:off+16])[0]
            except struct.error as e:
                raise ValueError(f"Bad largesize at offset {off}") from e
            if size < 16:
                raise ValueError(f"Invalid largesize {size} at offset {off}")
        elif size < 8:
            raise ValueError(f"Invalid box size {size} at offset {off}")
        if size == 0:
            break
        off += size
    raise ValueError("No moof/mdat element found — chunk 0 is structurally malformed")


def concat_fragmented_media(paths: list,
                            init_segment_path: Optional[str] = None,
                            output_suffix: str = '.webm') -> str:
    """Concatenate MediaRecorder timeslice fragments into one valid file.

    Handles both Matroska/WebM and fragmented MP4 (fMP4) — they share the
    same architectural property: with a timeslice, MediaRecorder emits a
    sequence of byte-concatenable fragments of a single continuous stream.
    Only the first blob carries the init prefix (EBML/Segment/Tracks for
    WebM; ftyp+moov for fMP4); subsequent blobs are header-less fragment
    bodies (Matroska Clusters / MP4 moof+mdat) with timestamps absolute to
    the original recording. Per the respective byte-stream formats, those
    fragments concatenated in order as raw bytes form exactly one valid
    file. This function does that append, then runs a single ffmpeg remux
    pass to rewrite the container's duration metadata so the output is
    seekable and reports the correct length.

    For batches that don't include the first fragment of the recording,
    pass `init_segment_path` pointing at a previously-extracted init
    segment (see `extract_webm_init_segment` / `extract_mp4_init_segment`).
    Its bytes are binary-prepended.

    `output_suffix` should be `.webm` for Matroska inputs and `.mp4` for
    fMP4. The function body is format-agnostic — `+genpts` and `-c copy`
    don't care about container — but the suffix tells ffmpeg which muxer
    to use for the output.

    Returns the path to the merged output file. Caller is responsible for
    cleaning it up (parity with `concat_audio_files`).
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


def concat_audio_files(paths: list, output_suffix: str = '.webm',
                       reencode_codec: Optional[str] = None) -> str:
    """Concatenate multiple audio files into a single output.

    Args:
        paths: List of file paths to concatenate (in order).
        output_suffix: Extension for the output temp file.
        reencode_codec: When set (e.g. 'aac'), use the ffmpeg concat
            FILTER to decode each input independently, concatenate
            decoded sample buffers, and re-encode. Required for MP4
            sources: the concat demuxer + `-c copy` path mangles
            per-packet timestamps across fragmented-MP4 batch
            boundaries (output reports correct duration but most of
            the audio reads as silence). Operating on decoded samples
            sidesteps any demuxer-level PTS quirks. When None, uses
            the fast concat demuxer + `-c copy` (fine for WebM/Opus,
            whose Matroska cluster timestamps are robust).

    Returns:
        Path to the merged temp file. Caller is responsible for cleanup.

    Raises:
        RuntimeError: If ffmpeg fails.
        ValueError: If paths is empty.
    """
    if not paths:
        raise ValueError("No audio files to concatenate")

    if len(paths) == 1 and not reencode_codec:
        # Nothing to merge — return a copy so the caller can always unlink
        import tempfile
        fd, out_path = tempfile.mkstemp(suffix=output_suffix, prefix='merged_')
        os.close(fd)
        shutil.copy2(paths[0], out_path)
        return out_path

    import subprocess
    import tempfile

    if reencode_codec:
        # WAV-intermediate path. Both the concat demuxer (-c copy) and
        # the concat filter (-filter_complex) yielded mostly-silent
        # output for fragmented-MP4 batches from MediaRecorder, even
        # though each batch decodes correctly on its own. The
        # symptomatic Qavg of 2231 from a direct concat-filter pass
        # means the encoder was eating zero/near-zero samples — the
        # batches' codec extradata or per-frame priming gets confused
        # when fed sequentially through libavcodec.
        #
        # Decoding each batch to PCM WAV separately sidesteps the
        # whole extradata / priming dance: each batch is decoded in
        # its own libavcodec context, samples are written verbatim,
        # then the WAV concat-demux + AAC encode is timestamp-trivial
        # because PCM has no codec delay.
        wav_dir = tempfile.mkdtemp(prefix='wav_concat_')
        try:
            wav_paths = []
            for i, p in enumerate(paths):
                wav_path = os.path.join(wav_dir, f'part_{i:04d}.wav')
                r = subprocess.run(
                    ['ffmpeg', '-y', '-i', p,
                     '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '1',
                     wav_path],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode != 0:
                    raise RuntimeError(
                        f"ffmpeg decode-to-wav failed for {p}: "
                        f"{r.stderr[:500]}"
                    )
                wav_paths.append(wav_path)

            list_path = os.path.join(wav_dir, 'list.txt')
            with open(list_path, 'w') as f:
                for w in wav_paths:
                    escaped = w.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            fd, out_path = tempfile.mkstemp(suffix=output_suffix,
                                            prefix='merged_')
            os.close(fd)
            r = subprocess.run(
                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                 '-i', list_path,
                 '-c:a', reencode_codec, '-b:a', '128k', out_path],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode != 0:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
                raise RuntimeError(
                    f"ffmpeg WAV-concat encode failed: {r.stderr[:500]}"
                )
            return out_path
        finally:
            shutil.rmtree(wav_dir, ignore_errors=True)

    # concat demuxer path — fast stream-copy, used for WebM
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
            capture_output=True, text=True, timeout=600,
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
