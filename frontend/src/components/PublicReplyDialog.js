import React, { useEffect, useState } from "react";

const overlayStyle = {
  position: "fixed",
  top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
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
  marginBottom: "1.25rem",
};

/**
 * Consent dialog shown before sending a reply under a PUBLIC node (#228):
 * replies in public threads are public. The don't-show-again choice is
 * remembered in localStorage ('loore_public_reply_ack') — the server
 * enforces the rule regardless, this dialog just makes it felt.
 */
export const PUBLIC_REPLY_ACK_KEY = "loore_public_reply_ack";

function PublicReplyDialog({ open, onClose, onConfirm }) {
  const [dontShowAgain, setDontShowAgain] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const confirm = () => {
    if (dontShowAgain) {
      localStorage.setItem(PUBLIC_REPLY_ACK_KEY, "true");
    }
    onConfirm();
  };

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={cardStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={titleStyle}>This reply will be public</h2>
        <p style={bodyStyle}>
          You're responding in a public thread, so your reply will be
          visible to everyone — including people who aren't signed in.
          To think about this privately instead, quote it into one of
          your own threads.
        </p>
        <label style={{
          display: "flex", alignItems: "center", gap: "8px",
          fontFamily: "var(--sans)", fontSize: "0.82rem", fontWeight: 300,
          color: "var(--text-muted)", marginBottom: "1.25rem",
          cursor: "pointer",
        }}>
          <input
            type="checkbox"
            checked={dontShowAgain}
            onChange={(e) => setDontShowAgain(e.target.checked)}
          />
          Don't show this again
        </label>
        <div style={{ display: "flex", gap: "10px", justifyContent: "flex-end" }}>
          <button
            onClick={onClose}
            style={{
              fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 300,
              padding: "10px 18px", borderRadius: "6px", cursor: "pointer",
              background: "none", border: "1px solid var(--border)",
              color: "var(--text-secondary)",
            }}
          >
            Cancel
          </button>
          <button
            onClick={confirm}
            style={{
              fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 400,
              padding: "10px 18px", borderRadius: "6px", cursor: "pointer",
              background: "var(--accent)", border: "none",
              color: "var(--bg-deep)",
            }}
          >
            Publish reply
          </button>
        </div>
      </div>
    </div>
  );
}

export default PublicReplyDialog;
