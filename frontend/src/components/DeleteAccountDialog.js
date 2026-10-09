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

const DAY_MS = 24 * 3600 * 1000;

/**
 * Asked before "Delete my account" (#269). Says what happens and when:
 * the account is deleted and signed out at once, can be restored by
 * signing in during the grace period, and is then deleted forever with
 * everything in it.
 * Both choices are buttons; the delete one stays disabled until the
 * username is typed. An account with an email address confirms from a
 * mailed link, which the button says. onConfirm(typed) returns a
 * promise; a rejection's message is shown.
 */
function DeleteAccountDialog({
  open, username, email, info, xConnected, writingDeletionAt, onConfirm, onClose,
}) {
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

  const days = info?.grace_days || 30;
  const reserveDays = info?.username_reserve_days || 365;
  const byEmail = !!info?.confirm_by_email;
  const date = formatDeletionDate(new Date(Date.now() + days * DAY_MS));
  const matches = typed.trim().toLowerCase() === (username || "").toLowerCase();

  const confirm = async () => {
    if (!matches || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(typed.trim());
    } catch (e) {
      setError(e?.response?.data?.error || "Could not start the deletion. Please try again.");
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
        aria-labelledby="delete-account-title"
      >
        <h2 id="delete-account-title" style={titleStyle}>Delete your account?</h2>
        <p style={bodyStyle}>
          Your account is deleted at once and you are signed out everywhere.
          Nobody can see your public writing any more.
        </p>
        <p style={bodyStyle}>
          For {days} days, until {date}, you can restore it by signing in.
          After that it is deleted forever, with everything in it: your
          entries and recordings, the AI's replies, your profile, intentions
          and other documents, your todo list, drafts, shares, saved
          references, imports, poll answers, settings and sign-in.
          If other people replied to your entries, their replies stay and
          your entry shows as deleted. Loore keeps a record of what your AI
          use cost, without your name.
        </p>
        <p style={bodyStyle}>
          Once it is deleted forever, nobody else can take your username
          for {reserveDays} days.
        </p>
        {writingDeletionAt && (
          <p style={bodyStyle}>
            This replaces your request to delete all your writing on{" "}
            {formatDeletionDate(writingDeletionAt)}: everything is deleted
            on {date} instead, and restoring your account cancels both.
          </p>
        )}
        {xConnected && (
          <p style={bodyStyle}>
            Loore also forgets your X connection for bookmarks and removes
            its access on X. If X still lists Loore afterwards, remove it
            yourself: {X_REMOVE_ACCESS_STEPS}
          </p>
        )}
        {byEmail && (
          <p style={bodyStyle}>
            To make sure it is you, Loore emails a confirmation link
            to <strong>{email}</strong>. Nothing happens until you open it and
            confirm there.
          </p>
        )}
        <label htmlFor="delete-account-confirm" style={{ ...bodyStyle, display: "block", marginBottom: "6px" }}>
          To confirm, type your username, <strong>{username}</strong>.
        </label>
        <input
          id="delete-account-confirm"
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
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
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
              {busy ? "One moment…" : byEmail ? "Email me the confirmation link" : "Delete my account"}
            </div>
            <div style={subStyle}>
              {byEmail
                ? "Nothing changes until you confirm from the link."
                : `Signs you out now. You can restore it until ${date}.`}
            </div>
          </button>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)", cursor: "pointer" }}
          >
            Keep my account
          </button>
        </div>
      </div>
    </div>
  );
}

export default DeleteAccountDialog;
