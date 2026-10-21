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

// How the user's version came about (the 409's changed_by).
const WHAT = {
  user: "edited their intentions",
  revert: "restored an earlier version of their intentions",
  conversation: "had their intentions updated in a conversation",
};

/**
 * Asked by the admin Users tab's "Infer intentions" / "Run now" when the
 * user's current intentions are a version they wrote, restored or had
 * updated in a conversation (#412): the user's edit wins, so the backend
 * refused the run. The dialog says only that the version exists and when,
 * never its text. onConfirm() sends the run again naming that version;
 * Cancel does nothing.
 */
function IntentionsReplaceDialog({ ask, onConfirm, onClose }) {
  useEffect(() => {
    if (!ask) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [ask, onClose]);

  if (!ask) return null;

  const what = WHAT[ask.changedBy] || WHAT.user;
  const when = ask.changedAt ? ` on ${new Date(ask.changedAt).toLocaleString()}` : "";
  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>Replace their intentions?</h2>
        <div style={bodyStyle}>
          @{ask.username} {what}{when}. Inferring now would replace their
          version with a new one built from their tweets. Their version stays
          in the history, but Loore would no longer use it.
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>Keep their version</div>
            <div style={subStyle}>Nothing is run.</div>
          </button>
          <button
            onClick={onConfirm}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
          >
            <div style={{ fontWeight: 500 }}>Replace their version</div>
            <div style={subStyle}>
              A billed {ask.mode === "sync" ? "call at full price" : "batch call"}. If they change
              their intentions again before it finishes, their new version is kept.
            </div>
          </button>
        </div>
      </div>
    </div>
  );
}

export default IntentionsReplaceDialog;
