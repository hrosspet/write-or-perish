import React, { useEffect } from "react";
import { Link } from "react-router-dom";

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
  maxHeight: "85vh",
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

const quoteStyle = {
  display: "block",
  fontFamily: "var(--sans)",
  fontSize: "0.86rem",
  fontWeight: 300,
  color: "var(--text-secondary)",
  lineHeight: 1.5,
  padding: "10px 12px",
  border: "1px solid var(--border)",
  borderRadius: "6px",
  marginBottom: "8px",
  textDecoration: "none",
};

export const QUOTES_NOT_PUBLIC_FALLBACK = (
  "This can't be made public yet: it quotes an entry that isn't public. "
  + "Make the quoted entry public first, then try again."
);

/**
 * Shown when something about to be public quotes an entry that isn't
 * (Peter, 2026-11-12): nothing was saved. Names each quoted entry and
 * links it, so the user can make it public first. *quoted* is the
 * server's list ({ id, can_open, preview, permalink }).
 */
function QuotesNotPublicDialog({ open, message, quoted, onClose }) {
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
    <div style={overlayStyle} onClick={onClose}>
      <div
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="quotes-not-public-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="quotes-not-public-title" style={titleStyle}>
          Not public yet
        </h2>
        <p style={bodyStyle}>{message || QUOTES_NOT_PUBLIC_FALLBACK}</p>
        {(quoted || []).map((q) => (q.can_open ? (
          <Link
            key={q.id}
            to={`/node/${q.id}`}
            style={quoteStyle}
            onClick={onClose}
          >
            {q.preview || `Entry #${q.id}`}
          </Link>
        ) : (
          <div key={q.id} style={quoteStyle}>
            An entry you can't open (#{q.id}). Remove its quote to publish.
          </div>
        )))}
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "1rem" }}>
          <button
            onClick={onClose}
            style={{
              fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 300,
              padding: "10px 18px", borderRadius: "6px", cursor: "pointer",
              background: "none", border: "1px solid var(--border)",
              color: "var(--text-secondary)",
            }}
          >
            OK
          </button>
        </div>
      </div>
    </div>
  );
}

export default QuotesNotPublicDialog;
