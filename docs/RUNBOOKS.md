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
