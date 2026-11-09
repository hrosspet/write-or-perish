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
| `write-or-perish-backup.timer` → `scripts/backup.sh` | `pg_dump` (custom format) + media directory | `BACKUP_BUCKET` in GCS, locked retention | 28 days, **undeletable** |

The snapshot is the fast whole-VM restore. The bucket is the durable,
portable one: once its retention policy is *locked*, nobody can delete or
overwrite a backup for 28 days — not the project owner, not a compromised VM.

**Why 28 days.** The terms say that after an account is deleted for good,
"Copies in our backups are erased within another 30 days"
(`frontend/src/components/TermsModal.js`,
`ios/Loore/Features/Onboarding/TermsText.swift`; the deletion email in
`backend/utils/email.py` says the same). The lifecycle rule deletes each
object at age 28, but Cloud Storage runs lifecycle actions asynchronously,
so a deletion can happen some time after the object reaches that age (in
practice within about a day; Google gives no bound). 28 leaves that margin
inside the 30 days. The retention period and
the lifecycle age are both 28: a lifecycle rule never deletes an object
before it has fulfilled the retention policy, so the two don't race.
Soft delete is off on the bucket. With Google's default 7-day soft delete, a
deleted object would stay restorable, and billed, for 7 more days: about 36
days in total.

### What is (and isn't) in the bucket

- `db/<host>-<UTC stamp>.dump` — one full logical dump per night
  (`pg_dump --format=custom --no-owner --no-privileges`, compressed), so
  about 28 dumps at any time.
- `db/<host>-<UTC stamp>.dump.ok` — the dump's size in bytes, written only
  after `pg_dump` and the upload have both succeeded. **Restore only a dump
  that has its `.ok` marker.** A dump without one is from a failed run: when
  `pg_dump` dies midway, gcloud still finishes uploading what it received,
  and under retention that truncated object can't be deleted. It ages out
  with the rest.
