// "Delete all my writing" (#268): shared helpers for the Account page,
// its dialog and the status notice.
import api from "../api";

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

// The writing is hidden now and "Restore my writing" brings it back: the
// user's own request, until the purge starts. Servers before the
// rework sent no `restorable`; a scheduled request of the user's counts.
export function writingRestorable(dataDeletion) {
  if (!dataDeletion || dataDeletion.status !== "scheduled") return false;
  if (typeof dataDeletion.restorable === "boolean") return dataDeletion.restorable;
  return dataDeletion.source !== "admin";
}

// Restore my writing: the server shows again exactly what the request
// hid. Then the whole user is read again (has_own_entries, description),
// so every page shows the writing at once.
export async function restoreWriting(setUser) {
  const res = await api.post("/account/data/restore");
  setUser((prev) => (prev ? { ...prev, data_deletion: res.data } : prev));
  await reloadUser(setUser);
  return res.data;
}

// After hiding or restoring the writing: the user fields that describe
// it (has_own_entries, description) change too.
export async function reloadUser(setUser) {
  try {
    const res = await api.get("/dashboard", { params: { profile: 0 } });
    if (res?.data?.user) setUser(res.data.user);
  } catch (e) {
    // The next page load reads it; the deletion state is already set.
  }
}

// Load the current page again (after a restore from the status notice,
// every page's data was fetched while the writing was hidden).
export function reloadPage() {
  window.location.reload();
}
