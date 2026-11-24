"""ffmpeg and ffprobe read stored and uploaded audio only as one of the
containers the apps send (webm_utils.AUDIO_INPUT_FORMATS): every format the
apps record or upload in is measured, cut and merged as before, and a
playlist that names another file is not read.

Needs ffmpeg on PATH (CI installs it)."""
import logging
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="needs ffmpeg")

LOG = logging.getLogger("test")

# (file name, ffmpeg output arguments): what browsers, the iPhone app and
# uploads send.
FORMATS = [
    ("chrome.webm", ["-c:a", "libopus"]),
    ("iphone.mp4", ["-c:a", "aac", "-movflags",
                    "frag_keyframe+empty_moov+default_base_moof"]),
    ("memo.m4a", ["-c:a", "aac"]),
    ("song.mp3", ["-c:a", "libmp3lame"]),
    ("talk.wav", ["-c:a", "pcm_s16le"]),
    ("talk.flac", ["-c:a", "flac"]),
    ("firefox.ogg", ["-c:a", "libopus"]),
    ("vorbis.oga", ["-c:a", "libvorbis"]),
    ("raw.aac", ["-c:a", "aac", "-f", "adts"]),
    ("old.mpeg", ["-c:a", "libmp3lame", "-f", "mp3"]),
]


def _tone(path, seconds=2.0, extra=("-c:a", "libmp3lame")):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", *extra, str(path)],
        check=True)
    return path


def _playlist(path, target):
    path.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:3.0,\n"
        f"{target}\n#EXT-X-ENDLIST\n")
    return path


@pytest.mark.parametrize("name,args", FORMATS, ids=[f[0] for f in FORMATS])
def test_formats_the_apps_send_are_measured(tmp_path, name, args):
    from backend.utils.audio_processing import (
        container_seconds, decoded_audio_seconds,
    )
    from backend.utils.webm_utils import get_webm_duration
    path = _tone(tmp_path / name, extra=args)
    assert decoded_audio_seconds(path) == pytest.approx(2.0, abs=0.15)
    # The length the container states (how ffmpeg releases read a
    # fragmented MP4's differs): read, not refused.
    assert container_seconds(path) > 0
    assert get_webm_duration(str(path)) > 0


@pytest.mark.parametrize("name,args", FORMATS, ids=[f[0] for f in FORMATS])
def test_formats_the_apps_send_are_cut_into_pieces(tmp_path, name, args):
    from backend.utils.audio_processing import split_audio_for_transcription
    path = _tone(tmp_path / name, seconds=3.0, extra=args)
    pieces, cut = split_audio_for_transcription(path, 2, 60, logger=LOG)
    try:
        assert len(pieces) == 2 and not cut
        assert sum(s for _, s in pieces) == pytest.approx(3.0, abs=0.2)
    finally:
        if pieces:
            shutil.rmtree(str(pieces[0][0].rsplit("/", 1)[0]),
                          ignore_errors=True)


def test_recorded_mp4_batches_merge(tmp_path):
    from backend.utils.audio_processing import decoded_audio_seconds
    from backend.utils.webm_utils import concat_audio_files
    parts = [str(_tone(tmp_path / f"batch_{i}.mp4", extra=FORMATS[1][1]))
             for i in range(2)]
    merged = concat_audio_files(parts, output_suffix=".mp4",
                                reencode_codec="aac")
    try:
        assert decoded_audio_seconds(merged) == pytest.approx(4.0, abs=0.2)
    finally:
        shutil.os.unlink(merged)


def test_recorded_webm_files_merge(tmp_path):
    from backend.utils.audio_processing import decoded_audio_seconds
    from backend.utils.webm_utils import concat_audio_files
    parts = [str(_tone(tmp_path / f"chunk_{i}.webm", extra=FORMATS[0][1]))
             for i in range(2)]
    merged = concat_audio_files(parts, output_suffix=".webm")
    try:
        assert decoded_audio_seconds(merged) == pytest.approx(4.0, abs=0.2)
    finally:
        shutil.os.unlink(merged)


def test_playlist_naming_another_file_is_not_read(tmp_path):
    from backend.utils.audio_processing import (
        container_seconds, decoded_audio_seconds,
        split_audio_for_transcription,
    )
    from backend.utils.webm_utils import get_webm_duration
    (tmp_path / "other").mkdir()
    other = _tone(tmp_path / "other" / "original.mp3")
    listing = _playlist(tmp_path / "list.m3u8", other)

    assert decoded_audio_seconds(listing) is None
    assert container_seconds(listing) is None
    assert get_webm_duration(str(listing)) is None
    assert split_audio_for_transcription(listing, 2, 60, logger=LOG) == (
        [], False)


def test_merge_does_not_read_a_playlist_among_its_files(tmp_path):
    from backend.utils.audio_processing import decoded_audio_seconds
    from backend.utils.webm_utils import concat_audio_files
    (tmp_path / "other").mkdir()
    other = _tone(tmp_path / "other" / "original.mp3", seconds=3.0)
    first = _tone(tmp_path / "chunk_0.mp3", seconds=2.0)
    listing = _playlist(tmp_path / "chunk_1.m3u8", other)

    # Stream copy: the merge fails, or holds the first file's audio only
    # (ffmpeg releases differ in how a list that stops early ends).
    try:
        merged = concat_audio_files([str(first), str(listing)],
                                    output_suffix=".mp3")
    except RuntimeError:
        merged = None
    if merged is not None:
        try:
            assert decoded_audio_seconds(merged) == pytest.approx(
                2.0, abs=0.2)
        finally:
            shutil.os.unlink(merged)
    # Re-encoded through WAV: the playlist is not decoded.
    with pytest.raises(RuntimeError):
        concat_audio_files([str(first), str(listing)],
                           output_suffix=".mp4", reencode_codec="aac")
