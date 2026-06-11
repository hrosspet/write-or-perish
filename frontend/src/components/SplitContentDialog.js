import React, { useEffect } from "react";

export const NODE_CHAR_CAP = 100000;

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
};

const bodyStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.92rem",
  fontWeight: 300,
  color: "var(--text-secondary)",
  lineHeight: 1.6,
  marginBottom: "1.5rem",
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

/**
 * Dialog shown when content exceeds the per-entry character cap
 * (NODE_CHAR_CAP). Long entries are split server-side into a series of
 * connected entries — one flowing text for the AI, separate entries in
 * the thread. onConfirm proceeds with the split; onClose cancels.
 */
function SplitContentDialog({ open, charCount, onClose, onConfirm }) {
  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const parts = Math.max(2, Math.ceil(charCount / NODE_CHAR_CAP));

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>Long entry</h2>
        <div style={bodyStyle}>
          This text is {charCount.toLocaleString()} characters — above the{" "}
          {NODE_CHAR_CAP.toLocaleString()}-character limit for a single
          entry. It can be saved as ~{parts} connected entries: they read
          as one continuous text and stay together in the thread.
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={onConfirm}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>
              Split into ~{parts} connected entries
            </div>
            <div style={{
              fontSize: "0.82rem", color: "var(--text-muted)",
              fontWeight: 300, marginTop: "2px",
            }}>
              Split happens on line breaks — no line is cut in half.
            </div>
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

export default SplitContentDialog;
