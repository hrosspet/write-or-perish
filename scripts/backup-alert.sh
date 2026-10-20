#!/bin/bash
# OnFailure= handler of write-or-perish-backup.service: reports a failed
# nightly backup (docs/RUNBOOKS.md → Backups and restore).
#
#   backup-alert.sh [unit]
#
# 1. Logs the failure at error priority in the journal.
# 2. Pings BACKUP_HEARTBEAT_URL + /fail, healthchecks.io's failure signal,
#    so the monitor alerts now rather than when the success ping is overdue.
# 3. Sends an error message to Sentry when SENTRY_DSN is set. Sentry groups
#    repeated failures into one issue, so the heartbeat is the alert that
#    repeats; this one names the cause sooner.
#
# Each step runs even when the one before it failed. The message names only
# the unit; the details stay in the journal.

UNIT="${1:-write-or-perish-backup.service}"
MSG="Nightly backup failed (${UNIT}). See: journalctl -u ${UNIT} -n 100"

echo "<3>${MSG}"   # <3>: error priority in the journal

if [ -n "${BACKUP_HEARTBEAT_URL:-}" ]; then
  curl -fsS -m 10 -o /dev/null --data-raw "$MSG" "${BACKUP_HEARTBEAT_URL%/}/fail" \
    || echo "<4>backup-alert: the failure ping to the heartbeat monitor failed"
else
  echo "<4>backup-alert: BACKUP_HEARTBEAT_URL is not set"
fi

if [ -n "${SENTRY_DSN:-}" ]; then
  if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniconda3/etc/profile.d/conda.sh" && conda activate write-or-perish
  fi
  python - "$MSG" <<'EOF' || echo "<4>backup-alert: the Sentry message failed"
import os
import sys

import sentry_sdk

sentry_sdk.init(
    dsn=os.environ["SENTRY_DSN"],
    environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
    # As SENTRY_PRIVACY_OPTIONS in backend/__init__.py.
    send_default_pii=False,
    include_local_variables=False,
    max_request_body_size="never",
    traces_sample_rate=0.0,
)
sentry_sdk.capture_message(sys.argv[1], level="error")
sentry_sdk.flush(timeout=10)
EOF
fi

exit 0
