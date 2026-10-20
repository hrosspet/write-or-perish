#!/bin/bash
# Nightly backup for Loore production: PostgreSQL logical dump + media files.
#
# What it does
#   1. pg_dump (custom format, compressed) streamed straight to the destination.
#      No local temp file, so the prod disk never fills up.
#   2. Media directory (voice recordings; KMS-encrypted .enc files) synced to
#      media/<period>/ at the destination, one folder per 14-day period (see
#      MEDIA_PERIOD_DAYS below). Additive only — nothing is ever deleted or
#      overwritten there. In-flight recording drafts (drafts/) are skipped:
#      they are transient.
#   3. On success, pings BACKUP_HEARTBEAT_URL (optional) so a dead-man's-switch
#      monitor can alert when backups silently stop running.
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
#   AUDIO_STORAGE_PATH    optional, default data/audio (relative to cwd)
#   BACKUP_HEARTBEAT_URL  optional
#
# Exits non-zero on any failure so systemd marks the unit failed and it shows
# up in `systemctl --failed` and the journal. Runbook: docs/RUNBOOKS.md.

set -euo pipefail

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] backup: $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set"
[ -n "${BACKUP_BUCKET:-}" ] || die "BACKUP_BUCKET is not set — backups are NOT running. Set it in .env.production (docs/RUNBOOKS.md → Backups)."

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

case "$DEST" in
  gs://*) MODE=gcs ;;
  *)      MODE=local ;;
esac

command -v pg_dump >/dev/null || die "pg_dump not found on PATH"
if [ "$MODE" = gcs ]; then
  command -v gcloud >/dev/null || die "gcloud not found — install google-cloud-cli (docs/RUNBOOKS.md → Backups → one-time setup)"
fi

# --- 1. database -----------------------------------------------------------
# --no-owner/--no-privileges: restores cleanly into a DB owned by a different
# role (local restore test, a rebuilt VM, the lab).
log "dumping database → ${DEST}/db/${DUMP_NAME}"
if [ "$MODE" = gcs ]; then
  # pipefail keeps pg_dump's exit status; a failed upload never leaves a
  # partial object because GCS object uploads are atomic.
  pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
    | gcloud storage cp --quiet - "${DEST}/db/${DUMP_NAME}"
  SIZE="$(gcloud storage objects describe "${DEST}/db/${DUMP_NAME}" --format='value(size)' 2>/dev/null || echo '?')"
else
  mkdir -p "${DEST}/db"
  pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
    > "${DEST}/db/${DUMP_NAME}"
  SIZE="$(wc -c < "${DEST}/db/${DUMP_NAME}" | tr -d ' ')"
fi
log "database dump done (${SIZE} bytes)"

# --- 2. media --------------------------------------------------------------
# --no-clobber: a file that changed after its upload this period (a TTS file
# regenerated after an edit) is skipped, not overwritten. The bucket refuses
# overwrites under retention, which would fail the whole run; the new version
# lands with the next period's full copy.
if [ -d "$AUDIO_DIR" ]; then
  log "syncing media ${AUDIO_DIR} → ${MEDIA_DEST} (additive; drafts/ excluded)"
  if [ "$MODE" = gcs ]; then
    gcloud storage rsync --quiet --recursive --no-clobber --exclude='^drafts/.*' "$AUDIO_DIR" "$MEDIA_DEST"
  else
    mkdir -p "$MEDIA_DEST"
    if command -v rsync >/dev/null; then
      rsync -a --ignore-existing --exclude='/drafts/' "${AUDIO_DIR}/" "${MEDIA_DEST}/"
    else
      (cd "$AUDIO_DIR" && find . -path './drafts' -prune -o -type f -print0 \
        | xargs -0 -I{} sh -c '[ -e "$1/$2" ] || { mkdir -p "$1/$(dirname "$2")" && cp -p "$2" "$1/$2"; }' _ "$MEDIA_DEST" {})
    fi
  fi
  log "media sync done"
else
  log "media dir ${AUDIO_DIR} does not exist — skipping media"
fi

# --- 3. heartbeat ----------------------------------------------------------
if [ -n "${BACKUP_HEARTBEAT_URL:-}" ]; then
  if curl -fsS -m 10 -o /dev/null "$BACKUP_HEARTBEAT_URL"; then
    log "heartbeat sent"
  else
    log "WARNING: heartbeat ping failed (backup itself succeeded)"
  fi
fi

log "OK"
