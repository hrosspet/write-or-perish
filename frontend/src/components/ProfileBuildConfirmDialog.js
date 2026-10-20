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
  marginBottom: "1.5rem",
};

const buttonRowStyle = { display: "flex", flexDirection: "column", gap: "8px" };

const buttonBaseStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 400,
  padding: "10px 16px",
  borderRadius: "6px",
  cursor: "pointer",
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
 * Asked by the admin Users tab's "Build profile" button when the user's
 * profile job is held after cut-off outputs (#368): waiting an hour after
 * the first, stopped after the second. onConfirm() sends the build with
 * force, which runs it now despite the hold; Cancel does nothing.
 */
function ProfileBuildConfirmDialog({ open, username, backoff, onConfirm, onClose }) {
  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const stopped = backoff?.state === "stopped";
  const what = stopped
    ? `failed ${backoff?.refusals || 2} times in a row`
    : "failed on its last try";
  const hold = stopped
    ? "It is stopped: nothing retries it on its own."
    : `The next automatic try is after ${backoff?.until ? new Date(backoff.until).toLocaleString() : "an hour"}.`;
  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>Build the profile anyway?</h2>
        <div style={bodyStyle}>
          Profile generation for {username} {what} (cut-off or empty
          output). {hold} Start another build anyway?
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={onConfirm}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>Build now</div>
            <div style={subStyle}>A billed batch call; if it is cut off again, the profile job stops.</div>
          </button>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

export default ProfileBuildConfirmDialog;
