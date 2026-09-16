# Operational Runbooks

Internal procedures for support / on-call. Each section is self-contained;
read top to bottom and run the SQL.

---

## Restoring a soft-deleted node within the grace window

Soft-deleted nodes are recoverable via direct SQL within
`SOFT_DELETE_GRACE_DAYS` (defined in `backend/constants.py`, currently
30 days). After that window the daily Celery cleanup task wipes content
+ versions and (when no child rows remain) deletes the row entirely.

The user-facing dialog promises "support can recover it during this
window" — this is how that happens.

### Procedure

1. **Locate the row** and check whether content has already been wiped:
   ```sql
   SELECT id, deleted_at, content IS NOT NULL AS has_content
   FROM node
   WHERE id = <X>;
   ```

2. **If `has_content` is `true`** — restore is possible:
   ```sql
   UPDATE node SET deleted_at = NULL WHERE id = <X>;
   ```

3. **If `has_content` is `false`** — content + versions have already
   been wiped by the cleanup task. Restore is no longer possible. Inform
   the user.

4. **Un-cascading descendants** — if the user originally requested
   `delete_descendants=true`, every descendant the user could edit was
   soft-deleted in the same transaction and shares the exact same
   `deleted_at` timestamp. Restore them all at once:
   ```sql
   UPDATE node SET deleted_at = NULL WHERE deleted_at = '<exact timestamp from step 1>';
   ```
   If descendants were deleted across multiple transactions (different
   timestamps), restore selectively — match each set's timestamp.

   Note: `pinned_at` was only cleared on the *root* during the original
   delete; any pinned descendants kept their `pinned_at` and pop back as
   pinned after restore. This is intentional — restore preserves prior
   state including pinning.

5. **Restoring after content-wipe** — technically possible (just clear
   `deleted_at`), but the user gets back a tombstone shell with no
   content. Generally don't bother.

### Why this is the only restore path

We deliberately did not build a user-facing restore UI in v1 (see
"Q1" in the soft-delete plan). The grace window is a backend safety
buffer for ops, not a feature. Most users won't hit this; for those
that do, support runs the SQL above.

### If you change the grace period

If `SOFT_DELETE_GRACE_DAYS` changes:

- Update `backend/constants.py`.
- Update the dialog body strings in
  `frontend/src/components/DeleteConfirmDialog.js` (or, better, route
  the constant through the API config response).
- The SQL in this runbook is value-agnostic — no edits needed beyond
  updating the section header if you want it to mention the current
  value explicitly.

---

## Backups and restore

Nightly and automated, in two layers:

| layer | what | where | retention |
|---|---|---|---|
| GCP disk snapshot schedule `default-schedule-1` | whole VM disk, crash-consistent | same GCP project as the VM | 14 days |
| `write-or-perish-backup.timer` → `scripts/backup.sh` | `pg_dump` (custom format) + media directory | `BACKUP_BUCKET` in GCS, locked retention | 30 days, **undeletable** |

The snapshot is the fast whole-VM restore. The bucket is the durable,
portable one: a *locked* retention policy means nobody can delete or
overwrite a backup for 30 days — not the project owner, not a compromised VM.

### What is (and isn't) in the bucket

- `db/<host>-<UTC stamp>.dump` — one full logical dump per night
  (`pg_dump --format=custom --no-owner --no-privileges`, compressed).
- `media/…` — voice recordings from `AUDIO_STORAGE_PATH`, synced additively.
  In-flight recording drafts (`drafts/`) are skipped; they are transient.
- **Not included:** `.env.production` (secrets), `logs/`, the frontend build.
  Keep your own copy of `.env.production` somewhere safe — a restore is
  useless without it.

**Encryption.** Node content and audio are KMS envelope-encrypted *before*
they reach the database or disk, so the dumps and media copies are
ciphertext. They are only readable together with the KMS key named by
`GCP_KMS_KEY_NAME`. Two consequences: the bucket is low-risk to store, and
**the KMS key and its old versions must never be destroyed** — that would
kill every backup at once. Rotate, don't destroy.

**Deleted data persists in backups for up to 31 days** (30-day lock, and the
lifecycle rule deletes each object on day 31). Deletion promises in the terms
and in the account-deletion feature must state this window.

### Checking that it runs

```bash
systemctl list-timers write-or-perish-backup.timer     # next / last run
systemctl status write-or-perish-backup.service        # result of the last run
journalctl -u write-or-perish-backup.service -n 50     # its log
gcloud storage ls -l gs://BUCKET/db/ | tail -5          # dumps landing?
sudo systemctl start write-or-perish-backup.service    # run one now
```

A failed run is loud in the journal; a timer that silently stopped firing is
not. Set `BACKUP_HEARTBEAT_URL` to a dead-man's-switch monitor (UptimeRobot
heartbeat, healthchecks.io) — the script pings it only after a successful run,
so the monitor alerts when backups stop.

