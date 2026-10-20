import React, { useEffect, useState } from "react";

// Same shell as CraftModeDialog / RenameThreadDialog so it reads as one
// family. Shown once, right after a personal API token is minted.
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
  width: "480px",
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
  marginBottom: "1rem",
};

// The token box is the one active element: the whole box copies on
// click; the icon in its corner says so, and the copied state answers
// the click in place with just the icon turning into a green check
// (same icon pair and colors as the share cards in ProposalInline, so
// it reads as one family).
const tokenBoxStyle = {
  position: "relative",
  display: "block",
  width: "100%",
  boxSizing: "border-box",
  textAlign: "left",
  fontFamily: "var(--mono, ui-monospace, monospace)",
  fontSize: "0.85rem",
  lineHeight: 1.5,
  color: "var(--text-primary)",
  background: "var(--bg-deep)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  padding: "12px 40px 12px 14px",
  wordBreak: "break-all",
  cursor: "pointer",
  marginBottom: "0.5rem",
  transition: "border-color 0.15s ease",
};

const cornerStyle = {
  position: "absolute",
  top: "8px",
  right: "8px",
  display: "flex",
  lineHeight: 0,
  transition: "opacity 0.15s ease, color 0.15s ease",
};

const hintStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.82rem",
  fontWeight: 300,
  color: "var(--text-muted)",
  lineHeight: 1.5,
  margin: 0,
  marginBottom: "1.5rem",
  minHeight: "1.2em",
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
 * The one moment a personal API token's plaintext exists on screen.
 * Only the hash is stored server-side, so once this closes the text is
 * gone for good; a lost token means minting a new one, which is cheap.
 *
 * Deliberately no click-outside or Escape dismissal: losing the token
 * to a stray click would be worse than one extra button press.
 */
function NewTokenDialog({ token, onClose }) {
  const [copied, setCopied] = useState(false);
  const [hover, setHover] = useState(false);

  useEffect(() => { setCopied(false); }, [token]);

  useEffect(() => {
    if (!copied) return undefined;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);

  if (!token) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
    } catch (e) {
      setCopied(false);
    }
  };

  const cornerColor = copied ? "var(--success)" : "var(--text-muted)";

  return (
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-labelledby="new-token-title">
      <div style={cardStyle}>
        <h2 id="new-token-title" style={titleStyle}>Your clipper token</h2>
        <div style={bodyStyle}>
          Paste it into the extension's options now. It is shown only this
          once: Loore keeps just a fingerprint of it, so after you close this
          there is no way to see it again. If it gets lost, revoke it and
          create another.
        </div>
        <button
          type="button"
          onClick={copy}
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
          title="Copy token"
          aria-label="Copy token to clipboard"
          style={{
            ...tokenBoxStyle,
            borderColor: hover ? "var(--accent)" : "var(--border)",
          }}
        >
          {token}
          <span style={{ ...cornerStyle, color: cornerColor, opacity: copied || hover ? 1 : 0.6 }}>
            {copied ? (
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M3 8.5 L6.5 12 L13 4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
                <path d="M10.5 5.5 V4 A1.5 1.5 0 0 0 9 2.5 H4 A1.5 1.5 0 0 0 2.5 4 V9 A1.5 1.5 0 0 0 4 10.5 H5.5" stroke="currentColor" strokeWidth="1.2" />
              </svg>
            )}
          </span>
        </button>
        <p style={hintStyle}>Click the token to copy it.</p>
        <div style={buttonRowStyle}>
          <button
            onClick={onClose}
            style={{ ...buttonBaseStyle, color: "var(--text-secondary)" }}
          >
            <div style={{ fontWeight: 500 }}>Done, I've saved it</div>
            <div style={subStyle}>Closes this for good.</div>
          </button>
        </div>
      </div>
    </div>
  );
}

export default NewTokenDialog;
