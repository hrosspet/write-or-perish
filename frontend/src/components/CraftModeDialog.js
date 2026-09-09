import React, { useEffect } from "react";
import CraftIcon from "./CraftIcon";

const overlayStyle = {
  position: "fixed",
  top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  // Above the nav (1000) and its dropdown (1001), like the other dialogs.
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
};

const titleStyle = {
  fontFamily: "var(--serif)",
  fontSize: "1.4rem",
  fontWeight: 400,
  color: "var(--text-primary)",
  margin: 0,
  marginBottom: "1rem",
  display: "flex",
  alignItems: "center",
  gap: "10px",
};

const bodyStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.92rem",
  fontWeight: 300,
  color: "var(--text-secondary)",
  lineHeight: 1.6,
  marginBottom: "1.5rem",
};

const listStyle = {
  margin: "0.6rem 0 0.9rem",
  paddingLeft: "1.1rem",
};

const buttonRowStyle = {
  display: "flex",
  flexDirection: "column",
  gap: "8px",
};

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
  transition: "border-color 0.15s ease, background 0.15s ease",
};

const subStyle = {
  fontSize: "0.82rem",
  color: "var(--text-muted)",
  fontWeight: 300,
  marginTop: "2px",
};

/**
 * Asked before craft mode turns on. Switching it off stays a plain flip.
 * The one moment of friction covers the case an alpha user hit
 * (2026-09-05): a tap on a menu row that looked like every other row
 * turned on a mode he never learned the name of, and the controls it
 * added had nothing tying them back to the switch.
 */
function CraftModeDialog({ open, onClose, onConfirm }) {
  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>
          <CraftIcon size={18} style={{ color: "var(--accent)" }} />
          Turn on craft mode?
        </h2>
        <div style={bodyStyle}>
          It shows extra controls for steering the details:
          <ul style={listStyle}>
            <li>privacy and AI usage on each entry</li>
            <li>a switch for auto-generating responses, model picker and voice upload on threads</li>
            <li>prompt editing and data export in the ⋮ menu</li>
          </ul>
          Everything it adds carries the sliders icon. Turn it off any time
          in the ⋮ menu or under Account.
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={onConfirm}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>Turn on</div>
          </button>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
          >
            <div style={{ fontWeight: 500 }}>Not now</div>
            <div style={subStyle}>Keep Loore's current simple UI.</div>
          </button>
        </div>
      </div>
    </div>
  );
}

export default CraftModeDialog;
