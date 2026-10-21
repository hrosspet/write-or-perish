import React, { useEffect } from "react";

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
  width: "440px",
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

const buttonRowStyle = { display: "flex", flexDirection: "column", gap: "8px", marginTop: "0.6rem" };

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
 * Asked by Disconnect X (#494) when the account also syncs its X
 * bookmarks: that connection is separate, and Loore can't know whether
 * the user wants it to end too, so both choices are shown. Ending it
 * removes all of Loore's access on X; keeping it leaves Loore listed on
 * X. onChoose(endBookmarkSync) disconnects; the page shows the outcome.
 */
function DisconnectXDialog({ open, busy, onChoose, onClose }) {
  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const choice = (enabled) => ({
    ...buttonBaseStyle,
    cursor: enabled ? "pointer" : "default",
    opacity: enabled ? 1 : 0.45,
  });
  return (
    <div onClick={onClose} style={overlayStyle}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="disconnect-x-title"
      >
        <h2 id="disconnect-x-title" style={titleStyle}>Disconnect X?</h2>
        <p style={bodyStyle}>
          Sign in with X will no longer open this account. You sign in
          with email.
        </p>
        <p style={bodyStyle}>
          Your X bookmarks also sync to Loore. Stop that too? Bookmarks
          already in Loore stay either way.
        </p>
        <div style={buttonRowStyle}>
          <button
            onClick={() => onChoose(true)}
            disabled={busy}
            style={{ ...choice(!busy), color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>
              {busy ? "One moment…" : "Disconnect X and stop bookmark sync"}
            </div>
            <div style={subStyle}>Loore removes all its access on X.</div>
          </button>
          <button
            onClick={() => onChoose(false)}
            disabled={busy}
            style={{ ...choice(!busy), color: "var(--text-primary)" }}
          >
            <div style={{ fontWeight: 500 }}>Disconnect X, keep bookmark sync</div>
            <div style={subStyle}>
              New bookmarks keep arriving every night, so X still lists Loore
              as an app with access to your account.
            </div>
          </button>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)", cursor: "pointer" }}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

export default DisconnectXDialog;
