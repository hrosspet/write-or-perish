import React, { useState } from 'react';
import api from '../api';
import LegalDocument, { renderInline } from '../legal/LegalDocument';
import terms from '../legal/terms.json';
import { useUser } from '../contexts/UserContext';
import { startExportDownload } from '../utils/exportData';
import DeleteAccountBlock from './DeleteAccountBlock';

const backendUrl = process.env.REACT_APP_BACKEND_URL;

// Shown over everything while the signed-in user's accepted terms are out
// of date. The text is frontend/src/legal/terms.json, the same as /terms
// and the iPhone app (#440).
//
// "I Agree" sends the version this page showed. If the server's current
// version is another one (the page was loaded before a deploy), it records
// nothing and answers 409; the page then reloads to show the current text.
//
// A person who doesn't accept can still leave: export their writing,
// delete their account, or sign out, without agreeing (Terms §13).
function TermsModal({ onAccepted, onOutdated = () => window.location.reload() }) {
  const { user } = useUser();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [exportNote, setExportNote] = useState("");

  const handleAgree = () => {
    setLoading(true);
    setError("");
    api.post('/terms/accept', { version: terms.version })
      .then(() => {
        setLoading(false);
        onAccepted();
      })
      .catch((err) => {
        setLoading(false);
        if (err?.response?.status === 409
            && err.response.data?.error === 'terms_version_mismatch') {
          setError("The Terms have changed since this page loaded. Loading the current Terms…");
          onOutdated();
          return;
        }
        setError("Error accepting terms. Please try again.");
      });
  };

  const handleExport = async () => {
    setExportNote("");
    const started = await startExportDownload();
    setExportNote(started
      ? "Your export is downloading."
      : "The export couldn't start. Please try again.");
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

  const quietText = {
    fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem",
    lineHeight: 1.6, color: "var(--text-muted)", margin: "0 0 0.8rem",
  };

  const quietButton = {
    padding: "8px 16px", borderRadius: "6px", border: "1px solid var(--border)",
    background: "none", color: "var(--text-secondary)", cursor: "pointer",
    fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem",
  };

  return (
    <div style={modalStyle}>
      <div style={contentStyle} role="dialog" aria-modal="true" aria-label="Terms of Service">
        <LegalDocument doc={terms} variant="modal" />

        <p className="loore-legal" style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
          {renderInline(terms.acceptNote, { newTab: true })}
        </p>

        {error && <p role="alert" style={{ color: "var(--accent)" }}>{error}</p>}
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

        <section aria-label="Without agreeing" style={{
          marginTop: "2.5rem", paddingTop: "1.5rem", borderTop: "1px solid var(--border)",
        }}>
          <p style={{ ...quietText, color: "var(--text-secondary)" }}>
            Not ready to agree? You can take your writing with you, delete
            your account, or sign out, without agreeing.
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", marginBottom: "1.2rem" }}>
            {user?.approved && (
              <button type="button" onClick={handleExport} style={quietButton}>
                Export my data
              </button>
            )}
            <a href={`${backendUrl}/auth/logout`} style={{ ...quietButton, textDecoration: "none" }}>
              Sign out
            </a>
          </div>
          {exportNote && <p role="status" style={quietText}>{exportNote}</p>}
          <DeleteAccountBlock user={user} dialogZIndex={10001} />
        </section>
      </div>
    </div>
  );
}

export default TermsModal;
