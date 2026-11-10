#!/bin/bash
# Nightly backup for Loore production: PostgreSQL logical dump + media files.
#
# What it does
#   1. pg_dump (custom format, compressed) streamed straight to the destination.
#      No local temp file, so the prod disk never fills up. Only when pg_dump
#      and the upload both succeed is a marker db/<dump>.ok written next to it;
#      a dump without its marker is incomplete and must never be restored.
#   2. Media directory (voice recordings and TTS audio) synced to
#      media/<period>/ at the destination, one folder per 14-day period (see
#      MEDIA_PERIOD_DAYS below). Additive only — nothing is ever deleted or
#      overwritten there. Transient files are skipped (see MEDIA_EXCLUDE_*).
#      Runs even when the dump failed.
#   3. When both succeeded, pings BACKUP_HEARTBEAT_URL so a dead-man's-switch
#      monitor alerts when backups stop. A failure exits non-zero, and the
#      unit's OnFailure= handler (scripts/backup-alert.sh) reports it.
#
# Destination (BACKUP_BUCKET)
#   gs://bucket-name  Google Cloud Storage, authenticated as the VM's attached
#                     service account (needs the devstorage.read_write scope and
#                     objectCreator + objectViewer on the bucket).
#   /some/directory   Plain filesystem copy — for dry runs and local testing.
#
# Environment (loaded from .env.production by the systemd unit)
#   DATABASE_URL          required
#   BACKUP_BUCKET         required (gs://… or a directory)
#   BACKUP_HEARTBEAT_URL  required on prod (docs/RUNBOOKS.md → one-time setup)
#   AUDIO_STORAGE_PATH    optional, default data/audio (relative to cwd)
#   GCP_KMS_KEY_NAME, ENCRYPTION_DISABLED  read to know whether audio is
#                         encrypted at rest (see MEDIA_EXCLUDE_PLAINTEXT)
#
# Exits non-zero on any failure so systemd marks the unit failed and it shows
# up in `systemctl --failed` and the journal. Runbook: docs/RUNBOOKS.md.

set -euo pipefail

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] backup: $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set"
[ -n "${BACKUP_BUCKET:-}" ] || die "BACKUP_BUCKET is not set — backups are NOT running. Set it in .env.production (docs/RUNBOOKS.md → Backups)."

# --- database connection -----------------------------------------------------
# The password never goes on pg_dump's command line: any process on the VM
# can read another process's arguments (ps), but not its environment. So
# DATABASE_URL is split here, pg_dump gets the URL without the password, and
# the password goes in PGPASSWORD in pg_dump's environment only (see
# run_pg_dump). The URL keeps its port (or none) and its query parameters
# (e.g. sslmode). Nothing in this script logs the URL or the password.
# Form: postgresql://[user[:password]@][host][:port][/dbname][?params]
url_decode() {  # %XX → byte; printf is a builtin, so no process sees it
  local s="${1//\\/\\\\}"
  printf '%b' "${s//%/\\x}"
}

case "${DATABASE_URL%%://*}" in
  postgres|postgresql|postgresql+*) ;;
  *) die "DATABASE_URL is not a postgresql:// URL" ;;
