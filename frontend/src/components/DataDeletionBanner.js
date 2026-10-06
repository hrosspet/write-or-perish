import React, { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import { deletionPending, formatDeletionDate } from "../utils/dataDeletion";

const DISMISS_KEY = "loore:data-deletion-banner-dismissed";

function readDismissed() {
  try {
    return window.sessionStorage.getItem(DISMISS_KEY) === "1";
  } catch (e) {
    return false;
  }
}

/**
 * A quiet reminder on every page while "Delete all my writing" (#268) is
 * waiting out its grace period or running: everything written until the
 * date goes too, so the user should not be surprised. Links to the
 * Account page, where the deletion can be cancelled. Hidden there (the
 * page says it in full) and for the rest of the browser session once
 * dismissed.
 */
export default function DataDeletionBanner() {
  const { user } = useUser();
  const location = useLocation();
  const [dismissed, setDismissed] = useState(readDismissed);

  const deletion = user?.data_deletion;
  if (!deletionPending(deletion) || dismissed) return null;
  if (location.pathname.startsWith("/account")) return null;

  const dismiss = () => {
    setDismissed(true);
    try {
      window.sessionStorage.setItem(DISMISS_KEY, "1");
    } catch (e) {
      // Storage blocked: dismissed until the next page load.
    }
  };

  const text = deletion.status === "running"
    ? "Your writing is being deleted now."
    : `All your writing will be deleted on ${formatDeletionDate(deletion.purge_at)}, including anything you write before then.`;

  return (
    <div
      role="status"
      style={{
        maxWidth: 680,
        margin: "12px auto 0",
        padding: "10px 16px",
        boxSizing: "border-box",
        display: "flex",
        alignItems: "center",
        gap: 12,
        border: "1px solid var(--border)",
        borderRadius: 8,
        background: "var(--bg-card)",
        fontFamily: "var(--sans)",
        fontSize: "0.85rem",
        fontWeight: 300,
        lineHeight: 1.5,
        color: "var(--text-secondary)",
      }}
    >
      <span style={{ flex: 1 }}>
        {text}{" "}
        {deletion.status === "scheduled" && (
          <Link to="/account#delete-data" style={{ color: "var(--accent)" }}>
            Cancel on the Account page
          </Link>
        )}
      </span>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss"
        style={{
          background: "transparent",
          border: "none",
          color: "var(--text-muted)",
          fontSize: "1.1rem",
          lineHeight: 1,
          cursor: "pointer",
          padding: "0 4px",
        }}
      >
        ×
      </button>
    </div>
  );
}
