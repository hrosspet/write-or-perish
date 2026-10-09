import React, { useEffect, useState } from "react";
import { formatDeletionDate, X_REMOVE_ACCESS_STEPS } from "../utils/dataDeletion";

const overlayStyle = {
  position: "fixed",
  top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1100,
};

const cardStyle = {
  background: "var(--bg-card)",
  border: "1px solid var(--border)",
  borderRadius: "12px",
  padding: "2rem",
  width: "480px",
  maxWidth: "90vw",
  maxHeight: "90vh",
  overflowY: "auto",
  boxSizing: "border-box",
};

const titleStyle = {
  fontFamily: "var(--serif)",
  fontSize: "1.4rem",
  fontWeight: 400,
  color: "var(--text-primary)",
  margin: 0,
  marginBottom: "1rem",
};

const bodyStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.92rem",
  fontWeight: 300,
  color: "var(--text-secondary)",
  lineHeight: 1.6,
  margin: "0 0 0.9rem",
};

const inputStyle = {
  width: "100%",
  padding: "10px 12px",
  borderRadius: "6px",
  border: "1px solid var(--border)",
  backgroundColor: "var(--bg-input)",
  color: "var(--text-primary)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.95rem",
  boxSizing: "border-box",
  marginBottom: "1.25rem",
};

const buttonRowStyle = { display: "flex", flexDirection: "column", gap: "8px" };

const buttonBaseStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 400,
  padding: "10px 16px",
  borderRadius: "6px",
  textAlign: "left",
  background: "var(--bg-deep)",
  border: "1px solid var(--border)",
};

const subStyle = {
  fontSize: "0.82rem",
  color: "var(--text-muted)",
  fontWeight: 300,
  marginTop: "2px",
};

/**
 * Asked before "Delete all my writing" (#268). Says in plain words what
 * goes and what stays, and when: the writing disappears at once, can be
 * restored safely until the end of the grace period, and is then deleted
 * forever (Peter, 2026-10-09, in the framing of his terms text). The user
 * types their username to confirm; the delete option stays disabled until
 * it matches. onConfirm(typed) returns a promise; a rejection's message
 * is shown.
 */
function DeleteWritingDialog({ open, username, graceDays, xConnected, onConfirm, onClose }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!open) return undefined;
    setTyped("");
    setError(null);
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const days = graceDays || 30;
  const date = formatDeletionDate(new Date(Date.now() + days * 24 * 3600 * 1000));
  const matches = typed.trim().toLowerCase() === (username || "").toLowerCase();

  const confirm = async () => {
    if (!matches || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(typed.trim());
    } catch (e) {
      setError(e?.response?.data?.error || "Could not delete your writing. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-writing-title"
      >
        <h2 id="delete-writing-title" style={titleStyle}>Delete all your writing?</h2>
        <p style={bodyStyle}>
          Loore will delete everything you have written or recorded here:
          your entries and recordings, the AI's replies to you, your profile,
          intentions, memory and other documents, your todo list, drafts,
          shares, saved references and imports.
        </p>
        <p style={bodyStyle}>
          Your account stays. You can still sign in, and your username and
          settings are kept. If other people replied to your entries, their
          replies stay and your entry shows as deleted. Loore keeps a record
          of what your AI use cost, without your name.
        </p>
        <p style={bodyStyle}>
          It disappears at once, for you and for everyone else. If you
          change your mind, you can restore your writing safely within
          {" "}{days} days, until {date}, on the Account page. After that it
          is deleted forever. What you write from now on stays.
        </p>
        {xConnected && (
          <p style={bodyStyle}>
            Loore also forgets your X connection for bookmarks and removes
            its access on X. If X still lists Loore afterwards, remove it
            yourself: {X_REMOVE_ACCESS_STEPS}
          </p>
        )}
        <label htmlFor="delete-writing-confirm" style={{ ...bodyStyle, display: "block", marginBottom: "6px" }}>
          To confirm, type your username, <strong>{username}</strong>.
        </label>
        <input
          id="delete-writing-confirm"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") confirm(); }}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          style={inputStyle}
        />
        {error && (
          <div style={{ ...bodyStyle, color: "var(--error)" }}>{error}</div>
        )}
        <div style={buttonRowStyle}>
          <button
            onClick={confirm}
            disabled={!matches || busy}
            style={{
              ...buttonBaseStyle,
              color: "var(--error)",
              cursor: matches && !busy ? "pointer" : "default",
              opacity: matches && !busy ? 1 : 0.45,
            }}
          >
            <div style={{ fontWeight: 500 }}>
              {busy ? "Deleting…" : "Delete all my writing"}
            </div>
            <div style={subStyle}>You can restore it until {date}.</div>
          </button>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)", cursor: "pointer" }}
          >
            Keep my writing
          </button>
        </div>
      </div>
    </div>
  );
}

export default DeleteWritingDialog;
