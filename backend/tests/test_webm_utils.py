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
    find_all_cluster_offsets,
    find_first_cluster_offset,
    persist_init_segment,
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


def test_extract_init_rejects_segment_missing_tracks():
    """A malformed chunk 0 with no Tracks element would pass the cluster walk
    but produce a WebM with no decodable stream. extract_webm_init_segment
    must refuse it so the upload fails on chunk 0 rather than at batch 2
    ffmpeg remux time."""
    info = _element(INFO_ID, b'\x00' * 5)
    cluster = _element(CLUSTER_ID, b'\x00' * 10)
    segment = _element(SEGMENT_ID, info + cluster)  # No Tracks
    header = _element(EBML_HEADER_ID, b'\x00' * 4)
    buf = header + segment

    with pytest.raises(ValueError, match="Tracks"):
        extract_webm_init_segment(buf)


def test_find_all_cluster_offsets_multi_cluster():
    info = _element(INFO_ID, b'\x00' * 4)
    tracks = _element(TRACKS_ID, b'\x00' * 4)
    c1 = _element(CLUSTER_ID, b'\xAA' * 6)
    c2 = _element(CLUSTER_ID, b'\xBB' * 6)
    c3 = _element(CLUSTER_ID, b'\xCC' * 6)
    segment = _element(SEGMENT_ID, info + tracks + c1 + c2 + c3)
    header = _element(EBML_HEADER_ID, b'\x00' * 4)
    buf = header + segment

    offsets = find_all_cluster_offsets(buf)
    assert len(offsets) == 3
    # Each offset should land on the Cluster element's ID bytes
    for off in offsets:
        assert buf[off:off + 4] == bytes.fromhex(CLUSTER_ID)


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
    """Produce a real multi-cluster WebM bytestring via ffmpeg.

    Uses -cluster_time_limit 1000 (ms) so the 5-second source is emitted as
    multiple ~1-second Clusters, matching what a real 15s-timeslice
    MediaRecorder session looks like (one Cluster per timeslice fragment).
    """
    out_path = tmp_path / "source.webm"
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi', '-i',
         'sine=frequency=440:duration=5',
         '-c:a', 'libopus', '-b:a', '64k',
         '-cluster_time_limit', '1000',
         str(out_path)],
        capture_output=True, check=True,
    )
    data = out_path.read_bytes()
    # Sanity: the fixture is only useful if it exercises multi-cluster
    # concat. If ffmpeg ever collapses to a single cluster, tests below
    # would silently degenerate — fail loudly here instead.
    assert len(find_all_cluster_offsets(data)) >= 3, (
        f"fixture has only {len(find_all_cluster_offsets(data))} clusters; "
        "multi-cluster concat not exercised"
    )
    return data


@requires_ffmpeg
def test_concat_single_fragment_including_init(tmp_path, webm_fixture):
    """Batch-1 path: the fragment list includes chunk 0, so no separate init
    segment needs prepending. Whole source as one file in, valid WebM out
    with duration matching the source."""
    chunk_0 = tmp_path / "chunk_0000.webm"
    chunk_0.write_bytes(webm_fixture)

    out = concat_webm_fragments([str(chunk_0)])

    assert _ffprobe_duration(out) == pytest.approx(5.0, abs=0.3)


@requires_ffmpeg
def test_concat_multi_cluster_batch_with_init_segment(tmp_path, webm_fixture):
    """Batch-2+ path, real shape: the fragment list is multiple cluster-only
    files (one per real MediaRecorder chunk), none of them contain an EBML
    header, and the caller supplies init_segment_path with the cached
    chunk-0 prefix. Splits the multi-cluster fixture at each Cluster
    boundary so each fragment is exactly one Cluster — matching the
    real-world 20-fragments-per-batch shape."""
    cluster_offsets = find_all_cluster_offsets(webm_fixture)
    assert len(cluster_offsets) >= 3

    init_path = tmp_path / "init.webm"
    init_path.write_bytes(webm_fixture[:cluster_offsets[0]])

    # One file per cluster. Element boundaries are at cluster_offsets[i];
    # the last cluster runs to end of buffer.
    fragment_paths = []
    boundaries = cluster_offsets + [len(webm_fixture)]
    for i in range(len(cluster_offsets)):
        fpath = tmp_path / f"chunk_{i + 20:04d}.webm"
        fpath.write_bytes(webm_fixture[boundaries[i]:boundaries[i + 1]])
        fragment_paths.append(str(fpath))

    out = concat_webm_fragments(
        fragment_paths, init_segment_path=str(init_path),
    )

    # Duration should cover all clusters — i.e. the full source
    assert _ffprobe_duration(out) == pytest.approx(5.0, abs=0.3)


@requires_ffmpeg
def test_persist_init_segment_writes_init_file(tmp_path, webm_fixture):
    """Happy path for the chunk-0 upload glue: given a real WebM blob,
    persist_init_segment must write an init.webm(.enc) file on disk. Guards
    against mutations that drop the write or the encrypt_file call."""
    chunk_path = tmp_path / "chunk_0000.webm"
    chunk_path.write_bytes(webm_fixture)

    persist_init_segment(chunk_path, tmp_path)

    init_files = list(tmp_path.glob("init.webm*"))
    assert len(init_files) == 1, (
        f"expected exactly one init.webm file, got {init_files}"
    )
    assert init_files[0].stat().st_size > 0


def test_persist_init_segment_raises_on_unparseable_bytes(tmp_path):
    """Error path for the chunk-0 upload glue: if the file doesn't parse as
    WebM, persist_init_segment must raise rather than silently succeed."""
    chunk_path = tmp_path / "chunk_0000.webm"
    chunk_path.write_bytes(b"this is not a webm file")

    with pytest.raises(ValueError):
        persist_init_segment(chunk_path, tmp_path)

    # And no init file should have been left behind
    assert not list(tmp_path.glob("init.webm*"))


def test_persist_init_segment_raises_on_missing_tracks(tmp_path):
    """Chunk 0 with a Segment but no Tracks element must be rejected by
    persist_init_segment even though the Cluster walk would succeed — the
    resulting init segment has no decodable stream description."""
    info = _element(INFO_ID, b'\x00' * 4)
    cluster = _element(CLUSTER_ID, b'\x00' * 8)
    segment = _element(SEGMENT_ID, info + cluster)
    header = _element(EBML_HEADER_ID, b'\x00' * 4)
    chunk_path = tmp_path / "chunk_0000.webm"
    chunk_path.write_bytes(header + segment)

    with pytest.raises(ValueError, match="Tracks"):
        persist_init_segment(chunk_path, tmp_path)


@requires_ffmpeg
def test_concat_without_init_on_body_only_input_fails(
        tmp_path, webm_fixture):
    """Guard: confirm the batch-boundary test actually exercises the init
    prepend. A fragment list of cluster-only bytes with no init_segment_path
    must fail the ffmpeg remux — otherwise the previous test's success
    could come from ffmpeg being lenient rather than from the prepend doing
    its job."""
    split = find_first_cluster_offset(webm_fixture)
    body_path = tmp_path / "chunk_0020.webm"
    body_path.write_bytes(webm_fixture[split:])

    with pytest.raises(RuntimeError, match="ffmpeg"):
        concat_webm_fragments([str(body_path)])
