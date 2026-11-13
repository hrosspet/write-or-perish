# Pre-fill bundles: from the lab to production (#698)

The lab holds accounts pre-filled from the Community Archive: each member's public tweets and the profile (and intentions, where built) a model drafted from them. A **bundle** is that material for one member, written by the lab into private object storage and loaded into an account on production without running any model.

- **Lab:** `backend/scripts/prefill_bundles.py export` writes one bundle per account, keyed by the account's X id, plus a manifest (no content).
- **Production, admin:** Admin → Users → "Make ready from prepared material" (handle or X id) fills the account with that X id, or opens one under the bundle's handle, so the person's X sign-in finds it. It lets nobody in: Activate the account as for any other.
- **Production, automatic:** a signed-in person whose X id an X sign-in proved answers Yes to "Start from your tweets". The request only records the Yes; a background task looks for a bundle for that X id. When there is one it loads, the person sees "Preparing your starting point…" (web and iPhone), then their profile, and the account is let in. With no bundle nothing changes.

A bundle is only ever loaded into the account whose X id is the bundle's. A "no" to the tweet seed and AI usage None are always refused. The admin's load also refuses an account someone signed in to or one with writing of its own, unless the admin ticks "Load anyway". A "no" given while a load runs stops it; what was loaded stays (linked), and a later Yes continues where it stopped.

Nothing in a bundle is trusted beyond what a pre-fill makes. Entries must be public posts Loore fetched, by the bundle's account, with no model fields (an allow-list; other fields are dropped). They are stored private, with AI usage at most "chat", and encrypted with production's key. The same holds for the profile, recent contexts and intentions. Todos, other artifact kinds and versions a person saved are refused.

Code: `backend/utils/bundle_storage.py` (storage), `backend/utils/prefill_bundles.py` (layout, manifest, exclusions), `backend/utils/prefill_bundle_load.py` (the load), `backend/tasks/prefill_bundles.py` (its runs), `backend/routes/prefill_bundles.py` (admin).

## Layout

Under the storage location (`gs://<BUCKET>[/<PREFIX>]`, or a directory for local development):

```
bundles/<x_id>/bundle.jsonl.gz   dump_user.py's file (format 2), as gzip parts
bundles/<x_id>/manifest.json     formats, X id, handle, counts, dates, sha256, size, part index
handles/<handle>.json            {"x_id"}: lets the admin type a handle
excluded/<x_id>.json             {"x_id", "reason", "excluded_at"}
```

The bundle is a series of gzip members: the header line alone, then 500 entries per member. `gzip -d` reads it as one file. A load reads one member at a time by its byte range and never downloads the whole object. The manifest lists each member's offset, length, line count and sha256. Before writing anything, a load checks every part and the whole object against the manifest. It checks each part again when it reads it, and refuses a format it does not know. The manifest is written after the bundle; a bundle without a manifest is never read.

## How the bucket's contents are protected

The bundles hold members' public tweets and the profiles drafted from them. They are **not** encrypted with production's key: each environment has its own key, and production encrypts every entry with its own key when it writes it. So access to the bucket is the protection:

- A **private** bucket with **uniform bucket-level access** (no per-object ACLs) and **public access prevention** enforced. No grant to `allUsers` or `allAuthenticatedUsers`, ever.
- **Soft delete off** on this bucket, so a removal (a deletion request) is final at once, not recoverable for days.
- **Object versioning off**, for the same reason.
- **The lab** writes: its service account gets `roles/storage.objectAdmin` **on this bucket only** (create, overwrite, delete). The lab never holds production credentials.
- **Production** reads: its service account gets `roles/storage.objectViewer` **on this bucket only** (read and list). Check that this account has no project-wide storage role (the default compute account has a broad project role; if production runs as it, the bucket grant does not limit it, and a dedicated account or narrower scopes do).
- The VMs' access scopes must allow it: storage read-write on the lab, at least storage read-only on production. Scopes change only while a VM is stopped.
- Google encrypts the objects at rest by default; nothing else reads them.

## One-time setup

