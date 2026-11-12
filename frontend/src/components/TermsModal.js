import React, { useState } from 'react';
import api from '../api';
import LegalDocument, { renderInline } from '../legal/LegalDocument';
import terms from '../legal/terms.json';

// Shown over everything while the signed-in user's accepted terms are out
// of date. The text is frontend/src/legal/terms.json, the same as /terms
// and the iPhone app (#440).
function TermsModal({ onAccepted }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleAgree = () => {
    setLoading(true);
    api.post('/terms/accept')
      .then(() => {
        setLoading(false);
        onAccepted();
      })
      .catch(() => {
        setLoading(false);
        setError("Error accepting terms. Please try again.");
      });
  };

  const modalStyle = {
    position: "fixed",
    top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: "rgba(0, 0, 0, 0.7)",
    backdropFilter: "blur(8px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 10000
  };

  const contentStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    padding: "2rem",
    borderRadius: "12px",
    width: "80%",
    maxWidth: "800px",
    color: "var(--text-primary)",
    maxHeight: "80vh",
    overflowY: "auto",
    fontFamily: "var(--sans)",
    fontWeight: 300,
  };

  return (
    <div style={modalStyle}>
      <div style={contentStyle} role="dialog" aria-modal="true" aria-label="Terms of Service">
        <LegalDocument doc={terms} variant="modal" />

        <p className="loore-legal" style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
          {renderInline(terms.acceptNote, { newTab: true })}
        </p>

        {error && <p style={{ color: "var(--accent)" }}>{error}</p>}
        <button
          onClick={handleAgree}
          disabled={loading}
          style={{
            marginTop: "20px",
            borderColor: "var(--accent)",
            color: "var(--accent)",
            padding: "12px 24px",
            fontSize: "1rem",
          }}
        >
          {loading ? "Processing..." : "I Agree"}
        </button>
      </div>
    </div>
  );
}

export default TermsModal;