- `media/<period start>/…` — voice recordings and TTS audio from
  `AUDIO_STORAGE_PATH`, one folder per 14-day period, named after the
  period's first day (UTC). The first run of a period uploads a full copy
  (all the media, so that night's run takes longer); later runs in the
  period add new files only. The newest folder holds every file that
  existed at the last run; the folder before it stays until the lifecycle
  deletes it, about 14 days into the current period.
- **Skipped from the media:** the transient folders `drafts/` (voice-mode
  drafts), `chunks/` (chunked uploads being assembled, unencrypted) and
  `streaming/` (live recording chunks, batched into `nodes/` at the end);
  and, with encryption on, every file that does not end in `.enc`. Every
  audio writer calls `encrypt_file()`, which writes `X.enc` and then deletes
  `X`, for public and private nodes alike, so a plain file is one about to
  be replaced, being assembled, or an ffmpeg temp file. The
  `.legacy_backup` rollback copies of `backfill_legacy_chunks.py` are
  skipped by the same rule; they are never served.
- **Not included:** `.env.production` (secrets), `logs/`, the frontend build.
  Keep your own copy of `.env.production` somewhere safe — a restore is
  useless without it.

Why one folder per period: with a single additive `media/` folder, the
lifecycle rule would delete every media copy on day 28 and only the next
night's run would upload it again, so for up to a day every 28 days the
bucket would hold no copy of the recordings. Every object in the current
period's folder is less than 14 days old, so none of them has reached the
28-day delete.

The media directory is live. A file deleted between gcloud's listing and its
upload (a purge, a finished recording) makes the rsync fail, so the script
runs it a second time; the new listing no longer has the file, and
`--no-clobber` skips what the first attempt uploaded. The media sync runs
even when the dump failed.

**Encryption.** Node content and audio are KMS envelope-encrypted *before*
they reach the database or disk, so the dumps and media copies are
ciphertext. They are only readable together with the KMS key named by
`GCP_KMS_KEY_NAME`. Two consequences: the bucket is low-risk to store, and
**the KMS key and its old versions must never be destroyed** — that would
kill every backup at once. Rotate, don't destroy.

**Deleted data stays in the backups for up to about 29 days.** The last dump
and media copy made before a deletion are deleted at age 28, plus the
lifecycle's lag. That is inside the 30 days the terms promise. Every other
copy of a dump or of the media (the restore test, the off-Google copy) must
be gone by day 28 too.

### Checking that it runs

```bash
systemctl list-timers write-or-perish-backup.timer     # next / last run
systemctl status write-or-perish-backup.service        # result of the last run
journalctl -u write-or-perish-backup.service -n 50     # its log
journalctl -u write-or-perish-backup-alert.service -n 20   # failure reports
gcloud storage ls -l gs://BUCKET/db/ | tail -6          # dumps and .ok markers landing?
gcloud storage ls gs://BUCKET/media/                    # one folder per 14-day period
sudo systemctl start write-or-perish-backup.service    # run one now
```

Two alerts, both set up in the one-time setup:

- **Heartbeat** (`BACKUP_HEARTBEAT_URL`, required): the script pings it after
  every successful run. The monitor alerts when a ping is overdue, which
  catches a failing run and also a timer that never fires, for example on a
  new VM whose `.env.production` lacks `BACKUP_BUCKET`.
- **`OnFailure=`**: a failed run (or one killed by OOM or a timeout) starts
  `write-or-perish-backup-alert.service` (`scripts/backup-alert.sh`). It logs
  the failure at error priority, pings `BACKUP_HEARTBEAT_URL/fail` so the
  monitor alerts at once, and sends an error to Sentry. Sentry groups repeated
  failures into one issue and emails once; the heartbeat keeps alerting.

### One-time setup

Locking the retention policy is irreversible, so it is the last step: after
a nightly run and a test restore have worked. Until then a setup mistake can
still be corrected.

1. **VM access scope** — ✅ done on 2026-10-20 for `loore-prod-a`. Any new
   prod VM (for example after the planned VM move) needs the same scope.
   Access scopes cap what the attached service account can do regardless of
   IAM, and without a storage scope the VM cannot write to the bucket.
   Adding a scope needs a stop/start (about two minutes of downtime; the
   static IP is reserved and survives). For a new VM, use its name and zone:

   ```bash
   PROJECT=$(gcloud config get-value project)
   SA=loore-kms-sa@$PROJECT.iam.gserviceaccount.com
   gcloud compute instances stop loore-prod-a --zone us-central1-a
   gcloud compute instances set-service-account loore-prod-a --zone us-central1-a \
     --service-account "$SA" \
     --scopes https://www.googleapis.com/auth/cloudkms,https://www.googleapis.com/auth/devstorage.read_write
   gcloud compute instances start loore-prod-a --zone us-central1-a
   ```

2. **Bucket** — same region as the VM (transfer inside a region is free),
   28-day retention (not locked yet), soft delete off, lifecycle deletes at
   age 28, no public access:

   ```bash
   PROJECT=$(gcloud config get-value project)
   BUCKET=gs://loore-prod-backups     # bucket names are global — adjust if taken
   gcloud storage buckets create "$BUCKET" --location=us-central1 \
     --uniform-bucket-level-access --public-access-prevention \
     --retention-period=28d --soft-delete-duration=0
   gcloud storage buckets update "$BUCKET" --lifecycle-file=configs/gcs-backup-lifecycle.json
   gcloud storage buckets describe "$BUCKET" \
     --format="default(retention_policy,soft_delete_policy,lifecycle_config)"
   ```

   The describe must show a retention period of `2419200` seconds (28 days),
   not locked; a soft delete retention of `0`; and one `Delete` rule at age
   28. If soft delete still shows 7 days, run
   `gcloud storage buckets update --clear-soft-delete "$BUCKET"`.

3. **IAM** — the VM's service account may create and list objects, never
   delete them:

   ```bash
   SA=loore-kms-sa@$PROJECT.iam.gserviceaccount.com
   gcloud storage buckets add-iam-policy-binding "$BUCKET" \
     --member="serviceAccount:$SA" --role=roles/storage.objectCreator
   gcloud storage buckets add-iam-policy-binding "$BUCKET" \
     --member="serviceAccount:$SA" --role=roles/storage.objectViewer
   ```

4. **Heartbeat and the VM** — required, not optional:
   - Create a check on healthchecks.io (free plan): period 1 day, grace
     6 hours, email alerts on. Its ping URL is `BACKUP_HEARTBEAT_URL`.
     Another dead-man's-switch monitor works too; the `/fail` ping is
     healthchecks.io's and is simply ignored elsewhere.
   - Install the gcloud CLI on the VM if `gcloud --version` fails (Ubuntu:
     https://cloud.google.com/sdk/docs/install#deb). `gcloud auth list` must
     show the VM service account as active; if a user account is active,
     `gcloud config set account "$SA"`.
   - Add to `.env.production`:

     ```
     BACKUP_BUCKET=gs://loore-prod-backups
     BACKUP_HEARTBEAT_URL=https://hc-ping.com/<check-uuid>
     ```

5. **Deploy** (push to `main`, or run `./deploy.sh`). It installs the backup
   unit, its timer and the alert unit, and arms the timer only when
   `BACKUP_BUCKET` is set (it warns when `BACKUP_HEARTBEAT_URL` is missing).
   Then:
   - run one by hand, `sudo systemctl start write-or-perish-backup.service`,
     and check the journal and the bucket listing (see "Checking that it
     runs"): a dump with its `.ok` marker, and a `media/<period start>/`
     folder. The healthchecks.io check turns green. An `ERROR:` line about
     parallel composite uploads for a large file is harmless: with these
     roles gcloud can't read the bucket's retention setting, and the file is
     uploaded normally.
   - test the alert once: `sudo systemctl start write-or-perish-backup-alert.service`.
     The check turns red and emails you, Sentry shows "Nightly backup
     failed…", and the alert unit's journal has the error line. Run the
     backup by hand again to turn the check green.

6. **First nightly run** — the next day after 08:30 UTC, check that the timer
   ran it by itself: `systemctl list-timers write-or-perish-backup.timer`
   shows a last run, `systemctl status write-or-perish-backup.service` shows
   success, `db/` holds a second dump with its marker, and the
   healthchecks.io check logged a ping at about that time.

7. **Plaintext check** — on the VM, from the project directory. It must
   print 0:

   ```bash
   A="${AUDIO_STORAGE_PATH:-data/audio}"
   find "$A/user" "$A/nodes" -type f ! -name '*.enc' ! -name '*.legacy_backup' -mmin +60 | wc -l
   ```

   These are plain audio files older than an hour, which the backup skips.
   If there are any, list them (`… | head`, paths only) before going on. A
   file whose `.enc` sibling exists is left over from a crash between
   writing and encrypting; delete it (the app would serve it before the
   complete `.enc`). One without a sibling is audio stored before audio
   encryption was turned on: encrypt it (docs/GCP-KMS-SETUP.md, "Encrypt
   Audio Files"; one KMS call per file) so it gets backed up.

8. **Test restore** — run "Restore test" below. It must pass.

9. **Lock the retention policy.** **Irreversible**: the period can later be
   raised, never lowered or removed, and the bucket cannot be deleted while
   it holds objects under retention.

   ```bash
   gcloud storage buckets update "$BUCKET" --lock-retention-period
   gcloud storage buckets describe "$BUCKET" --format="default(retention_policy)"   # isLocked: true
   ```

   Before this step a mistake can be undone:
   `gcloud storage buckets update "$BUCKET" --clear-retention-period` removes
   the policy, after which the objects (`gcloud storage rm "$BUCKET/**"`) and
   the bucket can be deleted, and setup can restart at step 2.

Dry run without GCS: point `BACKUP_BUCKET` at a local directory and run
`bash scripts/backup.sh`. The script does a plain filesystem copy with the
same layout, markers and skip rules.

### Restore

Database — the newest dump that has its `.ok` marker, into a fresh
database, then point `DATABASE_URL` at it:

```bash
DUMP=$(gcloud storage ls gs://BUCKET/db/ | grep '\.dump\.ok$' | tail -1 | sed 's/\.ok$//')
gcloud storage cp "$DUMP" /tmp/restore.dump
createdb writeorperish_restored
pg_restore --no-owner --no-privileges -d writeorperish_restored /tmp/restore.dump
```

Purges re-run by themselves. Both kinds (account deletion, "Delete all my
writing") run 30 days after the request, and the oldest dump is 28 days old,
so a purge that ran after the dump was made is still scheduled in it, and
the beat runs it again.

Media — back onto disk, wherever `AUDIO_STORAGE_PATH` points. The newest
`media/<period start>/` folder holds every file that existed at the last run.
Restore the folder before it first, then the newest one, so that a newest
folder whose first run did not finish is completed from the older one:

```bash
gcloud storage ls gs://BUCKET/media/        # oldest first
gcloud storage rsync --recursive gs://BUCKET/media/<second newest> "$AUDIO_STORAGE_PATH"
gcloud storage rsync --recursive gs://BUCKET/media/<newest> "$AUDIO_STORAGE_PATH"
```

Then two clean-ups in `flask shell` on the restored instance.

The media folders are additive, so they also hold files deleted after their
upload: recordings of accounts deleted for good, of "Delete all my writing",
and the AI replies' audio, which lives under the model account's folder.
Nothing would ever delete these files again, and every new period would back
them up anew. Delete each folder whose node, profile, reference or user is
not in the restored database:

```python
import shutil
from backend.extensions import db
from backend.models import Node, User, UserProfile, ExternalItem
from backend.utils.audio_storage import AUDIO_STORAGE_ROOT as root
ids = lambda cls: {str(i) for (i,) in db.session.query(cls.id)}
nodes, users, profiles, items = ids(Node), ids(User), ids(UserProfile), ids(ExternalItem)
gone = [p for p in root.glob("user/*/node/*") if p.name not in nodes]
gone += [p for p in root.glob("nodes/*/*") if p.name not in nodes]
gone += [p for p in root.glob("user/*/profile/*") if p.name not in profiles]
gone += [p for p in root.glob("user/*/item/*") if p.name not in items]
gone += [p for p in root.glob("user/*") if p.name not in users]
gone += [p for p in root.glob("nodes/*") if p.name not in users]
print(len(gone), "folders to delete")
for p in gone:
    shutil.rmtree(p, ignore_errors=True)

```

TTS regenerated after a period's first upload is kept in that folder in its
old version (the sync never overwrites), while the restored `TTSChunk` rows
describe the new one. Clear the TTS of everything regenerated since the
newest folder's date, so it is generated again when played:

```python
from datetime import datetime
from backend.extensions import db
from backend.models import Node, UserProfile, ExternalItem, TTSChunk
from backend.utils.audio_storage import clear_tts_artifacts
cut = datetime(2026, 10, 22)   # the newest media/ folder's date (UTC)
rows = TTSChunk.query.filter(TTSChunk.completed_at >= cut).all()
for cls, key in ((Node, "node_id"), (UserProfile, "profile_id"), (ExternalItem, "item_id")):
    for e in cls.query.filter(cls.id.in_({getattr(r, key) for r in rows} - {None})):
        clear_tts_artifacts(e)

db.session.commit()
```

Both need the same `GCP_KMS_KEY_NAME` and a service account with the KMS
role, otherwise every node is unreadable ciphertext.

### Restore test — before the lock, then quarterly

Run it on your own machine, not on the prod VM (its disk and memory are
small). It restores the newest complete dump into a scratch database in the
local Docker Postgres and compares row counts with prod. Content stays
ciphertext throughout.

```bash
DUMP=$(gcloud storage ls gs://BUCKET/db/ | grep '\.dump\.ok$' | tail -1 | sed 's/\.ok$//')
gcloud storage cp "$DUMP" /tmp/restore.dump
docker compose cp /tmp/restore.dump db:/tmp/restore.dump
docker compose exec db bash -c '
  createdb -U "$POSTGRES_USER" restore_test &&
  pg_restore --no-owner --no-privileges --exit-on-error -U "$POSTGRES_USER" -d restore_test /tmp/restore.dump &&
  psql -U "$POSTGRES_USER" -d restore_test -c "select count(*) as nodes from node" -c "select count(*) as users from \"user\""'
```

It passes when `pg_restore` finishes without an error and the two counts
match prod's (`flask shell` on the VM: `Node.query.count()`,
`User.query.count()`), or are a little lower because of rows added since the
dump. If `pg_restore` stops with "unsupported version … in file header",
the local Postgres is older than prod's: run the test in a `postgres` image
of prod's major version.

Media — on the VM, from the project directory, compare the number of files
the backup takes with the number of objects in the newest period folder (the
bucket can hold more: files deleted since the period began; or fewer: files
added since the last run):

```bash
A="${AUDIO_STORAGE_PATH:-data/audio}"
find "$A" -type f -name '*.enc' ! -path "$A/drafts/*" ! -path "$A/chunks/*" ! -path "$A/streaming/*" | wc -l
gcloud storage ls "$(gcloud storage ls gs://BUCKET/media/ | tail -1)**" | wc -l
```

Then delete the local copies; the deletion promise covers them too:

```bash
docker compose exec db bash -c 'dropdb -U "$POSTGRES_USER" restore_test && rm /tmp/restore.dump'
rm /tmp/restore.dump
```

### Off-Google copy

Disk, snapshots and bucket all live under one Google account and one billing
account. A billing failure or account loss takes them together. Once a week,
copy the latest complete dump to a disk you own. The deletion promise in the
terms covers this copy too, so delete old copies in the same step:

```bash
DUMP=$(gcloud storage ls gs://BUCKET/db/ | grep '\.dump\.ok$' | tail -1 | sed 's/\.ok$//')
gcloud storage cp "$DUMP" ~/loore-backups/
find ~/loore-backups -name '*.dump' -mtime +20 -delete   # copied weekly, each one is gone by day 28
```

A copy of the media follows the same rule, counted from the date in the
folder's name, not from the day you copy it: the folder holds files uploaded
since that date, including those of accounts deleted since. Delete a local
copy of `media/2026-10-22/` by 2026-11-19, whenever you made it.

### Known limits

- Nightly dump + crash-consistent snapshot means up to 24 hours of data loss
  in the worst case. WAL archiving is the next step if that stops being
  acceptable.
- A restore loses everything after the dump, deletion requests included: an
  account deletion or "Delete all my writing" requested after the dump's
  time has to be requested again.
- The media sync never overwrites (the bucket refuses overwrites under
  retention). A file that changed after its upload in the current period,
  such as a TTS file regenerated after an edit, keeps its first version in
  that period's folder; the new version arrives with the next period's full
  copy, within 14 days. See "Restore" for clearing such TTS after a restore.
- A media restore also brings back files deleted after their upload; see
  "Restore" for the cleanup.
- A deletion request cannot be honoured inside backups for up to about 29
  days (28 plus the lifecycle's lag). That is the documented window, inside
  the 30 days the terms promise, not a bug.

---

## Celery queue split

Issue #379. By default one Celery worker (`write-or-perish-celery`, 4
pool processes) runs every task, so a reply or a voice turn can wait behind
profile builds and imports, and during a deploy it waits while that worker
drains. The split runs two workers instead:

| worker | unit | reads | runs |
|---|---|---|---|
| interactive | `write-or-perish-celery-interactive` | `interactive` | replies (text, voice, Read/Glean), the voice cache warm, transcription, TTS, the per-user spend-cap check |
| background | `write-or-perish-celery-background` | `background`, `celery` | profile builds, recent context, intentions, digests, poll drafts, imports and pre-fills, syncs, sweeps, purges; on `celery`: Celery's own tasks and anything queued before the switch |

The full table is `TASK_QUEUES` in `backend/celery_queues.py`; a test fails
when a task is missing from it. The unit files are in `deploy/systemd/`.

Two protections work with the split off and on (#613): a memory limit on
the worker that runs the heavy jobs ("Memory limits" below), and a slot
that runs one heavy job at a time ("Heavy-job slot" below). Both are live
from the first deploy that carries them; only the split itself waits for
the switch.

The split is off until `CELERY_SPLIT_QUEUES=true` is in `.env.production`. Off, no
routes are set and every task goes to the default queue `celery`, as before.

What `deploy.sh` does:

- No split unit enabled: what it did before the split (installs, enables
  and restarts `write-or-perish-celery`).
- A split unit enabled: copies both unit files from `deploy/systemd/` when
  they changed, restarts both workers in one call (they drain at the same
  time, 90 s each), also restarts the single worker while it still runs,
  and no longer enables it. It never installs or enables a split unit
  itself.
- `CELERY_SPLIT_QUEUES` on without an enabled split unit: the deploy turns
  red, because tasks would wait in queues nobody reads. On while the
  single worker still runs: a warning.

### Memory limits

The single worker (`write-or-perish-celery.service`, in the repo root) and
the background worker (`deploy/systemd/write-or-perish-celery-background.service`)
have a memory limit; the interactive worker has none (its tasks are short
and small, and a limit there would cut off replies):

| unit | `MemoryMax` | `MemorySwapMax` | `OOMPolicy` |
|---|---|---|---|
| `write-or-perish-celery` | 1408M | 0 | `continue` |
| `write-or-perish-celery-background` | 1152M | 0 | `continue` |
| `write-or-perish-celery-interactive` | none | none | `continue` |

Sized for the production host: room for the worker's main process, its
pool processes and one heavy job (the heavy-job slot keeps it to one).

What happens at the limit: the kernel's OOM killer kills one process of
the unit, a pool process: each pool process raises its own
`oom_score_adj` by 300 when it starts (`backend/utils/worker_oom.py`), so
it is picked before the main process, and the one that grew is the
largest. The main process logs `Process 'ForkPoolWorker-N' ... exited with
'signal 9 (SIGKILL)'` and the task's `WorkerLostError`, and starts a new
pool process. A task with `acks_late` and `reject_on_worker_lost` (the
profile window advance, ChatGPT/Claude imports) goes back to the queue;
any other task is lost and reported. A task id killed 3 times is not run
again ("Heavy-job slot" below). `OOMPolicy=continue` matters only on
cgroup v2: there, with systemd's default (`stop`), systemd stops the whole
unit after an OOM kill in it, with every task it runs. On cgroup v1
systemd does not see the kill, and the unit stays up whatever the policy.
`MemorySwapMax=0` keeps the limit the same on a server with swap: the job
is killed, not moved to swap.

A pool process whose peak memory reached about 400 MB
(`worker_max_memory_per_child` in `backend/celery_app.py`; a pool process
at rest uses about half) is replaced once its task ends, so a heavy job's
peak does not stay with an idle process. The worker log then says
`child process exiting after exceeding memory limit (...KiB / 409600KiB)`
at WARNING; that is expected after a heavy job.

There is no `MemoryHigh`. Without swap it frees nothing; it holds a job
that grows past it in a crawl (in a test unit with a 10 % band, about a
minute and a half at 200 MB, longer at the workers' size) before the
kill, and slows every process in the unit meanwhile, which in the single
worker includes replies. Past `MemoryMax` alone, the job is killed at
once.

`deploy.sh` copies `write-or-perish-celery.service` when it changed, runs
`daemon-reload` and restarts the worker, so the limit applies from that
deploy. It copies the split units only once one is enabled (see above);
the first copy is step 2 of "Switching on".

Check what applies and what the worker uses now. `systemctl show` prints
the configured values; the cgroup file is what the kernel enforces. Both
should print `1476395008` for the single worker (1152M, `1207959552`, for
the background worker):

```bash
systemctl show -p MemoryMax -p MemorySwapMax -p OOMPolicy -p MemoryCurrent write-or-perish-celery
stat -fc %T /sys/fs/cgroup   # cgroup2fs: cgroup v2; tmpfs: cgroup v1
# cgroup v2:
cat /sys/fs/cgroup/system.slice/write-or-perish-celery.service/memory.max
# cgroup v1:
cat /sys/fs/cgroup/memory/system.slice/write-or-perish-celery.service/memory.limit_in_bytes
```

On cgroup v1 systemd writes `MemoryMax` as the v1 memory limit, and
`MemorySwapMax` has no effect (there is no swap to limit on a server
without one). `MemoryCurrent` counts the worker's file cache too, which
the kernel reclaims before it kills anything.

An OOM kill shows in the kernel log, under both cgroup versions, and in
the worker log (`journalctl -u` shows it only on cgroup v2, where systemd
watches for it):

```bash
journalctl -k --since "1 hour ago" | grep -iE "out of memory|oom-kill"
grep -E "SIGKILL|WorkerLostError" logs/celery-worker.log | tail   # celery-background.log with the split
```

Adjust with a drop-in, which deploys do not overwrite (they copy only the
unit file itself), for example after measuring:

```bash
sudo systemctl edit write-or-perish-celery   # or write-or-perish-celery-background
# in the editor:
#   [Service]
#   MemoryMax=1536M
sudo systemctl daemon-reload
sudo systemctl restart write-or-perish-celery   # 90 s drain, as in a deploy
```

`sudo systemctl revert write-or-perish-celery` removes the drop-in. A
lasting change of the default belongs in the repo's unit file.

### Heavy-job slot

The heavy jobs run one at a time, on whichever worker runs them:

- profile window passes (`profile_windows.advance_profile_windows`) and
  the synchronous profile task (`exports.update_user_profile`), which
  runs the same window pass inside it for a large build;
- imports: `chat_imports.import_conversations`,
  `imports.import_twitter_archive`;
- pre-fills: `imports.prefill_community_archive`, `imports.prefill_x_api`;
- the Community Archive snapshot refresh
  (`imports.refresh_community_archive_snapshot`).

They use `base=HeavyTask` (`backend/utils/heavy_slot.py`); a test lists
them. A heavy task takes the Redis key `loore:heavy_slot` before it runs.
When another heavy job holds it, the task is queued again in 30–40 s on
the queue it came from, with the same task id, arguments and retry count,
and shows as RETRY (an import's status reads "queued"). It is never
dropped. The holder refreshes the key's 300 s TTL every 30 s from a
heartbeat thread and deletes it when the task ends, with success or with
an error; when the holder is killed (the memory limit, the hard time
limit), the key expires within 300 s. A heartbeat stops by itself after
the task's hard time limit. When Redis cannot be reached, a heavy task
runs without the slot.

A run that is killed (the memory limit, the hard time limit) is not run
again for ever. Each start of a task id is counted under
`loore:heavy_runs:<task id>` (kept 24 h), and a run that ends in any
other way deletes the count. When a task id was killed 3 times, its next
start does not run it: the task fails (FAILURE), the message is acked,
the slot is freed and the worker logs `... was killed in 3 runs ...; it
is not run again` at ERROR (Sentry). A ChatGPT/Claude import then shows
as failed to the user ("The import stopped because it ran out of memory.
Press Confirm Import again to continue where it stopped."), and its
import lock is released so Confirm starts it at once. A profile window
build shows `Profile window build failed: HeavyJobKilled` in the admin's
Profile column, its guard is cleared, and the hourly seeder starts it
again under a new task id. Only tasks with `acks_late` and
`reject_on_worker_lost` come back after a kill, so only they reach the
count; any other killed task fails at once, as before.

```bash
redis-cli get loore:heavy_slot   # "<task name>[<task id>] <token>", or (nil)
redis-cli ttl loore:heavy_slot
grep "waiting for the heavy-job slot" logs/celery-worker.log | tail   # celery-background.log with the split
```

`redis-cli del loore:heavy_slot` frees it by hand. Only do that when the
task it names no longer runs (`celery -A backend.celery_app:celery
inspect active`): otherwise two heavy jobs run at once until it ends.

### Memory

Before switching on, on the VM:

```bash
free -m
# What the current worker uses (PSS counts shared pages once)
for p in $(pgrep -f 'backend.celery_app:celery worker'); do grep '^Pss:' /proc/$p/smaps_rollup; done | awk '{s+=$2} END {printf "%.0f MB\n", s/1024}'
pgrep -fc 'backend.celery_app:celery worker'   # 5: the main process and 4 pool processes
```

The two workers run 7 processes instead of 5 (each a main process, plus 3
and 2 pool processes), so expect them to use about 7/5 of the figure above
once they have run for a while. During steps 2–5 below the single worker
runs beside them; the heavy-job slot keeps it to one heavy job across all
three. If `available` in `free -m` is not well above that estimate, lower
`--concurrency` in the unit files first (to 2 and 1), or free memory for
the switch (for example `docker compose -p wop-staging stop`, and `start`
afterwards). Change the numbers in `deploy/systemd/` in the repo, not in
`/etc/systemd/system/`: once a split unit is enabled, every deploy copies
the repo's files over the installed ones (a drop-in, as in "Memory
limits", stays).

Concurrency. The interactive worker's 3 pool processes are also how many
voice turns can end at the same moment: a finishing voice turn holds one
process while it waits (up to 10 minutes) for its last chunks, and their
transcription needs another. When every process is held by a waiting
turn, the transcriptions wait until those turns time out. Before the split
the limit was 4 minus the background jobs running at that moment; with
memory to spare, 4 interactive processes keep it at 4. The background
worker's 2 processes let one long job (an import, an export, the Community
Archive download) run while the every-minute polls go on.

### Switching on

The order matters: every queue gets a reader before any task is routed to
it, and the single worker stops only once nothing new reaches its queue
that the background worker does not also read. Run in `~/write-or-perish`:

1. **Check the code is deployed and the limits apply:**
   `grep -c celery_queues backend/celery_app.py` prints a number above 0,
   and the limit applies to the single worker (the `systemctl show` and
   cgroup file checks in "Memory limits"). Start when no heavy
   job runs (`redis-cli get loore:heavy_slot` prints `(nil)`), so the
   three workers of steps 2–5 do not run beside a job near its limit.
2. **Install and start both workers.** Nothing is routed to their queues
   yet; the background worker shares the default queue with the single
   worker.

   ```bash
   sudo cp deploy/systemd/write-or-perish-celery-interactive.service deploy/systemd/write-or-perish-celery-background.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now write-or-perish-celery-interactive write-or-perish-celery-background
   systemctl is-active write-or-perish-celery-interactive write-or-perish-celery-background
   systemctl show -p MemoryMax -p OOMPolicy write-or-perish-celery-background
   # and the cgroup file, as in "Memory limits" (1207959552)
   ```

3. **Turn the routing on** (check there is no other `CELERY_SPLIT_QUEUES`
   line first):

   ```bash
   grep -n CELERY_SPLIT_QUEUES .env.production
   echo 'CELERY_SPLIT_QUEUES=true' >> .env.production
   ```

4. **Restart every process that queues tasks**, so each reads the setting:
   both new workers (a voice turn queues its reply from a worker), beat
   (with the schedule-file cleanup `deploy.sh` does) and the site. The site
   needs a full restart, a few seconds of 502s: a reload does not re-read
   the env file.

   ```bash
   sudo systemctl restart write-or-perish-celery-interactive write-or-perish-celery-background
   sudo systemctl stop write-or-perish-celery-beat && rm -f celerybeat-schedule celerybeat-schedule.* celery-beat.pid && sudo systemctl start write-or-perish-celery-beat
   sudo systemctl restart write-or-perish
   ```

5. **Stop the single worker.** Its warm shutdown finishes what it is
   running (up to 90 s) and puts what it had reserved or scheduled (tasks
   held until a set time) back on the default queue, which the background
   worker reads, so this step needs no `inspect` check. The same holds for
   the restarts in step 4: each worker reads the queues its held tasks go
   back to.

   ```bash
   sudo systemctl disable --now write-or-perish-celery
   ```

6. **Check** that each worker reads its queues, the queues do not grow, a
   reply runs on the interactive worker, and memory is fine:

   ```bash
   source ~/miniconda3/etc/profile.d/conda.sh && conda activate write-or-perish
   celery -A backend.celery_app:celery inspect active_queues
   redis-cli llen interactive; redis-cli llen background; redis-cli llen celery
   # send a reply on the site, then:
   grep generate_llm_response logs/celery-interactive.log | tail -3
   tail -5 logs/celery-background.log   # the every-minute batch polls
   free -m
   systemctl show -p MemoryCurrent write-or-perish-celery-interactive write-or-perish-celery-background
   ```

   The next deploy's log lists `Celery workers:
   write-or-perish-celery-interactive write-or-perish-celery-background`.

### Switching off

The same steps in reverse. The two workers keep reading their queues until
those are empty, so nothing is left in a queue nobody reads:

1. Remove the setting and restart the processes that queue tasks. From
   then on every new task goes to the default queue, which the background
   worker reads:

   ```bash
   sed -i '/^CELERY_SPLIT_QUEUES=/d' .env.production
   sudo systemctl restart write-or-perish-celery-interactive write-or-perish-celery-background
   sudo systemctl stop write-or-perish-celery-beat && rm -f celerybeat-schedule celerybeat-schedule.* celery-beat.pid && sudo systemctl start write-or-perish-celery-beat
   sudo systemctl restart write-or-perish
   ```

2. Start the single worker: `sudo systemctl enable --now write-or-perish-celery`.
3. Stop the two workers once nothing is left for them. `redis-cli llen
   interactive` and `redis-cli llen background` stay at 0, and `inspect
   active`, `inspect reserved` and `inspect scheduled` list nothing under
   `interactive@…` and `background@…`, except at most a Read's batch poll
   (`generate_llm_response`, see below). A scheduled task is one a worker
   holds until a set time: an import's retry after its time limit, the
   spaced bookmark syncs, the spend-cap check 15 s after a call, a heavy
   job waiting for the heavy-job slot. Stopped before that time, it goes
   back to a queue nobody reads. A heavy job that waits keeps coming back
   to the background queue until it gets the slot and runs there, so wait
   for it too.

   ```bash
   source ~/miniconda3/etc/profile.d/conda.sh && conda activate write-or-perish
   redis-cli llen interactive; redis-cli llen background
   celery -A backend.celery_app:celery inspect active
   celery -A backend.celery_app:celery inspect reserved
   celery -A backend.celery_app:celery inspect scheduled
   # all empty for interactive@… and background@…:
   sudo systemctl disable --now write-or-perish-celery-interactive write-or-perish-celery-background
   ```

   A task a stopping worker still held goes back to its queue, which
   nobody reads any more. These recover on their own: a Read's batch poll
   (it re-queues itself to the interactive queue every 2 minutes until its
   batch ends; the 10-minute `resume_stuck_feed_batches` sweep restarts it
   on the default queue), profile seeds (the hourly seeder), digests (the
   nightly sweep) and purges (the every-minute check). An import, a
   bookmark sync or a spend-cap check stranded there does not, which is
   why step 3 waits until the workers hold nothing else.

### Docker Compose (local dev, staging)

The same setting: `CELERY_SPLIT_QUEUES=true` in `.env` (dev) or in
`.env.staging` (staging; the next staging deploy applies it), then
`docker compose up -d`, which recreates the backend, the workers and beat
with it. The `celery` service is the interactive worker and
`celery-background` the background one. Both start through
`backend/celery_queues.py`, which picks their queues from the setting.
Off, `celery` runs the same worker command as before the split and
`celery-background` starts no worker (it waits, so that Compose does not
restart it). There are no ordered steps on dev and staging; switching off
there can leave a task queued at that moment in a queue nobody reads.
