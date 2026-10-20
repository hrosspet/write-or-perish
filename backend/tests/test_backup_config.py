"""Nightly backups (PR #278).

- The bucket's lifecycle config, the runbook's retention commands and
  scripts/backup.sh agree on 28 days, so every backup copy is gone within
  the 30 days the terms promise after an account's final deletion.
- The media go into one folder per 14-day period, so the lifecycle delete
  never leaves the recordings without a copy.
- The media sync skips transient folders and, with encryption on, every
  file that is not *.enc: gcloud's --exclude regex is checked with re.match,
  as gcloud applies it.
- A dump gets its .ok marker only when pg_dump succeeded; the media sync
  runs anyway, and a failed sync is retried once.
- A failed run is reported (OnFailure= → scripts/backup-alert.sh).
- The database password reaches pg_dump through PGPASSWORD, never its
  command line, and is never logged.

The script runs in its local mode (BACKUP_BUCKET is a directory) with a
stub pg_dump; the gs:// path needs a real bucket and is not tested here."""
import datetime
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
LIFECYCLE = REPO / "configs" / "gcs-backup-lifecycle.json"
RUNBOOK = REPO / "docs" / "RUNBOOKS.md"
SCRIPT = REPO / "scripts" / "backup.sh"
ALERT_SCRIPT = REPO / "scripts" / "backup-alert.sh"
SERVICE = REPO / "write-or-perish-backup.service"
ALERT_SERVICE = REPO / "write-or-perish-backup-alert.service"
DEPLOY = REPO / "deploy.sh"

RETENTION_DAYS = 28
KMS_KEY = "projects/p/locations/l/keyRings/r/cryptoKeys/k"


def _lifecycle_age():
    return json.loads(LIFECYCLE.read_text())["rule"][0]["condition"]["age"]


def _script_value(name):
    m = re.search(rf"^{name}=(?:'([^']*)'|(\S+))$", SCRIPT.read_text(), re.M)
    assert m, f"{name} not found in scripts/backup.sh"
    return m.group(1) if m.group(1) is not None else m.group(2)


def _media_period_days():
    return int(_script_value("MEDIA_PERIOD_DAYS"))


# --- config and runbook ----------------------------------------------------

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


