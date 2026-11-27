"""WAV and FLAC files are converted to MP3 file to file by ffmpeg
(utils/audio_processing.compress_audio_if_needed): the decoded audio never
sits in the worker's memory, the conversion stops at COMPRESS_MAX_SECONDS,
and a file ffmpeg cannot convert is handed on unchanged.

Needs ffmpeg on PATH (CI installs it)."""
import json
import logging
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="needs ffmpeg")

LOG = logging.getLogger("test")

# Ten minutes of 48 kHz stereo silence: about 100 KB as FLAC, 115 MB as
# decoded 16-bit samples.
MINUTES = 10
DECODED_BYTES = MINUTES * 60 * 48000 * 2 * 2

CHILD = textwrap.dedent("""
    import json, logging, pathlib, resource, sys
    def peak():
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return v if sys.platform == "darwin" else v * 1024
    from backend.utils.audio_processing import compress_audio_if_needed
    before = peak()
    out = compress_audio_if_needed(pathlib.Path(sys.argv[1]),
                                   logging.getLogger("t"))
    print(json.dumps({"before": before, "after": peak(), "out": str(out)}))
""")


def _silence(path, seconds, codec):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=48000:cl=stereo", "-t", str(seconds),
         "-c:a", codec, str(path)], check=True)
    return path


def test_flac_conversion_keeps_worker_memory_far_below_decoded_size(
        tmp_path):
    flac = _silence(tmp_path / "long.flac", MINUTES * 60, "flac")
    assert flac.stat().st_size < 1024 * 1024

    import backend
    repo_root = str(pathlib.Path(backend.__file__).resolve().parent.parent)
    res = subprocess.run([sys.executable, "-c", CHILD, str(flac)],
                         capture_output=True, text=True, cwd=repo_root,
                         check=True)
    data = json.loads(res.stdout.strip().splitlines()[-1])
    growth = data["after"] - data["before"]
    assert growth < DECODED_BYTES / 4, growth
    out = pathlib.Path(data["out"])
    assert out.suffix == ".mp3" and out.exists()

    from backend.utils.audio_processing import decoded_audio_seconds
    assert decoded_audio_seconds(out) == pytest.approx(MINUTES * 60, abs=1)


def test_wav_is_converted_to_mp3_beside_it(tmp_path):
    from backend.utils.audio_processing import (
        compress_audio_if_needed, decoded_audio_seconds,
    )
    wav = _silence(tmp_path / "talk.wav", 3, "pcm_s16le")
    out = compress_audio_if_needed(wav, LOG)
    assert out == tmp_path / "talk.mp3"
    assert decoded_audio_seconds(out) == pytest.approx(3, abs=0.2)
    assert wav.exists()


def test_conversion_stops_at_the_bound(tmp_path, monkeypatch):
    from backend.utils import audio_processing
    monkeypatch.setattr(audio_processing, "COMPRESS_MAX_SECONDS", 4)
    flac = _silence(tmp_path / "long.flac", 10, "flac")
    out = audio_processing.compress_audio_if_needed(flac, LOG)
    assert out.suffix == ".mp3"
    assert audio_processing.decoded_audio_seconds(out) == pytest.approx(
        4, abs=0.2)


def test_bound_is_one_second_past_the_longest_upload_transcribed():
    from backend.utils.audio_processing import COMPRESS_MAX_SECONDS
    from backend.utils.recording_limits import MAX_UPLOAD_TRANSCRIBED_SECONDS
    assert COMPRESS_MAX_SECONDS == MAX_UPLOAD_TRANSCRIBED_SECONDS + 1


def test_file_that_cannot_be_converted_is_returned_unchanged(tmp_path):
    from backend.utils.audio_processing import compress_audio_if_needed
    wav = tmp_path / "broken.wav"
    wav.write_bytes(b"not audio at all" * 100)
    assert compress_audio_if_needed(wav, LOG) == wav
    assert not (tmp_path / "broken.mp3").exists()


def test_compressed_formats_are_not_converted(tmp_path):
    from backend.utils.audio_processing import compress_audio_if_needed
    m4a = tmp_path / "voice.m4a"
    m4a.write_bytes(b"\0" * 10)
    assert compress_audio_if_needed(m4a, LOG) == m4a