Placeholders: `<PROJECT>`, `<BUCKET>`, `<REGION>` (the VMs' region), `<LAB_SA>` and `<PROD_SA>` (the service account emails the lab VM and the production VM run as).

```bash
gcloud storage buckets create gs://<BUCKET> --project=<PROJECT> --location=<REGION> \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://<BUCKET> --clear-soft-delete
gcloud storage buckets add-iam-policy-binding gs://<BUCKET> \
  --member=serviceAccount:<LAB_SA> --role=roles/storage.objectAdmin
gcloud storage buckets add-iam-policy-binding gs://<BUCKET> \
  --member=serviceAccount:<PROD_SA> --role=roles/storage.objectViewer
```

**Remove the default project bindings.** A new bucket comes with bindings for the project's editors and viewers: `projectEditor` holds the legacy bucket-owner and object-owner roles, and `projectViewer` holds the legacy bucket-reader and object-reader roles. These give everyone with a broad project role access to the bundles. Remove the editor and viewer members from those four legacy roles. Project owners may keep theirs, since they control the project anyway. The members are named by the project's number, so editing the policy file is the reliable way:

```bash
gcloud storage buckets get-iam-policy gs://<BUCKET> --format=json > bucket-policy.json
# Edit bucket-policy.json: delete every "projectEditor:…" and "projectViewer:…"
# member; drop a binding that is left with no members.
gcloud storage buckets set-iam-policy gs://<BUCKET> bucket-policy.json
# Check: no public access, uniform access on, soft delete off, and only the
# lab's grant, production's grant and the project owners' legacy roles.
gcloud storage buckets describe gs://<BUCKET> \
  --format="yaml(public_access_prevention,uniform_bucket_level_access,soft_delete_policy)"
gcloud storage buckets get-iam-policy gs://<BUCKET>
```

**Environment variables** (no values in the repo; each server's env file):

| Variable | Lab | Production |
|---|---|---|
| `PREFILL_BUNDLE_LOCATION` | `gs://<BUCKET>` | `gs://<BUCKET>` |
| `PREFILL_BUNDLES_ENABLED` | not needed | `true` once the export is checked |

Both unset or off: nothing reads the storage and nothing loads. Switching `PREFILL_BUNDLES_ENABLED` off pauses loads under way; they continue when it is on again.

**Code on the lab:** check out the branch with this code and install the requirements (`google-cloud-storage` is new). Do not run `deploy.sh` there (it generates and pushes migrations). The export reads no new tables, but the models of the newer code read every column they map: when the lab's schema is older than the code, add the missing columns first (additively, as the lab's schema sync does; never `flask db migrate`). An `export --dry-run` stops at once with "column … does not exist" when one is missing.

**Production:** the deploy creates the two new tables (`prefill_bundle_load`, `prefill_bundle_node_map`) and installs the requirements. Then set the two variables in the env file and restart the web service and both Celery workers.

## The lab export

Run with the app's virtualenv and the lab's env file loaded, as its systemd units run the app, from the checkout's root. The export refuses to run where content is encrypted (any server but the lab).

**Before the export, every prepared account must carry its X id.** Bundles are keyed by the account's numeric X id (`User.twitter_id`), the identity an X sign-in proves. An account without one is skipped as `skipped_no_x_id`. Write the ids first, from the archive's own account records, never from a handle alone.

**Name the archive snapshot.** `--archive-snapshot` names the Community Archive export the accounts were imported from, as the import's log names it. It goes into every manifest. A real export refuses to run without it. It is never taken from the server's settings, which point at whatever snapshot the server used last.

```bash
python backend/scripts/prefill_bundles.py export --dry-run                                  # counts only, writes nothing
python backend/scripts/prefill_bundles.py export --archive-snapshot <EXPORT_ID> --limit 20  # a first batch; time it
python backend/scripts/prefill_bundles.py export --archive-snapshot <EXPORT_ID>             # the rest, in tmux
python backend/scripts/prefill_bundles.py status                                            # bundles and exclusions
python backend/scripts/prefill_bundles.py status --x-id <X_ID>                              # one manifest
```

**Expected counts:** about one bundle per account with a profile (roughly 2,000). Under the summary, one line per skip reason says what it means and what would change it. The reasons:
- `skipped_ai_none`: accounts set to no AI.
- `skipped_no_profile`.
- `skipped_unfinished_profile`: a window set without its final merge; production would finish it with a model call.
- `skipped_no_x_id`: no X id stored; see above.
- `skipped_signed_in`: the account shows a sign that a person used it, such as a sign-in or terms on record, an X sign-in, a setting or answer, or a draft or token. An address on the account is not such a sign; the lab's placeholder addresses in the reserved `.invalid` domain are no address at all.
- `skipped_admin`, `skipped_excluded`, `skipped_declined`.
- `skipped_other_material`: an entry that is not a fetched tweet, a todo list, an artifact other than intentions, or a version a person saved. A bundle holds a pre-fill's material only, and production refuses anything else.

Look at any reason you did not expect before the real run. The manifests' `counts.embeddings` say whether the lab's entries carry embeddings (the lab ran without the embedding sweep, so expect 0).

**How long:** an estimate of one to two hours for ~2,000 accounts and ~14 million entries (dump, gzip, upload); not measured on the lab. The `--limit 20` run gives the rate. Memory stays flat: one account at a time, its entries, embeddings and prompt pins read in batches, written to a temporary file and uploaded in chunks.

**A deletion during an export holds.** The export checks the exclusion list just before it writes an account and again after. If the X id was excluded meanwhile, it removes what it wrote. A `delete` run while an export is going is therefore never undone by that export.

**Resume:** run the same command again. An account whose manifest fingerprint matches the lab's metadata is skipped as unchanged; one changed on the lab since is exported again. `--force` exports everything again. `--only <handle|x_id>` does one account.

## The production check (metadata only)

From the app's directory on production, in `flask shell`, with the env loaded. It reads one small manifest; no content.

```python
from backend.utils import prefill_bundles as pb
s = pb.configured_storage()
print(pb.bundles_enabled(), type(s).__name__)
m = pb.read_manifest(s, "<X_ID>")
print({k: m.get(k) for k in ("x_id", "handle", "exported_at", "newest_entry_at", "counts")} if m else "no bundle")
```

Then "Check" in Admin → Users → "Make ready from prepared material" shows the same metadata and the account it would go into.

## Removal and the exclusion list

Deleting an account on production does not delete its bundle (the bundle is the lab's material). These commands remove a bundle and put its X id on the exclusion list, so no export writes it again and no load reads it. Run them on the lab, or anywhere with the lab's write access:

```bash
python backend/scripts/prefill_bundles.py delete --x-id <X_ID> --reason deletion_request
python backend/scripts/prefill_bundles.py delete --x-id <X_ID> --reason declined    # a "no" on production
python backend/scripts/prefill_bundles.py delete --x-id <X_ID> --reason protected   # the X account went protected
python backend/scripts/prefill_bundles.py unexclude --x-id <X_ID>                   # they opted in again; then export --only <X_ID>
```

Reasons: `deletion_request`, `declined`, `protected`, `owner_request`, `other`. With the lab stopped, the same with `gcloud`:

```bash
echo '{"x_id": "<X_ID>", "reason": "deletion_request"}' | gcloud storage cp - gs://<BUCKET>/excluded/<X_ID>.json
gcloud storage rm gs://<BUCKET>/bundles/<X_ID>/bundle.jsonl.gz gs://<BUCKET>/bundles/<X_ID>/manifest.json
gcloud storage rm gs://<BUCKET>/handles/<handle in lower case>.json
```

A deletion request also covers the lab's own copy (the lab account); remove it there as well.

**An unclaimed loaded account** falls under the deletion of pre-filled accounts nobody signed in to (#685): every load sets `User.prefilled_handle`, and nothing a person's own action sets (sign-in, terms, X handle, the tweet-seed answer, settings, defaults, time zone). The entries' `updated_at` is when production stored them, so the 90 days count from the load, not from the lab's import. That job removes the account, not the bundle.

## After a load: how current the account is

A bundle is a snapshot: tweets up to its `newest_entry_at` (the lab's Community Archive export). After a load the account is consistent: its entries, the profile drafted from them (with its cutoff), intentions where built. The profile pipeline treats that profile as current and builds nothing on its own. Newer tweets arrive only by:

- the admin's "Pre-fill from CA" on the account: it adds tweets not yet there (entries are matched on the tweet id), and the next profile update folds them in. It reads the Community Archive, whose old interface retires on 2026-11-19 (#690 moves to the keyed one);
- the person's own archive upload (Import);
- a newer lab export of the account, loaded again with "Make ready" (entries already there are skipped; the newer profile versions are added on top).

Nothing tops a loaded account up automatically.

Entries that arrive without an embedding (the lab's carry none) are embedded later by the existing embedding sweep, as after any import, also on an account nobody claimed. The sweep does 100 entries every 5 minutes, so 10,000 entries take about 8 hours. That is paid, but cheap at the embedding model's price.

## Local development

`google-cloud-storage` is a new requirement: rebuild the backend and Celery images (`make dev-build`, or `docker compose build backend celery`). Locally, bundles can live in a directory: `PREFILL_BUNDLE_LOCATION=/app/data/prefill-bundles` and `PREFILL_BUNDLES_ENABLED=true` in the local `.env` (the compose file passes both to the backend and the workers). A bundle loads into the account with its X id, which on one database is the account it was exported from; to try a load, export on one database and load on another. `backend/tests/test_prefill_bundles.py` does exactly that with sqlite files.