def test_runbook_setup_order_heartbeat_required_lock_last():
    text = RUNBOOK.read_text()
    setup = text[text.index("### One-time setup"):text.index("### Restore\n")]
    order = ["**Bucket**", "**Heartbeat and the VM**", "**First nightly run**",
             "**Plaintext check**", "**Test restore**", "--lock-retention-period"]
    positions = [setup.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert re.search(r"^\s*BACKUP_HEARTBEAT_URL=https://\S+\s*$", setup, re.M)


def test_runbook_restores_only_dumps_with_a_marker():
    text = RUNBOOK.read_text()
    picks = re.findall(r"gcloud storage ls gs://BUCKET/db/ \|([^\n]*)", text)
    assert picks
    for rest in picks:
        if "tail -1" in rest:
            assert r"grep '\.dump\.ok$'" in rest, rest


def test_media_period_is_at_most_half_the_lifecycle_age():
    # Objects in the current period's folder are younger than one period,
    # so they never reach the lifecycle delete; the previous folder is kept
    # for at least another period as a fallback.
    assert 2 * _media_period_days() <= _lifecycle_age()


# --- the media exclude regex, as gcloud applies it (re.match) --------------

def _gcloud_excludes(encrypted):
    transient = _script_value("MEDIA_EXCLUDE_TRANSIENT")
    plaintext = _script_value("MEDIA_EXCLUDE_PLAINTEXT")
    pattern = f"{transient}|{plaintext}" if encrypted else transient
    regex = re.compile(pattern)
    return lambda path: regex.match(path) is not None


KEPT = [
    "user/1/node/2/original.webm.enc",
    "user/1/node/2/tts_chunk_0.mp3.enc",
    "user/1/node/2/tts.mp3.enc",
    "user/1/profile/3/tts.mp3.enc",
    "user/1/item/4/tts_chunk_1.mp3.enc",
    "nodes/1/2/batch_0000-0003.webm.enc",
    "nodes/1/2/chunk_0001.webm.enc",
    "user/1/node/2/streaming.webm.enc",   # only top-level folders are transient
]
TRANSIENT = [
    "drafts/1/abc/chunk_0001.webm.enc",
    "chunks/1/upload-1/chunk_0000",
    "chunks/1/upload-1/metadata.json",
    "streaming/1/sess/chunk_0001.webm.enc",
]
PLAINTEXT = [
    "user/1/node/2/tts_chunk_0.mp3",          # about to become .mp3.enc
    "user/1/node/2/original.webm",
    "nodes/1/2/batch_0000-0003.webm",
    "nodes/1/2/chunk_0001.webm.enc.legacy_backup",
    "user/1/node/2/original.webm.encx",
]


@pytest.mark.parametrize("path", KEPT)
def test_exclude_keeps_stored_audio(path):
    assert not _gcloud_excludes(encrypted=True)(path)
    assert not _gcloud_excludes(encrypted=False)(path)


@pytest.mark.parametrize("path", TRANSIENT)
def test_exclude_skips_transient_folders(path):
    assert _gcloud_excludes(encrypted=True)(path)
    assert _gcloud_excludes(encrypted=False)(path)


@pytest.mark.parametrize("path", PLAINTEXT)
def test_exclude_skips_plaintext_only_when_encryption_is_on(path):
    assert _gcloud_excludes(encrypted=True)(path)
    assert not _gcloud_excludes(encrypted=False)(path)


def test_gcloud_rsync_uses_the_exclude_and_no_clobber():
    text = SCRIPT.read_text()
    call = text[text.index("gcloud storage rsync"):]
    call = call[:call.index("|| return 1")]
    assert '--exclude="$MEDIA_EXCLUDE"' in call
    assert "--no-clobber" in call


# --- the script in local mode ----------------------------------------------

def _period_folder(now):
    days = _media_period_days()
    epoch_day = int(now) // 86400
    start = (epoch_day - epoch_day % days) * 86400
    return datetime.datetime.fromtimestamp(
        start, datetime.timezone.utc).strftime("%Y-%m-%d")


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _stub(bin_dir, name, body):
    stub = bin_dir / name
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "pg_dump", "echo fake-dump\n")

    audio = tmp_path / "audio"
    _write(audio / "user/1/node/2/rec.webm.enc", b"v1")
    _write(audio / "user/1/node/2/tts_chunk_0.mp3", b"plain, being encrypted")
    _write(audio / "drafts/1/s/chunk_0001.webm.enc", b"draft")
    _write(audio / "chunks/1/u/chunk_0000", b"upload part")
    _write(audio / "streaming/1/s/chunk_0001.webm.enc", b"live")

    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "DATABASE_URL": "postgresql://unused",
        "BACKUP_BUCKET": str(tmp_path / "bucket"),
        "AUDIO_STORAGE_PATH": str(audio),
        "GCP_KMS_KEY_NAME": KMS_KEY,
    }


def _run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)


def _only_period_folder(bucket, before, after):
    folders = [p.name for p in (bucket / "media").iterdir()]
    assert len(folders) == 1 and folders[0] in {before, after}
    return bucket / "media" / folders[0]


