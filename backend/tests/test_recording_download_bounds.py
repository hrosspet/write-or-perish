"""Recorded audio of a streaming recording (routes/nodes.py):

- GET /audio-download prepares one merged download at a time per account
  and at most AUDIO_DOWNLOAD_SLOTS on the server; a request beyond those
  is answered 429 ``download_busy``, and the holds end with the response,
  also when the merge fails. No temporary file (decrypted copies, the
  merge, the MP3) is left once the response is closed.
- GET /audio-chunks measures each recorded file once and then reads its
  length from Redis, until the file changes.

decrypt_file_to_temp and subprocess.run are replaced by counting fakes,
and Redis by tests/fake_redis: no ffmpeg, KMS or network is used."""
import os
import shutil
import subprocess
import tempfile
import types

import pytest

from backend.tests.fake_redis import install
from backend.tests.test_route_access_checks import (  # noqa: F401 (fixtures)
    app, data, _call, _node, _db, User,
)

N_FILES = 4


@pytest.fixture
def rec(app, data, monkeypatch, tmp_path):  # noqa: F811
    import backend.routes.nodes as nodes_mod

    counts = types.SimpleNamespace(decrypt=0, ffmpeg=0, ffprobe=0,
                                   on_ffmpeg=None, fail_mp3=False,
                                   fail_decrypt=set(), temp=[])

    def fake_decrypt(path):
        if os.path.basename(path) in counts.fail_decrypt:
            raise ValueError("cannot decrypt")
        counts.decrypt += 1
        fd, tmp = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        shutil.copyfile(path, tmp)
        counts.temp.append(tmp)
        return tmp

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "ffprobe":
            counts.ffprobe += 1
            return types.SimpleNamespace(returncode=0, stdout="300.0\n",
                                         stderr="")
        assert cmd[0] == "ffmpeg", cmd
        counts.ffmpeg += 1
        if counts.on_ffmpeg is not None:
            hook, counts.on_ffmpeg = counts.on_ffmpeg, None
            hook()
        if counts.fail_mp3 and cmd[-1].endswith(".mp3"):
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="failed")
        with open(cmd[-1], "wb") as out:
            out.write(b"merged-output")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(nodes_mod, "decrypt_file_to_temp", fake_decrypt)
    monkeypatch.setattr(subprocess, "run", fake_run)
    counts.redis = install(monkeypatch)
    counts.nodes = nodes_mod
    # Every temporary file of a download is made here.
    counts.tmp = tmp_path / "tmp"
    counts.tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(counts.tmp))

    def recording(owner, privacy_level="private"):
        node = _node(owner, "RECORDING", privacy_level=privacy_level,
                     streaming_transcription=True)
        folder = app.media_root / f"nodes/{owner.id}/{node.id}"
        folder.mkdir(parents=True)
        for i in range(N_FILES):
            (folder / f"batch_{i:04d}.mp4.enc").write_bytes(b"ciphertext")
        node.folder = folder
        return node

    counts.recording = recording
    return counts


def _download(app, user, node):
    resp = _call(app, user, "GET",
                 f"/api/nodes/{node.id}/audio-download?format=mp3")
    status, body = resp.status_code, resp.get_json(silent=True)
    resp.close()
    return status, body


def _held(rec):
    return {k for k in rec.redis.data
            if k.startswith("loore:audio_download:")
            and rec.redis.get(k) is not None}


def test_second_download_while_one_is_prepared_is_refused(app, data, rec):  # noqa: F811
    node = rec.recording(data.alice)
    inner = {}

    def second_request():
        inner["answer"] = _download(app, data.alice, node)

    rec.on_ffmpeg = second_request
    assert _download(app, data.alice, node)[0] == 200
    status, body = inner["answer"]
    assert status == 429
    assert body["code"] == "download_busy"
    assert rec.ffmpeg == N_FILES + 2      # one merge, not two
    assert _held(rec) == set()
    # Once the first has ended, the next download goes ahead.
    assert _download(app, data.alice, node)[0] == 200


@pytest.mark.parametrize("fmt", ["mp3", "original"])
def test_download_leaves_no_temporary_file(app, data, rec, fmt):  # noqa: F811
    node = rec.recording(data.alice)
    resp = _call(app, data.alice, "GET",
                 f"/api/nodes/{node.id}/audio-download?format={fmt}")
    assert resp.status_code == 200
    assert resp.data == b"merged-output"
    assert rec.decrypt == N_FILES
    assert os.listdir(rec.tmp) == []
    resp.close()
    assert os.listdir(rec.tmp) == []
    assert _held(rec) == set()


def test_downloads_beyond_the_server_slots_are_refused(app, data, rec):  # noqa: F811
    node = rec.recording(data.alice)
    for slot in range(rec.nodes.AUDIO_DOWNLOAD_SLOTS):
        rec.redis.set(f"loore:audio_download:slot:{slot}", "other",
                      ex=rec.nodes.AUDIO_DOWNLOAD_HOLD_SECONDS)
    status, body = _download(app, data.alice, node)
    assert status == 429 and body["code"] == "download_busy"
    assert rec.decrypt == 0 and rec.ffmpeg == 0
    # The account's own hold was given back.
    assert rec.redis.get(f"loore:audio_download:user:{data.alice.id}") \
        is None
    # A slot that is free again lets it in.
    rec.redis.data.pop("loore:audio_download:slot:0")
    assert _download(app, data.alice, node)[0] == 200


