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
  // Above the edit overlay (NodeFormModal sits at 1000), like the TTS dialog.
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

const subStyle = {
  fontSize: "0.82rem",
  color: "var(--text-muted)",
  fontWeight: 300,
  marginTop: "2px",
};

/**
 * Asked when a user changes the privacy and/or AI usage of a node that
 * has replies: does the change apply to this node only, or also to the
 * user's replies below it? Mirrors DeleteConfirmDialog's "this only" /
 * "this + my replies" choice. The backend applies the cascade to the
 * user's own nodes and LLM nodes they are the human owner of; other
 * users' replies are left alone.
 *
 * onChoice(applyToReplies: boolean).
 */
function ApplyToRepliesDialog({
  open,
  privacyChanged = false,
  aiUsageChanged = false,
  onClose,
  onChoice,
}) {
  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const what = privacyChanged && aiUsageChanged
    ? "privacy and AI usage"
    : privacyChanged ? "privacy" : "AI usage";

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>Apply to replies too?</h2>
        <div style={bodyStyle}>
          You changed the {what} of a node that has replies.
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={() => onChoice(false)}
            style={{ ...buttonBaseStyle, color: "var(--text-primary)" }}
          >
            <div style={{ fontWeight: 500 }}>This node only</div>
            <div style={subStyle}>Replies keep their current settings.</div>
          </button>
          <button
            onClick={() => onChoice(true)}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>This node and all my replies</div>
            <div style={subStyle}>
              Every reply of yours below it, including AI responses you
              requested. Other users' replies are kept (they own them).
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

export default ApplyToRepliesDialog;