def test_backup_local_mode_writes_marked_dump_and_period_folder(env):
    before = _period_folder(time.time())
    result = _run(env)
    after = _period_folder(time.time())
    assert result.returncode == 0, result.stderr

    bucket = pathlib.Path(env["BACKUP_BUCKET"])
    dumps = list((bucket / "db").glob("*.dump"))
    assert len(dumps) == 1
    assert dumps[0].read_text() == "fake-dump\n"
    marker = dumps[0].with_name(dumps[0].name + ".ok")
    assert marker.read_text().strip() == str(len("fake-dump\n"))

    period = _only_period_folder(bucket, before, after)
    assert (period / "user/1/node/2/rec.webm.enc").read_bytes() == b"v1"
    assert not (period / "user/1/node/2/tts_chunk_0.mp3").exists()
    for transient in ("drafts", "chunks", "streaming"):
        assert not (period / transient).exists()

    # Same period: new files are added, existing copies are never
    # overwritten (the bucket refuses overwrites under retention).
    audio = pathlib.Path(env["AUDIO_STORAGE_PATH"])
    (audio / "user/1/node/2/rec.webm.enc").write_bytes(b"v2")
    _write(audio / "user/1/node/2/tts_chunk_0.mp3.enc", b"tts")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    if _period_folder(time.time()) == period.name:
        assert (period / "user/1/node/2/rec.webm.enc").read_bytes() == b"v1"
        assert (period / "user/1/node/2/tts_chunk_0.mp3.enc").read_bytes() == b"tts"


def test_backup_without_encryption_keeps_plain_files(env):
    env = dict(env, GCP_KMS_KEY_NAME="")
    before = _period_folder(time.time())
    result = _run(env)
    after = _period_folder(time.time())
    assert result.returncode == 0, result.stderr
    period = _only_period_folder(pathlib.Path(env["BACKUP_BUCKET"]), before, after)
    assert (period / "user/1/node/2/tts_chunk_0.mp3").exists()
    assert not (period / "chunks").exists()


def test_encryption_disabled_flag_counts_as_off(env):
    env = dict(env, ENCRYPTION_DISABLED="True")
    before = _period_folder(time.time())
    result = _run(env)
    after = _period_folder(time.time())
    assert result.returncode == 0, result.stderr
    period = _only_period_folder(pathlib.Path(env["BACKUP_BUCKET"]), before, after)
    assert (period / "user/1/node/2/tts_chunk_0.mp3").exists()


def test_failed_dump_has_no_marker_and_media_still_syncs(env, tmp_path):
    _stub(tmp_path / "bin", "pg_dump", "printf 'PGDMP-partial'\nexit 1\n")
    before = _period_folder(time.time())
    result = _run(env)
    after = _period_folder(time.time())
    assert result.returncode == 1
    assert "database dump failed" in result.stderr
    assert "no heartbeat sent" in result.stderr

    bucket = pathlib.Path(env["BACKUP_BUCKET"])
    assert list((bucket / "db").glob("*.dump"))           # the partial file
    assert not list((bucket / "db").glob("*.ok"))         # but no marker
    period = _only_period_folder(bucket, before, after)
    assert (period / "user/1/node/2/rec.webm.enc").exists()


def test_failed_media_sync_is_retried_once(env, tmp_path):
    calls = tmp_path / "rsync-calls"
    # A stub rsync that fails its first call, as gcloud's rsync does when a
    # file vanishes between the listing and the upload.
    _stub(tmp_path / "bin", "rsync",
          f'echo x >> "{calls}"\n'
          f'[ "$(wc -l < "{calls}" | tr -d " ")" -gt 1 ] || exit 24\n')
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().count("x") == 2
    assert "media sync done (second attempt)" in result.stdout


def test_media_sync_failing_twice_fails_the_run(env, tmp_path):
    _stub(tmp_path / "bin", "rsync", "exit 23\n")
    result = _run(env)
    assert result.returncode == 1
    assert "media sync failed twice" in result.stderr
    bucket = pathlib.Path(env["BACKUP_BUCKET"])
    assert list((bucket / "db").glob("*.ok"))             # the dump still counts


def test_backup_without_bucket_fails_loudly(env):
    env = dict(env, BACKUP_BUCKET="")
    result = _run(env)
    assert result.returncode == 1
    assert "BACKUP_BUCKET is not set" in result.stderr


# --- the database password stays off pg_dump's command line ------------------
# Any process on the VM can read another's arguments (ps), not its
# environment. The stub records what pg_dump received.