def test_failed_download_ends_its_holds_and_temporary_files(app, data, rec):  # noqa: F811
    node = rec.recording(data.alice)
    rec.fail_mp3 = True
    status, _ = _download(app, data.alice, node)
    assert status == 500
    assert _held(rec) == set()
    assert rec.temp and os.listdir(rec.tmp) == []
    rec.fail_mp3 = False
    assert _download(app, data.alice, node)[0] == 200


def test_hold_left_by_a_killed_request_expires(app, data, rec):  # noqa: F811
    node = rec.recording(data.alice)
    rec.redis.set(f"loore:audio_download:user:{data.alice.id}", "killed",
                  ex=rec.nodes.AUDIO_DOWNLOAD_HOLD_SECONDS)
    assert _download(app, data.alice, node)[0] == 429
    rec.redis.advance(rec.nodes.AUDIO_DOWNLOAD_HOLD_SECONDS + 1)
    assert _download(app, data.alice, node)[0] == 200


def test_public_recording_still_downloads_for_another_account(
        app, data, rec):  # noqa: F811
    carol = User(username="carol", approved=True, plan="free")
    _db.session.add(carol)
    _db.session.commit()
    node = rec.recording(data.alice, "public")
    assert _download(app, carol, node)[0] == 200


def test_chunk_lengths_are_measured_once(app, data, rec):  # noqa: F811
    node = rec.recording(data.alice)
    url = f"/api/nodes/{node.id}/audio-chunks"
    for _ in range(3):
        resp = _call(app, data.alice, "GET", url)
        assert resp.status_code == 200
        chunks = resp.get_json()["chunks"]
        assert [c["duration"] for c in chunks] == [300.0] * N_FILES
        assert chunks[0]["url"] == (
            f"/media/nodes/{data.alice.id}/{node.id}/batch_0000.mp4")
    assert rec.decrypt == N_FILES and rec.ffprobe == N_FILES

    # A file that changes is measured again; the others are not.
    (node.folder / "batch_0001.mp4.enc").write_bytes(b"longer ciphertext")
    assert _call(app, data.alice, "GET", url).status_code == 200
    assert rec.decrypt == N_FILES + 1 and rec.ffprobe == N_FILES + 1


def test_chunk_that_cannot_be_decrypted_is_tried_again(app, data, rec):  # noqa: F811
    node = rec.recording(data.alice)
    url = f"/api/nodes/{node.id}/audio-chunks"
    rec.fail_decrypt = {"batch_0002.mp4.enc"}
    resp = _call(app, data.alice, "GET", url)
    assert [c["duration"] for c in resp.get_json()["chunks"]] == (
        [300.0] * N_FILES)
    assert rec.ffprobe == N_FILES - 1
    rec.fail_decrypt = set()
    _call(app, data.alice, "GET", url)
    assert rec.ffprobe == N_FILES      # only the one that failed


def test_chunk_lengths_without_redis_are_measured_each_time(
        app, data, rec, monkeypatch):  # noqa: F811
    from backend.utils import run_locks
    attempts = []

    def unreachable():
        attempts.append(1)
        raise ConnectionError("no Redis")

    monkeypatch.setattr(run_locks, "_client", unreachable)
    node = rec.recording(data.alice)
    url = f"/api/nodes/{node.id}/audio-chunks"
    for round_no in (1, 2):
        resp = _call(app, data.alice, "GET", url)
        assert [c["duration"] for c in resp.get_json()["chunks"]] == (
            [300.0] * N_FILES)
        assert rec.ffprobe == round_no * N_FILES
    # One read and one write tried per request, not one per file.
    assert len(attempts) == 4


def test_measurement_without_a_length_is_measured_again_minutes_later(
        app, data, rec, monkeypatch):  # noqa: F811
    node = rec.recording(data.alice)
    url = f"/api/nodes/{node.id}/audio-chunks"
    calls = {"n": 0}

    def busy_once(cmd, *a, **k):
        # ffprobe states no length the first time (timed out, no output),
        # then 120 s.
        assert cmd[0] == "ffprobe"
        calls["n"] += 1
        out = "" if calls["n"] <= N_FILES else "120.0\n"
        return types.SimpleNamespace(returncode=1 if not out else 0,
                                     stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", busy_once)

    def lengths():
        chunks = _call(app, data.alice, "GET", url).get_json()["chunks"]
        return [c["duration"] for c in chunks]

    assert lengths() == [300.0] * N_FILES
    # Within the few minutes: answered without measuring again.
    assert lengths() == [300.0] * N_FILES
    assert calls["n"] == N_FILES
    # After them: measured again; that length is then kept.
    rec.redis.advance(rec.nodes.AUDIO_SECONDS_UNKNOWN_KEPT_FOR + 1)
    assert lengths() == [120.0] * N_FILES
    assert calls["n"] == 2 * N_FILES
    rec.redis.advance(rec.nodes.AUDIO_SECONDS_UNKNOWN_KEPT_FOR + 1)
    assert lengths() == [120.0] * N_FILES
    assert calls["n"] == 2 * N_FILES
    assert rec.nodes.AUDIO_SECONDS_UNKNOWN_KEPT_FOR <= 10 * 60
