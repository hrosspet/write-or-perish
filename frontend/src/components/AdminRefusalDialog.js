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

const buttonStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 400,
  padding: "10px 16px",
  borderRadius: "6px",
  cursor: "pointer",
  background: "var(--bg-deep)",
  border: "1px solid var(--border)",
  color: "var(--text-secondary)",
};

// Refusal codes the admin routes return when an account must not be sent
// to a model (#346); anything else stays an inline error.
export const REFUSAL_TITLES = {
  prefill_declined: "Pre-fill declined",
  ai_opt_out: "AI usage is off for this account",
};

/**
 * Shown when the backend refuses an admin pre-fill, intentions run or
 * profile build for an account: the user declined the tweet seed, or set
 * their account's AI usage to none. The action did not run.
 */
function AdminRefusalDialog({ refusal, onClose }) {
  useEffect(() => {
    if (!refusal) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [refusal, onClose]);

  if (!refusal) return null;

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>{REFUSAL_TITLES[refusal.code] || "Not allowed"}</h2>
        <div style={bodyStyle}>
          {refusal.username ? `@${refusal.username}: ` : ""}{refusal.message} Nothing was run.
        </div>
        <button onClick={onClose} style={buttonStyle}>OK</button>
      </div>
    </div>
  );
}

export default AdminRefusalDialog;
