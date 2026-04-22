"""Tests for EBML init-segment extraction in webm_utils.

The motivation is documented on extract_webm_init_segment: MediaRecorder emits
Matroska fragments and only chunk 0 carries the EBML/Segment/Tracks prefix, so
the batching pipeline needs to extract that prefix from chunk 0 and prepend it
to any later batch to produce a parseable WebM. The prior implementation did
a byte-pattern search for the Cluster magic (0x1F 0x43 0xB6 0x75) which could
be fooled by those bytes appearing inside other EBML elements. These tests
pin the structural-parse behavior.
"""

import json
import shutil
import subprocess

import pytest

from backend.utils.webm_utils import (
    concat_webm_fragments,
    extract_webm_init_segment,
    find_first_cluster_offset,
)


# --- tiny EBML encoder helpers ------------------------------------------------

def _encode_size_vint(value: int, length: int = None) -> bytes:
    """Encode `value` as an EBML size VINT. If length is None, use minimal."""
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - 1:
            length += 1
    marker = 1 << (7 * length)
    return (marker | value).to_bytes(length, 'big')


def _encode_unknown_size(length: int = 1) -> bytes:
    """The reserved 'size unknown' VINT: all data bits set after the marker."""
    marker = 1 << (7 * length)
    data_bits = (1 << (7 * length)) - 1
    return (marker | data_bits).to_bytes(length, 'big')


def _element(id_hex: str, body: bytes, unknown_size: bool = False) -> bytes:
    id_bytes = bytes.fromhex(id_hex)
    size = _encode_unknown_size() if unknown_size else _encode_size_vint(len(body))
    return id_bytes + size + body


# Element IDs we use below
EBML_HEADER_ID = '1A45DFA3'
SEGMENT_ID = '18538067'
INFO_ID = '1549A966'
TRACKS_ID = '1654AE6B'
CLUSTER_ID = '1F43B675'
CODEC_PRIVATE_ID = '63A2'


# --- tests --------------------------------------------------------------------

def test_finds_first_cluster_in_fixed_size_segment():
    info = _element(INFO_ID, b'\x00' * 10)
    tracks = _element(TRACKS_ID, b'\x00' * 20)
    cluster = _element(CLUSTER_ID, b'\xAA' * 15)
    segment = _element(SEGMENT_ID, info + tracks + cluster)
    header = _element(EBML_HEADER_ID, b'\x00' * 8)
    buf = header + segment

    offset = find_first_cluster_offset(buf)
    assert offset == len(buf) - len(cluster)
    assert buf[offset:offset + 4] == bytes.fromhex(CLUSTER_ID)


def test_handles_unknown_size_segment_from_media_recorder():
    """MediaRecorder emits Segment with an unknown-size VINT (0xFF) because
    the total recording length isn't known when streaming starts. The walker
    must treat that as 'parse children until we find a Cluster'."""
    info = _element(INFO_ID, b'\x00' * 10)
    tracks = _element(TRACKS_ID, b'\x00' * 20)
    cluster1 = _element(CLUSTER_ID, b'\xAA' * 12)
    cluster2 = _element(CLUSTER_ID, b'\xBB' * 12)
    segment = _element(
        SEGMENT_ID, info + tracks + cluster1 + cluster2, unknown_size=True,
    )
    header = _element(EBML_HEADER_ID, b'\x00' * 8)
    buf = header + segment

    offset = find_first_cluster_offset(buf)
    # Expected: right before cluster1 (not cluster2)
    expected = len(header) + len(bytes.fromhex(SEGMENT_ID)) + 1 + len(info) + len(tracks)
    assert offset == expected


def test_ignores_cluster_magic_inside_codec_private():
    """Regression guard: the old byte-pattern search mistook 0x1F 0x43 0xB6
    0x75 inside a CodecPrivate payload for the start of a Cluster element and
    truncated the header. The structural walker must skip past Tracks' body
    without looking at its bytes as element IDs."""
    # Bytes identical to the Cluster element ID embedded inside CodecPrivate
    codec_private = _element(
        CODEC_PRIVATE_ID, b'\x1F\x43\xB6\x75\x00\x00\x00\x00',
    )
    tracks_with_cp = _element(TRACKS_ID, codec_private)
    cluster = _element(CLUSTER_ID, b'\xCC' * 10)
    segment = _element(SEGMENT_ID, tracks_with_cp + cluster)
    header = _element(EBML_HEADER_ID, b'\x00' * 8)
    buf = header + segment

    offset = find_first_cluster_offset(buf)
    # The "fake" cluster magic inside codec_private is at an earlier offset.
    # We must return the later one — the real Cluster element.
    real_cluster_pos = len(buf) - len(cluster)
    assert offset == real_cluster_pos


