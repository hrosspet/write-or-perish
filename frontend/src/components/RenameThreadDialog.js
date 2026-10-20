import React, { useEffect, useRef, useState } from "react";

// Same shell as DeleteConfirmDialog — the two live side by side in the
// Log card's kebab and should read as one family.
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
  margin: 0,
};

const titleStyle = {
  fontFamily: "var(--serif)",
  fontSize: "1.4rem",
  fontWeight: 400,
  color: "var(--text-primary)",
  margin: 0,
  marginBottom: "1rem",
};

const inputStyle = {
  width: "100%",
  padding: "10px 12px",
  backgroundColor: "var(--bg-deep)",
  color: "var(--text-primary)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  fontFamily: "var(--sans)",
  // 16px keeps iOS Safari from zooming the page on focus.
  fontSize: "16px",
  boxSizing: "border-box",
  transition: "border-color 0.15s ease",
};

const hintStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.82rem",
  fontWeight: 300,
  color: "var(--text-muted)",
  lineHeight: 1.5,
  marginTop: "8px",
  marginBottom: "1.5rem",
};

const buttonRowStyle = {
  display: "flex",
  justifyContent: "flex-end",
  gap: "8px",
};

const buttonBaseStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 400,
  padding: "10px 16px",
  borderRadius: "6px",
  cursor: "pointer",
  background: "var(--bg-deep)",
  border: "1px solid var(--border)",
  transition: "border-color 0.15s ease, background 0.15s ease, opacity 0.15s ease",
};

/**
 * Name a thread for the Log. One text field; Enter saves, Escape closes.
 * Saving an empty field clears the name, so the card falls back to the
 * entry's own title — the placeholder shows which one.
 *
 * onSave receives the trimmed name ("" to clear).
 */
function RenameThreadDialog({
  open,
  currentName = "",
  fallbackTitle = "",
  saving = false,
  onClose,
  onSave,
}) {
  const [value, setValue] = useState(currentName || "");
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    setValue(currentName || "");
    // Focus with the old name selected so retyping replaces it in one go.
    const focusTimer = setTimeout(() => {
      if (inputRef.current) {
        inputRef.current.focus();
        inputRef.current.select();
      }
    }, 0);
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      clearTimeout(focusTimer);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, currentName, onClose]);

  if (!open) return null;

  const trimmed = value.trim();
  const unchanged = trimmed === (currentName || "").trim();

  const handleSubmit = (e) => {
    e.preventDefault();
    if (saving || unchanged) return;
    onSave(trimmed);
  };

  return (
    <div onClick={onClose} style={overlayStyle}>
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
        style={cardStyle}
      >
        <h2 style={titleStyle}>Rename thread</h2>
        <input
          ref={inputRef}
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          maxLength={120}
          placeholder={fallbackTitle || "Thread name"}
          aria-label="Thread name"
          disabled={saving}
          style={inputStyle}
          onFocus={(e) => { e.currentTarget.style.borderColor = "var(--accent-dim)"; }}
          onBlur={(e) => { e.currentTarget.style.borderColor = "var(--border)"; }}
        />
        <div style={hintStyle}>
          Shown on this thread's Log card. Leave it empty to show the entry's own title.
        </div>
        <div style={buttonRowStyle}>
          <button
            type="button"
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving || unchanged}
            style={{
              ...buttonBaseStyle,
              color: "var(--accent)",
              opacity: saving || unchanged ? 0.5 : 1,
              cursor: saving || unchanged ? "default" : "pointer",
            }}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

export default RenameThreadDialog;
