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
 * Asked by the admin whitelist form when the Community Archive does not
 * have the handle (or the archive lookup failed): whitelisting needs the
 * handle's X id, and the only other source is one paid X API user read.
 * The spend is a decision taken here, at the moment it is needed, never a
 * default. onConfirm() resends the whitelist with the X lookup allowed.
 */
function XLookupConfirmDialog({ open, handle, message, costUsd, onConfirm, onClose }) {
  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const cost = costUsd != null ? `about $${Number(costUsd).toFixed(2)}` : "a small charge";
  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>Look up @{handle} on X?</h2>
        <div style={bodyStyle}>
          {message} Whitelisting needs the handle's X id, and the only other
          source is the X API.
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={onConfirm}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>Look up on X and whitelist</div>
            <div style={subStyle}>One paid user read, {cost}, billed to the new account.</div>
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

export default XLookupConfirmDialog;
