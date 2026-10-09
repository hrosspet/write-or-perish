import React, { useState } from "react";
import { useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import {
  deletionPending, formatDeletionDate, reloadPage, restoreWriting,
  writingRestorable,
} from "../utils/dataDeletion";

/**
 * The status of "Delete all my writing" (#268) on every page while it
 * waits or runs. The writing is hidden at once, so every page looks empty
 * until a restore or the purge; this says why, and offers the way back.
 *
 * A status bar, not a toast (Peter, 2026-10-09: the earlier floating card
 * with a × "looked like a toast on the upper side of the window"): it
 * spans the page directly under the navigation bar, in the page flow,
 * and stays until the writing is restored or deleted. It reuses the
 * persistent notice of the spend cap (SpendCapBanner: an uppercase accent
 * label before the text) and the outlined accent button of the recording
 * recovery (RecoveryBanner). Hidden on the Account page, whose section
 * says the same with the same button.
 */
export default function DataDeletionBanner() {
  const { user, setUser } = useUser();
  const location = useLocation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const deletion = user?.data_deletion;
  if (!deletionPending(deletion)) return null;
  if (location.pathname.startsWith("/account")) return null;

  const running = deletion.status === "running";
  const restorable = writingRestorable(deletion);

  const restore = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await restoreWriting(setUser);
      // Every page fetched its data while the writing was hidden: load the
      // page again so it shows what came back.
      reloadPage();
    } catch (e) {
      setError(e?.response?.data?.error || "Could not restore your writing. Please try again.");
      setBusy(false);
    }
  };

  let text;
  if (running) {
    text = "Your writing is being deleted forever now.";
  } else if (restorable) {
    text = `You can restore your writing safely until ${formatDeletionDate(deletion.purge_at)}. After that it is deleted forever.`;
  } else {
    text = `Your writing will be deleted forever on ${formatDeletionDate(deletion.purge_at)}.`;
  }

  return (
    <div
      role="status"
      aria-label="Writing deleted"
      style={{
        width: "100%",
        background: "var(--accent-subtle)",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <div
        style={{
          maxWidth: 680,
          margin: "0 auto",
          padding: "12px 16px",
          boxSizing: "border-box",
          display: "flex",
          alignItems: "center",
          flexWrap: "wrap",
          columnGap: 16,
          rowGap: 8,
        }}
      >
        <span
          style={{
            fontFamily: "var(--sans)",
            fontSize: "0.7rem",
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            color: "var(--accent)",
            whiteSpace: "nowrap",
          }}
        >
          {running ? "Deleting your writing" : "Writing deleted"}
        </span>
        <span
          style={{
            flex: "1 1 260px",
            fontFamily: "var(--sans)",
            fontSize: "0.88rem",
            fontWeight: 300,
            lineHeight: 1.5,
            color: "var(--text-secondary)",
          }}
        >
          {text}
        </span>
        {restorable && (
          <button
            type="button"
            onClick={restore}
            disabled={busy}
            style={{
              padding: "8px 18px",
              background: "transparent",
              border: "1px solid var(--accent)",
              borderRadius: "6px",
              color: "var(--accent)",
              fontFamily: "var(--sans)",
              fontSize: "0.85rem",
              cursor: busy ? "default" : "pointer",
              opacity: busy ? 0.6 : 1,
              whiteSpace: "nowrap",
            }}
          >
            {busy ? "Restoring…" : "Restore my writing"}
          </button>
        )}
        {error && (
          <span
            role="alert"
            style={{
              flexBasis: "100%",
              fontFamily: "var(--sans)",
              fontSize: "0.82rem",
              fontWeight: 300,
              color: "var(--error)",
            }}
          >
            {error}
          </span>
        )}
      </div>
    </div>
  );
}
