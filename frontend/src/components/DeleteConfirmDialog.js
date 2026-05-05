import React, { useEffect } from "react";

const DEFAULT_GRACE_DAYS = 30;

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
 * Confirmation dialog for soft-deleting nodes.
 *
 * Modes:
 *   - "single", hasChildren=false: single Delete button.
 *   - "single", hasChildren=true: choice between "this only" and
 *     "this + my replies".
 *   - "thread": always-with-descendants, single Delete button. Used
 *     by the Log card kebab.
 *
 * onConfirm receives { withDescendants: boolean }.
 */
function DeleteConfirmDialog({
  open,
  mode = "single",
  hasChildren = false,
  graceDays = DEFAULT_GRACE_DAYS,
  onClose,
  onConfirm,
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

  let title;
  let body;
  let buttons;

  if (mode === "thread") {
    title = "Delete entire thread?";
    body = (
      <>
        All your nodes in this thread will be deleted. Other users' replies
        are preserved (they own them) — they'll become top-level after the
        {" "}{graceDays}-day grace period.
      </>
    );
    buttons = (
      <>
        <button
          onClick={() => onConfirm({ withDescendants: true })}
          style={{ ...buttonBaseStyle, color: "var(--accent)" }}
        >
          Delete thread
        </button>
        <button
          onClick={onClose}
          style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
        >
          Cancel
        </button>
      </>
    );
  } else if (hasChildren) {
    title = "Delete node?";
    body = (
      <>
        This node has replies. Choose how to handle them:
      </>
    );
    buttons = (
      <>
        <button
          onClick={() => onConfirm({ withDescendants: false })}
          style={{ ...buttonBaseStyle, color: "var(--text-primary)" }}
        >
          <div style={{ fontWeight: 500 }}>Delete this node only</div>
          <div style={{
            fontSize: "0.82rem", color: "var(--text-muted)",
            fontWeight: 300, marginTop: "2px",
          }}>
            Replies stay; this node becomes a placeholder.
          </div>
        </button>
        <button
          onClick={() => onConfirm({ withDescendants: true })}
          style={{ ...buttonBaseStyle, color: "var(--accent)" }}
        >
          <div style={{ fontWeight: 500 }}>
            Delete this node and all my replies
          </div>
          <div style={{
            fontSize: "0.82rem", color: "var(--text-muted)",
            fontWeight: 300, marginTop: "2px",
          }}>
            Other users' replies are kept (they own them).
          </div>
        </button>
        <button
          onClick={onClose}
          style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
        >
          Cancel
        </button>
      </>
    );
  } else {
    title = "Delete node?";
    body = (
      <>
        This will be permanently deleted in {graceDays} days. Support can
        recover it during this window; after {graceDays} days, content and
        edit history are wiped.
      </>
    );
    buttons = (
      <>
        <button
          onClick={() => onConfirm({ withDescendants: false })}
          style={{ ...buttonBaseStyle, color: "var(--accent)" }}
        >
          Delete
        </button>
        <button
          onClick={onClose}
          style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
        >
          Cancel
        </button>
      </>
    );
  }

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle}>
        <h2 style={titleStyle}>{title}</h2>
        <div style={bodyStyle}>{body}</div>
        <div style={buttonRowStyle}>{buttons}</div>
      </div>
    </div>
  );
}

export default DeleteConfirmDialog;
