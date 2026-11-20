import React, { useState } from "react";
import api from "../api";

// The invite link on the Account page (#439). It sits here and nowhere
// else: no reminders, no counts, no names (Peter, 2026-10-02/09). The
// link is made the first time it is asked for.

const labelStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 400,
  fontSize: "0.85rem",
  color: "var(--text-secondary)",
  marginBottom: "4px",
};

const textStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.92rem",
  color: "var(--text-primary)",
  lineHeight: 1.6,
};

const helperStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.78rem",
  color: "var(--text-muted)",
  marginTop: "6px",
  lineHeight: 1.6,
};

const buttonStyle = (disabled) => ({
  padding: "8px 16px",
  borderRadius: "6px",
  border: "1px solid var(--accent)",
  background: "none",
  color: "var(--accent)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.85rem",
  whiteSpace: "nowrap",
  cursor: disabled ? "default" : "pointer",
  opacity: disabled ? 0.4 : 1,
});

const inputStyle = {
  flex: 1,
  padding: "10px 12px",
  borderRadius: "6px",
  border: "1px solid var(--border)",
  backgroundColor: "var(--bg-input)",
  color: "var(--text-primary)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.9rem",
  boxSizing: "border-box",
  minWidth: 0,
};

export default function InviteSection() {
  const [url, setUrl] = useState(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(null);

  const show = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.get("/referrals/link");
      setUrl(res.data && res.data.url);
    } catch (e) {
      setError("Could not load your invite link. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch (e) {
      setCopied(false);
    }
  };

  return (
    <div id="invite" style={{ marginBottom: "1.25rem", scrollMarginTop: "72px" }}>
      <div style={labelStyle}>Invite a friend</div>
      <div style={textStyle}>
        Send your link to someone who might like Loore. When they make their first payment, you both get a free week.
      </div>
      {url ? (
        <div style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
          <input
            type="text"
            readOnly
            value={url}
            aria-label="Your invite link"
            onFocus={(e) => e.target.select()}
            style={inputStyle}
          />
          <button type="button" onClick={copy} style={buttonStyle(false)}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      ) : (
        <div style={{ marginTop: "10px" }}>
          <button type="button" onClick={show} disabled={busy} style={buttonStyle(busy)}>
            Show my invite link
          </button>
        </div>
      )}
      {error && <div role="status" style={{ ...helperStyle, color: "var(--error, #c0392b)" }}>{error}</div>}
      <div style={helperStyle}>
        Your friend signs up as anyone does and may wait for approval. You'll get a note when the free week arrives.
      </div>
    </div>
  );
}
