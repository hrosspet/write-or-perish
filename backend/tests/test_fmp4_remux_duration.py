"""A remuxed iPhone recording batch says as long as the audio it holds.

The fixtures in fixtures/ios_fmp4/ are the iPhone app's own output: its
FMP4SegmentWriter + SegmentPackager (ios/Loore/Audio/Recording), built for
macOS, fed a 440 Hz tone with 2 s segments. chunk_0000 is the init segment
+ the first segment, chunk_0001 the next segment. As on the phone, every
`moof` holds one `trun` per second of audio (three here).

ffmpeg 4.2 (Ubuntu 20.04, production in October 2026) started every `trun`
at its fragment's `tfdt`, so a batch's last chunk collapsed into its first
second: a one-chunk batch of 15 s came out saying ~1 s, and the player
never reached the recording's next file. On ffmpeg 4.2 the plain remux of
these fixtures says 0.98 s for chunk_0000 alone and 3.05 s for both chunks
(audio: 2.07 s and 4.07 s). The ffmpeg in CI and in Docker places the runs
correctly either way; the duration tests pin the result on whatever ffmpeg
runs them, and the tfdt and fallback tests pin the two mechanisms that make
it right on 4.2.
"""
import pathlib
import shutil
import struct
import subprocess

import pytest

from backend.utils import webm_utils
from backend.utils.webm_utils import (
    concat_fragmented_media,
    drop_fragment_decode_times,
    extract_mp4_init_segment,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ios_fmp4"
CHUNK_0 = FIXTURES / "chunk_0000.mp4"
CHUNK_1 = FIXTURES / "chunk_0001.mp4"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,
    reason="ffmpeg/ffprobe not available on PATH",
)


def _boxes(data, off=0, end=None, path=()):
    """(path, type, offset) of every top-level, moof and traf box, depth first."""
    end = len(data) if end is None else end
    out = []
    while off + 8 <= end:
        size = struct.unpack('>I', data[off:off + 4])[0]
        box = data[off + 4:off + 8].decode('latin1')
        out.append((path, box, off))
        if box in ('moof', 'traf'):
            out += _boxes(data, off + 8, off + size, path + (box,))
        off += size
    return out


def _merge(paths, init=None):
    out = concat_fragmented_media([str(p) for p in paths],
                                  init_segment_path=init,
                                  output_suffix='.mp4')
    try:
        durations = webm_utils._mp4_durations(out)
    finally:
        pathlib.Path(out).unlink()
    assert durations is not None, "ffprobe could not read the output"
    return durations


def test_fixture_has_several_runs_per_fragment():
    """The fixtures are only useful while they have the shape that broke."""
    for chunk in (CHUNK_0, CHUNK_1):
        boxes = _boxes(chunk.read_bytes())
        assert sum(1 for _, box, _ in boxes if box == 'trun') >= 2
        assert sum(1 for _, box, _ in boxes if box == 'tfdt') == 1


def test_drop_fragment_decode_times_renames_only_tfdt():
    data = CHUNK_0.read_bytes()
    out = drop_fragment_decode_times(data)

    assert len(out) == len(data)
    before, after = _boxes(data), _boxes(out)
    assert not [b for b in after if b[1] == 'tfdt']
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert [(a[1], b[1]) for a, b in changed] == [('tfdt', 'free')]
    assert changed[0][0][0] == ('moof', 'traf')
    # The init segment is untouched, so it still parses the same.
    assert extract_mp4_init_segment(out) == extract_mp4_init_segment(data)


def test_drop_fragment_decode_times_leaves_other_bytes_alone():
    assert drop_fragment_decode_times(b'') == b''
    junk = b'\x00\x00\x00\x03tfdt-not-a-box'
    assert drop_fragment_decode_times(junk) == junk


@requires_ffmpeg
def test_one_chunk_batch_says_as_long_as_its_audio():
    """Peter's case: a resumed recording, one chunk per stream."""
    container, audio = _merge([CHUNK_0])
    assert audio == pytest.approx(2.07, abs=0.1)
    assert container == pytest.approx(audio, abs=0.1)


@requires_ffmpeg
def test_batch_from_chunk_0_says_as_long_as_its_audio():
    container, audio = _merge([CHUNK_0, CHUNK_1])
    assert audio == pytest.approx(4.07, abs=0.1)
    assert container == pytest.approx(audio, abs=0.1)


@requires_ffmpeg
def test_later_batch_with_init_segment_says_as_long_as_its_audio(tmp_path):
    """A batch that starts after chunk 0 gets chunk 0's init prepended."""
    init = tmp_path / "init.mp4"
    init.write_bytes(extract_mp4_init_segment(CHUNK_0.read_bytes()))
    container, audio = _merge([CHUNK_1], init=str(init))
    assert audio == pytest.approx(2.0, abs=0.1)
    assert container == pytest.approx(audio, abs=0.1)


@requires_ffmpeg
def test_wrong_container_duration_is_rebuilt_from_decoded_audio(monkeypatch):
    """When the remux still comes out wrong (the first check sees what
    ffmpeg 4.2 wrote), the file is rebuilt from the decoded audio."""
    real = webm_utils._mp4_durations
    calls = []

    def first_check_sees_ffmpeg_42(path):
        calls.append(path)
        container, audio = real(path)
        return (0.98, audio) if len(calls) == 1 else (container, audio)

    monkeypatch.setattr(webm_utils, '_mp4_durations',
                        first_check_sees_ffmpeg_42)
    out = concat_fragmented_media([str(CHUNK_0), str(CHUNK_1)],
                                  output_suffix='.mp4')
    try:
        assert len(calls) == 1
        container, audio = real(out)
        assert audio == pytest.approx(4.07, abs=0.15)
        assert container == pytest.approx(audio, abs=0.1)
    finally:
        pathlib.Path(out).unlink()


@requires_ffmpeg
def test_right_file_is_not_rebuilt(monkeypatch):
    """A file that says the right length stays the stream copy."""
    ran = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        ran.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(webm_utils.subprocess, 'run', spy)
    out = concat_fragmented_media([str(CHUNK_0), str(CHUNK_1)],
                                  output_suffix='.mp4')
    pathlib.Path(out).unlink()
    assert not any('pcm_s16le' in cmd for cmd in ran)
