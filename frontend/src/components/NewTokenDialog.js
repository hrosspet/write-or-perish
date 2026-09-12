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

const tokenBoxStyle = {
  display: "block",
  fontFamily: "var(--mono, ui-monospace, monospace)",
  fontSize: "0.85rem",
  color: "var(--text-primary)",
  background: "var(--bg-deep)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  padding: "12px 14px",
  wordBreak: "break-all",
  userSelect: "all",
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
 * The one moment a personal API token's plaintext exists on screen.
 * Only the hash is stored server-side, so once this closes the text is
 * gone for good; a lost token means minting a new one, which is cheap.
 *
 * Deliberately no click-outside or Escape dismissal: losing the token
 * to a stray click would be worse than one extra button press.
 */
function NewTokenDialog({ token, onClose }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => { setCopied(false); }, [token]);

  if (!token) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
    } catch (e) {
      setCopied(false);
    }
  };

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
        <code style={tokenBoxStyle}>{token}</code>
        <div style={buttonRowStyle}>
          <button
            onClick={copy}
            style={{ ...buttonBaseStyle, color: "var(--accent)" }}
          >
            <div style={{ fontWeight: 500 }}>{copied ? "Copied" : "Copy token"}</div>
            {!copied && <div style={subStyle}>Puts it on your clipboard.</div>}
          </button>
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
