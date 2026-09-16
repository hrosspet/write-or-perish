#!/bin/bash
# Nightly backup for Loore production: PostgreSQL logical dump + media files.
#
# What it does
#   1. pg_dump (custom format, compressed) streamed straight to the destination.
#      No local temp file, so the prod disk never fills up.
#   2. Media directory (voice recordings; KMS-encrypted .enc files) synced to
#      the destination. Additive only — nothing is ever deleted there.
#      In-flight recording drafts (drafts/) are skipped: they are transient.
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
if [ -d "$AUDIO_DIR" ]; then
  log "syncing media ${AUDIO_DIR} → ${DEST}/media (additive; drafts/ excluded)"
  if [ "$MODE" = gcs ]; then
    gcloud storage rsync --quiet --recursive --exclude='^drafts/.*' "$AUDIO_DIR" "${DEST}/media"
  else
    mkdir -p "${DEST}/media"
    if command -v rsync >/dev/null; then
      rsync -a --exclude='/drafts/' "${AUDIO_DIR}/" "${DEST}/media/"
    else
      (cd "$AUDIO_DIR" && find . -path './drafts' -prune -o -type f -print0 \
        | xargs -0 -I{} sh -c 'mkdir -p "$1/$(dirname "$2")" && cp -p "$2" "$1/$2"' _ "${DEST}/media" {})
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
