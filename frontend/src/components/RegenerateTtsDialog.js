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
 * Asked when a user edits an entry/profile that has GENERATED TTS audio
 * (not an original voice recording). The edited text no longer matches the
 * audio, so the user chooses whether to keep the existing audio or
 * regenerate it. (#66)
 *
 * onChoice(regenerate: boolean):
 *   - false → keep existing audio; save without touching it.
 *   - true  → clear the audio so fresh TTS is generated on next playback.
 */
function RegenerateTtsDialog({ open, onClose, onChoice }) {
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
        <h2 style={titleStyle}>Regenerate audio?</h2>
        <div style={bodyStyle}>
          This entry has generated audio that won't match your edits. Keep the
          existing audio, or regenerate it (it'll be created fresh the next
          time you play it).
        </div>
        <div style={buttonRowStyle}>
          <button
            onClick={() => onChoice(true)}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>Regenerate audio</div>
            <div style={{
              fontSize: "0.82rem", color: "var(--text-muted)",
              fontWeight: 300, marginTop: "2px",
            }}>
              Removes the outdated audio; new audio is generated on next play.
            </div>
          </button>
          <button
            onClick={() => onChoice(false)}
            style={{ ...buttonBaseStyle, color: "var(--text-primary)" }}
          >
            <div style={{ fontWeight: 500 }}>Keep existing audio</div>
            <div style={{
              fontSize: "0.82rem", color: "var(--text-muted)",
              fontWeight: 300, marginTop: "2px",
            }}>
              Saves your edits; the current audio stays as-is.
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

export default RegenerateTtsDialog;