esac
DB_REST="${DATABASE_URL#*://}"
DB_AUTHORITY="${DB_REST%%[/?]*}"        # [user[:password]@][host][:port]
DB_TAIL="${DB_REST:${#DB_AUTHORITY}}"   # [/dbname][?params]
DB_USER=""
DB_PASSWORD=""
DB_HOSTPORT="$DB_AUTHORITY"
if [[ "$DB_AUTHORITY" == *@* ]]; then
  DB_USERINFO="${DB_AUTHORITY%@*}"
  DB_HOSTPORT="${DB_AUTHORITY##*@}"
  DB_USER="${DB_USERINFO%%:*}"          # stays %-encoded; libpq decodes it
  if [[ "$DB_USERINFO" == *:* ]]; then
    DB_PASSWORD="$(url_decode "${DB_USERINFO#*:}")"
  fi
  unset DB_USERINFO
fi
DB_URL_NO_PASSWORD="postgresql://${DB_USER:+${DB_USER}@}${DB_HOSTPORT}${DB_TAIL}"

run_pg_dump() {
  if [ -n "$DB_PASSWORD" ]; then
    PGPASSWORD="$DB_PASSWORD" pg_dump "$@"
  else
    pg_dump "$@"
  fi
}

AUDIO_DIR="${AUDIO_STORAGE_PATH:-data/audio}"
DEST="${BACKUP_BUCKET%/}"
STAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
DUMP_NAME="$(hostname -s)-${STAMP}.dump"

# Media goes into one folder per 14-day period, named after the period's
# first day (UTC), e.g. media/2026-10-22/. The first run of a period uploads
# a full copy; later runs add only new files. So every object in the current
# folder is less than 14 days old, well inside the bucket's 28-day lifecycle
# delete, and the previous folder is kept until 28 days after its upload.
# A single additive folder would not work: the lifecycle deletes each copy
# on day 28 and only the next night's run uploads it again, so the media
# would have no copy for up to a day every 28 days.
# Must stay at most half the lifecycle age (configs/gcs-backup-lifecycle.json).
MEDIA_PERIOD_DAYS=14
EPOCH_DAY=$(( $(date -u +%s) / 86400 ))
PERIOD_START=$(( (EPOCH_DAY - EPOCH_DAY % MEDIA_PERIOD_DAYS) * 86400 ))
PERIOD="$(date -u -d "@${PERIOD_START}" +%Y-%m-%d 2>/dev/null \
  || date -u -r "${PERIOD_START}" +%Y-%m-%d)"   # GNU date || BSD date
MEDIA_DEST="${DEST}/media/${PERIOD}"

# What the media sync skips. gcloud matches each --exclude regex with
# Python's re.match against the path relative to AUDIO_DIR.
# - Transient folders, removed when the recording or upload they belong to
#   finishes: drafts/ (voice-mode drafts), chunks/ (chunked uploads being
#   assembled, stored unencrypted), streaming/ (live recording chunks,
#   batched into nodes/ at the end).
# - With encryption on, every file that is not *.enc. All audio writers
#   (recordings, uploads, TTS, batches) call encrypt_file(), which writes
#   X.enc and then deletes X, whatever the node's privacy; so a plain file is
#   one about to be replaced, being assembled, or an ffmpeg temp file. The
#   .legacy_backup rollback copies of backend/scripts/backfill_legacy_chunks.py
#   are skipped too (they are never served).
# Encryption is on under the rule of is_encryption_enabled() in
# backend/utils/encryption.py: GCP_KMS_KEY_NAME set, ENCRYPTION_DISABLED not
# true/1/yes. With it off, every stored file is plain and is backed up.
MEDIA_EXCLUDE_TRANSIENT='^(drafts|chunks|streaming)/.*'
MEDIA_EXCLUDE_PLAINTEXT='.*(?<!\.enc)$'
ENC_ONLY=false
if [ -n "${GCP_KMS_KEY_NAME:-}" ]; then
  case "$(printf '%s' "${ENCRYPTION_DISABLED:-}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) ;;
    *) ENC_ONLY=true ;;
  esac
fi
if [ "$ENC_ONLY" = true ]; then
  MEDIA_EXCLUDE="${MEDIA_EXCLUDE_TRANSIENT}|${MEDIA_EXCLUDE_PLAINTEXT}"
  NAME_GLOB='*.enc'      # the same rule for the local copy below
else
  MEDIA_EXCLUDE="$MEDIA_EXCLUDE_TRANSIENT"
  NAME_GLOB='*'
fi

case "$DEST" in
  gs://*) MODE=gcs ;;
  *)      MODE=local ;;
esac

command -v pg_dump >/dev/null || die "pg_dump not found on PATH"
if [ "$MODE" = gcs ]; then
  command -v gcloud >/dev/null || die "gcloud not found — install google-cloud-cli (docs/RUNBOOKS.md → Backups → one-time setup)"
fi

FAILED=""

