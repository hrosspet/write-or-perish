// "Delete all my writing" (#268): shared helpers for the Account page,
// its dialog and the reminder banner.

// The date a scheduled deletion happens, e.g. "5 November 2026".
export function formatDeletionDate(value) {
  if (!value) return "";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
}

// The purge revokes Loore's access at X and deletes the stored X
// connection. If X still lists Loore (the call failed, or the user also
// signs in with X), these are the steps to remove it there.
export const X_REMOVE_ACCESS_STEPS =
  "on X, open Settings and privacy, then Security and account access, " +
  "Apps and sessions, Connected apps, choose Loore and revoke its permissions.";

// A deletion that is waiting out its grace period or running now.
export function deletionPending(dataDeletion) {
  const status = dataDeletion && dataDeletion.status;
  return status === "scheduled" || status === "running";
}
