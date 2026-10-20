"""Nightly backups (PR #278): the bucket's lifecycle config, the runbook's
retention commands and scripts/backup.sh agree on 28 days, so every backup
copy is gone within the 30 days the terms promise after an account's final
deletion; and the media go into one folder per 14-day period, so the
lifecycle delete never leaves the recordings without a copy.

The script is run in its local mode (BACKUP_BUCKET is a directory) with a
stub pg_dump; the gs:// path needs a real bucket and is not tested here."""
import datetime
import json
import os
import pathlib
import re
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
LIFECYCLE = REPO / "configs" / "gcs-backup-lifecycle.json"
RUNBOOK = REPO / "docs" / "RUNBOOKS.md"
SCRIPT = REPO / "scripts" / "backup.sh"

RETENTION_DAYS = 28


def _lifecycle_age():
    return json.loads(LIFECYCLE.read_text())["rule"][0]["condition"]["age"]


def _media_period_days():
    m = re.search(r"^MEDIA_PERIOD_DAYS=(\d+)$", SCRIPT.read_text(), re.M)
    assert m, "MEDIA_PERIOD_DAYS not found in scripts/backup.sh"
    return int(m.group(1))


def test_lifecycle_deletes_everything_at_age_28():
    config = json.loads(LIFECYCLE.read_text())
    assert config == {
        "rule": [{"action": {"type": "Delete"},
                  "condition": {"age": RETENTION_DAYS}}]
    }


def test_runbook_retention_matches_lifecycle_and_soft_delete_is_off():
    text = RUNBOOK.read_text()
    periods = re.findall(r"--retention-period=(\d+)d", text)
    assert periods, "runbook has no --retention-period command"
    assert {int(p) for p in periods} == {_lifecycle_age()}
    assert "--soft-delete-duration=0" in text
    # The lock comes after the first nightly run and the test restore.
    setup = text[text.index("### One-time setup"):text.index("### Restore\n")]
    assert (setup.index("--lock-retention-period")
            > setup.index("**First nightly run**")
            > setup.index("**Bucket**"))
    assert setup.index("**Test restore**") < setup.index("--lock-retention-period")


def test_media_period_is_at_most_half_the_lifecycle_age():
    # Objects in the current period's folder are younger than one period,
    # so they never reach the lifecycle delete; the previous folder is kept
    # for at least another period as a fallback.
    assert 2 * _media_period_days() <= _lifecycle_age()


def _period_folder(now):
    days = _media_period_days()
    epoch_day = int(now) // 86400
    start = (epoch_day - epoch_day % days) * 86400
    return datetime.datetime.fromtimestamp(
        start, datetime.timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pg_dump"
    stub.write_text("#!/bin/sh\necho fake-dump\n")
    stub.chmod(0o755)

    audio = tmp_path / "audio"
    (audio / "user" / "1" / "node" / "2").mkdir(parents=True)
    (audio / "user" / "1" / "node" / "2" / "rec.webm.enc").write_bytes(b"v1")
    (audio / "drafts" / "1" / "s").mkdir(parents=True)
    (audio / "drafts" / "1" / "s" / "chunk_0001.webm").write_bytes(b"draft")

    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "DATABASE_URL": "postgresql://unused",
        "BACKUP_BUCKET": str(tmp_path / "bucket"),
        "AUDIO_STORAGE_PATH": str(audio),
    }


def _run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)


def test_backup_local_mode_writes_dump_and_period_folder(env):
    before = _period_folder(time.time())
    result = _run(env)
    after = _period_folder(time.time())
    assert result.returncode == 0, result.stderr

    bucket = pathlib.Path(env["BACKUP_BUCKET"])
    dumps = list((bucket / "db").glob("*.dump"))
    assert len(dumps) == 1
    assert dumps[0].read_text() == "fake-dump\n"

    folders = [p.name for p in (bucket / "media").iterdir()]
    assert len(folders) == 1 and folders[0] in {before, after}
    period = bucket / "media" / folders[0]
    assert (period / "user/1/node/2/rec.webm.enc").read_bytes() == b"v1"
    assert not (period / "drafts").exists()

    # Same period: new files are added, existing copies are never
    # overwritten (the bucket refuses overwrites under retention).
    audio = pathlib.Path(env["AUDIO_STORAGE_PATH"])
    (audio / "user/1/node/2/rec.webm.enc").write_bytes(b"v2")
    (audio / "user/1/node/2/tts_chunk_0.mp3").write_bytes(b"tts")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    if _period_folder(time.time()) == folders[0]:
        assert (period / "user/1/node/2/rec.webm.enc").read_bytes() == b"v1"
        assert (period / "user/1/node/2/tts_chunk_0.mp3").read_bytes() == b"tts"
    assert len(list((bucket / "db").glob("*.dump"))) in {1, 2}


def test_backup_without_bucket_fails_loudly(env):
    env = dict(env, BACKUP_BUCKET="")
    result = _run(env)
    assert result.returncode == 1
    assert "BACKUP_BUCKET is not set" in result.stderr