def _recording_pg_dump(tmp_path):
    argv_file = tmp_path / "pg_dump-argv"
    env_file = tmp_path / "pg_dump-pgpassword"
    _stub(tmp_path / "bin", "pg_dump",
          f'printf "%s\\n" "$@" > "{argv_file}"\n'
          f'if [ "${{PGPASSWORD+set}}" = set ]; then '
          f'printf "%s" "$PGPASSWORD" > "{env_file}"; fi\n'
          'echo fake-dump\n')
    return argv_file, env_file


@pytest.mark.parametrize("url, password, passed_url", [
    # URL-encoded password (@, /, a backslash), a port, a query parameter
    ("postgresql://loore:s3cr%40t%2Fp%5Cw@db.internal:5433/loore_prod"
     "?sslmode=require",
     "s3cr@t/p\\w",
     "postgresql://loore@db.internal:5433/loore_prod?sslmode=require"),
    # no port
    ("postgresql://loore:plain-pw@localhost/loore_prod",
     "plain-pw",
     "postgresql://loore@localhost/loore_prod"),
    # the postgres:// spelling and a password with a colon
    ("postgres://loore:a:b-pw@127.0.0.1/loore_prod",
     "a:b-pw",
     "postgresql://loore@127.0.0.1/loore_prod"),
])
def test_pg_dump_gets_the_password_in_its_environment_only(
        env, tmp_path, url, password, passed_url):
    argv_file, env_file = _recording_pg_dump(tmp_path)
    result = _run(dict(env, DATABASE_URL=url))
    assert result.returncode == 0, result.stderr

    argv = argv_file.read_text().splitlines()
    assert f"--dbname={passed_url}" in argv
    raw_password = url.split("://", 1)[1].rsplit("@", 1)[0].split(":", 1)[1]
    for secret in {password, raw_password}:
        assert not any(secret in arg for arg in argv)
        assert secret not in result.stdout + result.stderr
    assert env_file.read_text() == password


def test_pg_dump_without_a_password_gets_no_pgpassword(env, tmp_path):
    argv_file, env_file = _recording_pg_dump(tmp_path)
    result = _run(dict(env, DATABASE_URL="postgresql:///loore_prod"))
    assert result.returncode == 0, result.stderr
    assert "--dbname=postgresql:///loore_prod" in argv_file.read_text().splitlines()
    assert not env_file.exists()


def test_a_database_url_that_is_not_postgres_fails_without_echoing_it(env):
    result = _run(dict(env, DATABASE_URL="mysql://u:hunter2-pw@h/d"))
    assert result.returncode == 1
    assert "DATABASE_URL is not a postgresql:// URL" in result.stderr
    assert "hunter2" not in result.stdout + result.stderr


# --- failure reporting -----------------------------------------------------

def test_backup_unit_reports_failures_and_deploy_installs_the_handler():
    assert re.search(r"^OnFailure=write-or-perish-backup-alert\.service$",
                     SERVICE.read_text(), re.M)
    assert "scripts/backup-alert.sh" in ALERT_SERVICE.read_text()
    deploy = DEPLOY.read_text()
    assert "write-or-perish-backup-alert.service" in deploy
    assert '"$BACKUP_ALERT_SOURCE:$BACKUP_ALERT_TARGET"' in deploy


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_alert_logs_an_error_and_pings_the_failure_url():
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append((self.path, self.rfile.read(length).decode()))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/check-uuid"
        result = subprocess.run(
            ["bash", str(ALERT_SCRIPT), "write-or-perish-backup.service"],
            env={"PATH": os.environ.get("PATH", ""),
                 "BACKUP_HEARTBEAT_URL": url},
            capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
    assert result.returncode == 0
    assert result.stdout.startswith(
        "<3>Nightly backup failed (write-or-perish-backup.service)")
    assert [path for path, _ in received] == ["/check-uuid/fail"]
    assert "Nightly backup failed" in received[0][1]