# --- 1. database -----------------------------------------------------------
# --no-owner/--no-privileges: restores cleanly into a DB owned by a different
# role (local restore test, a rebuilt VM, the lab).
# --exclude-schema=vec: the pgvector copies of the embeddings (#331) are
# derived data, rebuilt by `flask pgvector-enable --apply`; leaving them out
# keeps ~8.5 KB per embedding out of every dump. Matches nothing until then.
# If pg_dump dies midway, gcloud still completes the upload of what it got,
# and under retention that truncated object can't be deleted. pipefail makes
# the function fail, so no .ok marker is written for it.
dump_database() {
  if [ "$MODE" = gcs ]; then
    run_pg_dump --format=custom --no-owner --no-privileges --exclude-schema=vec --dbname="$DB_URL_NO_PASSWORD" \
      | gcloud storage cp --quiet - "${DEST}/db/${DUMP_NAME}" || return 1
    SIZE="$(gcloud storage objects describe "${DEST}/db/${DUMP_NAME}" --format='value(size)')" || return 1
    printf '%s\n' "$SIZE" | gcloud storage cp --quiet - "${DEST}/db/${DUMP_NAME}.ok" || return 1
  else
    mkdir -p "${DEST}/db" || return 1
    run_pg_dump --format=custom --no-owner --no-privileges --exclude-schema=vec --dbname="$DB_URL_NO_PASSWORD" \
      > "${DEST}/db/${DUMP_NAME}" || return 1
    SIZE="$(wc -c < "${DEST}/db/${DUMP_NAME}" | tr -d ' ')" || return 1
    printf '%s\n' "$SIZE" > "${DEST}/db/${DUMP_NAME}.ok" || return 1
  fi
}

log "dumping database → ${DEST}/db/${DUMP_NAME}"
if dump_database; then
  log "database dump done (${SIZE} bytes, marker ${DUMP_NAME}.ok)"
else
  log "ERROR: database dump failed — ${DUMP_NAME} has no .ok marker; never restore it" >&2
  FAILED="${FAILED} database"
fi

# --- 2. media --------------------------------------------------------------
# --no-clobber: a file that changed after its upload this period (a TTS file
# regenerated after an edit) is skipped, not overwritten. The bucket refuses
# overwrites under retention, which would fail the whole run; the new version
# lands with the next period's full copy.
sync_media() {
  if [ "$MODE" = gcs ]; then
    gcloud storage rsync --quiet --recursive --no-clobber \
      --exclude="$MEDIA_EXCLUDE" "$AUDIO_DIR" "$MEDIA_DEST" || return 1
  else
    mkdir -p "$MEDIA_DEST" || return 1
    if command -v rsync >/dev/null; then
      rsync -a --ignore-existing --exclude=/drafts/ --exclude=/chunks/ --exclude=/streaming/ \
        --include='*/' --include="$NAME_GLOB" --exclude='*' \
        "${AUDIO_DIR}/" "${MEDIA_DEST}/" || return 1
    else
      (cd "$AUDIO_DIR" \
        && find . \( -path ./drafts -o -path ./chunks -o -path ./streaming \) -prune \
             -o -type f -name "$NAME_GLOB" -print0 \
        | xargs -0 -I{} sh -c '[ -e "$1/$2" ] || { mkdir -p "$1/$(dirname "$2")" && cp -p "$2" "$1/$2"; }' _ "$MEDIA_DEST" {}) \
        || return 1
    fi
  fi
}

SKIPPED="drafts/, chunks/, streaming/"
if [ "$ENC_ONLY" = true ]; then
  SKIPPED="${SKIPPED}, files not ending in .enc"
fi

if [ -d "$AUDIO_DIR" ]; then
  log "syncing media ${AUDIO_DIR} → ${MEDIA_DEST} (additive; skipping ${SKIPPED})"
  if sync_media; then
    log "media sync done"
  else
    # The directory is live: a file deleted between the listing and its
    # upload (a purge, a finished recording) aborts gcloud's rsync. A second
    # run lists again without it, and --no-clobber skips what the first run
    # uploaded.
    log "WARNING: media sync failed; retrying once (a file may have vanished mid-run)"
    if sync_media; then
      log "media sync done (second attempt)"
    else
      log "ERROR: media sync failed twice" >&2
      FAILED="${FAILED} media"
    fi
  fi
else
  log "media dir ${AUDIO_DIR} does not exist — skipping media"
fi

[ -z "$FAILED" ] || die "failed:${FAILED} — no heartbeat sent"

# --- 3. heartbeat ----------------------------------------------------------
if [ -n "${BACKUP_HEARTBEAT_URL:-}" ]; then
  if curl -fsS -m 10 -o /dev/null "$BACKUP_HEARTBEAT_URL"; then
    log "heartbeat sent"
  else
    log "WARNING: heartbeat ping failed (backup itself succeeded)"
  fi
else
  log "WARNING: BACKUP_HEARTBEAT_URL is not set — nothing will notice if backups stop"
fi

log "OK"