### One-time setup

1. **Bucket** — same region as the VM (transfer inside a region is free),
   30-day retention, lifecycle deletes on day 31, no public access:

   ```bash
   PROJECT=$(gcloud config get-value project)
   BUCKET=gs://loore-prod-backups     # bucket names are global — adjust if taken
   gcloud storage buckets create "$BUCKET" --location=us-central1 \
     --uniform-bucket-level-access --public-access-prevention \
     --retention-period=30d
   gcloud storage buckets update "$BUCKET" --lifecycle-file=configs/gcs-backup-lifecycle.json
   ```

   Then lock it. **Locking is irreversible**: the period can later be raised,
   never lowered or removed, and the bucket cannot be deleted while it holds
   objects under retention.

   ```bash
   gcloud storage buckets update "$BUCKET" --lock-retention-period
   ```

2. **IAM** — the VM's service account may create and list objects, never
   delete them:

   ```bash
   SA=loore-kms-sa@$PROJECT.iam.gserviceaccount.com
   gcloud storage buckets add-iam-policy-binding "$BUCKET" \
     --member="serviceAccount:$SA" --role=roles/storage.objectCreator
   gcloud storage buckets add-iam-policy-binding "$BUCKET" \
     --member="serviceAccount:$SA" --role=roles/storage.objectViewer
   ```

3. **VM access scope** — access scopes cap what the attached service account
   can do regardless of IAM, and the VM currently has only `cloudkms`.
   Adding a storage scope needs a stop/start (about two minutes of downtime;
   the static IP is reserved and survives):

   ```bash
   gcloud compute instances stop loore-prod-a --zone us-central1-a
   gcloud compute instances set-service-account loore-prod-a --zone us-central1-a \
     --service-account "$SA" \
     --scopes https://www.googleapis.com/auth/cloudkms,https://www.googleapis.com/auth/devstorage.read_write
   gcloud compute instances start loore-prod-a --zone us-central1-a
   ```

4. **On the VM** — install the gcloud CLI if `gcloud --version` fails
   (Ubuntu: https://cloud.google.com/sdk/docs/install#deb). `gcloud auth list`
   must show the VM service account as active; if a user account is active,
   `gcloud config set account "$SA"`. Then add to `.env.production`:

   ```
   BACKUP_BUCKET=gs://loore-prod-backups
   BACKUP_HEARTBEAT_URL=        # optional
   ```

5. **Deploy** (push to `main`, or run `./deploy.sh`). It installs the unit
   and timer and arms the timer only when `BACKUP_BUCKET` is set. Then run
   one by hand and check the journal and the bucket listing.

Dry run without GCS: point `BACKUP_BUCKET` at a local directory and run
`bash scripts/backup.sh`. The script does a plain filesystem copy.

### Restore

Database — into a fresh database, then point `DATABASE_URL` at it:

```bash
gcloud storage cp gs://BUCKET/db/<dump> /tmp/restore.dump
createdb writeorperish_restored
pg_restore --no-owner --no-privileges -d writeorperish_restored /tmp/restore.dump
```

Media — back onto disk, wherever `AUDIO_STORAGE_PATH` points:

```bash
gcloud storage rsync --recursive gs://BUCKET/media "$AUDIO_STORAGE_PATH"
```

Both need the same `GCP_KMS_KEY_NAME` and a service account with the KMS
role, otherwise every node is unreadable ciphertext.

### Restore test — do it quarterly

Pull the latest dump into the local Docker Postgres. This proves the dump
format and the commands above; content stays ciphertext throughout.

```bash
gcloud storage cp "$(gcloud storage ls gs://BUCKET/db/ | tail -1)" /tmp/restore.dump
docker compose cp /tmp/restore.dump db:/tmp/restore.dump
docker compose exec db bash -c '
  createdb -U "$POSTGRES_USER" restore_test &&
  pg_restore --no-owner --no-privileges -U "$POSTGRES_USER" -d restore_test /tmp/restore.dump &&
  psql -U "$POSTGRES_USER" -d restore_test -c "select count(*) from node" &&
  dropdb -U "$POSTGRES_USER" restore_test'
```

### Off-Google copy

Disk, snapshots and bucket all live under one Google account and one billing
account. A billing failure or account loss takes them together. Once a week,
copy the latest dump (and occasionally `media/`) to a disk you own:

```bash
gcloud storage cp "$(gcloud storage ls gs://BUCKET/db/ | tail -1)" ~/loore-backups/
```

### Known limits

- Nightly dump + crash-consistent snapshot means up to 24 hours of data loss
  in the worst case. WAL archiving is the next step if that stops being
  acceptable.
- The media sync never overwrites: a file that changed after it was backed up
  stays at its first version. Audio is immutable after upload, so this is
  theoretical.
- A deletion request cannot be honoured inside backups for 31 days. That is
  the documented window, not a bug.