def test_extract_init_segment_returns_bytes_before_cluster():
    info = _element(INFO_ID, b'\x01' * 5)
    tracks = _element(TRACKS_ID, b'\x02' * 7)
    cluster = _element(CLUSTER_ID, b'\xFF' * 20)
    segment = _element(SEGMENT_ID, info + tracks + cluster)
    header = _element(EBML_HEADER_ID, b'\x03' * 4)
    buf = header + segment

    init = extract_webm_init_segment(buf)
    assert buf.startswith(init)
    assert len(init) == len(buf) - len(cluster)
    # The init segment should end right before the cluster's ID bytes
    assert init + cluster == buf


def test_raises_on_buffer_with_no_segment():
    header = _element(EBML_HEADER_ID, b'\x00' * 8)
    with pytest.raises(ValueError, match="No Segment"):
        find_first_cluster_offset(header)


def test_raises_on_segment_with_no_cluster():
    info = _element(INFO_ID, b'\x00' * 5)
    tracks = _element(TRACKS_ID, b'\x00' * 5)
    segment = _element(SEGMENT_ID, info + tracks)
    header = _element(EBML_HEADER_ID, b'\x00' * 4)
    buf = header + segment

    with pytest.raises(ValueError, match="No Cluster"):
        find_first_cluster_offset(buf)


# --- integration tests for concat_webm_fragments -----------------------------
# These exercise the full binary-append + ffmpeg remux pipeline and require
# ffmpeg on PATH; skipped locally if absent. CI installs ffmpeg (see ci.yml).

requires_ffmpeg = pytest.mark.skipif(
    shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,
    reason="ffmpeg/ffprobe not available on PATH",
)


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_format', '-of', 'json', path],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)['format']['duration'])


@pytest.fixture
def webm_fixture(tmp_path):
    """Produce a real WebM bytestring via ffmpeg. 3 s sine wave, Opus/WebM —
    mirrors the codec MediaRecorder uses in Chrome."""
    out_path = tmp_path / "source.webm"
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi', '-i',
         'sine=frequency=440:duration=3',
         '-c:a', 'libopus', '-b:a', '64k', str(out_path)],
        capture_output=True, check=True,
    )
    return out_path.read_bytes()


@requires_ffmpeg
def test_concat_single_fragment_including_init(tmp_path, webm_fixture):
    """Batch-1 path: the fragment list includes chunk 0, so no separate init
    segment needs prepending. concat_webm_fragments should produce a valid
    WebM whose duration matches the source."""
    chunk_0 = tmp_path / "chunk_0000.webm"
    chunk_0.write_bytes(webm_fixture)

    out = concat_webm_fragments([str(chunk_0)])

    assert _ffprobe_duration(out) == pytest.approx(3.0, abs=0.2)


@requires_ffmpeg
def test_concat_with_init_segment_for_batch_without_chunk_zero(
        tmp_path, webm_fixture):
    """Batch-2+ path: the fragment list does NOT include chunk 0, so the
    caller supplies init_segment_path with the cached header. Split the
    source at the first-cluster boundary to simulate this: `init.webm`
    holds everything up to the first Cluster; `body.webm` holds the
    cluster data. Feeding body alone would fail; feeding body with the
    init segment prepended must produce a valid WebM."""
    split = find_first_cluster_offset(webm_fixture)
    init_path = tmp_path / "init.webm"
    body_path = tmp_path / "chunk_0020.webm"
    init_path.write_bytes(webm_fixture[:split])
    body_path.write_bytes(webm_fixture[split:])

    out = concat_webm_fragments(
        [str(body_path)], init_segment_path=str(init_path),
    )

    assert _ffprobe_duration(out) == pytest.approx(3.0, abs=0.2)


@requires_ffmpeg
def test_concat_without_init_on_body_only_input_fails(
        tmp_path, webm_fixture):
    """Guard: confirm the batch-boundary scenario genuinely requires the
    init prepend. A body-only input (no EBML header) should fail the
    ffmpeg remux — otherwise Test B's success would be unconvincing."""
    split = find_first_cluster_offset(webm_fixture)
    body_path = tmp_path / "chunk_0020.webm"
    body_path.write_bytes(webm_fixture[split:])

    with pytest.raises(RuntimeError, match="ffmpeg"):
        concat_webm_fragments([str(body_path)])
