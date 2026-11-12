import React, { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import LegalDocument from "../legal/LegalDocument";
import terms from "../legal/terms.json";
import privacy from "../legal/privacy.json";

// /terms and /privacy (#440): public, no sign-in. The text comes from the
// same JSON the acceptance dialog and the iPhone app use.
const DOCS = {
  terms: { doc: terms, other: { to: "/privacy", label: "Privacy Policy" } },
  privacy: { doc: privacy, other: { to: "/terms", label: "Terms of Service" } },
};

export const LEGAL_PATHS = ["/terms", "/privacy"];

export default function LegalPage({ kind }) {
  const { doc, other } = DOCS[kind];
  const { hash } = useLocation();

  // A link to a section (/privacy#prefill) scrolls to it once rendered.
  useEffect(() => {
    if (!hash) {
      window.scrollTo(0, 0);
      return;
    }
    const el = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (el && el.scrollIntoView) el.scrollIntoView();
  }, [kind, hash]);

  return (
    <div className="loore-legal-page" style={{ padding: "3rem 1.5rem 5rem" }}>
      <div style={{ maxWidth: 680, margin: "0 auto" }}>
        <div style={{
          fontFamily: "var(--sans)", fontSize: "0.72rem", letterSpacing: "0.2em",
          textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "1.6rem",
        }}>
          Loore
        </div>
        <LegalDocument doc={doc} variant="page" />
        <div style={{
          marginTop: "3rem", paddingTop: "1.5rem", borderTop: "1px solid var(--border)",
          fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
          color: "var(--text-muted)",
        }}>
          See also the{" "}
          <Link to={other.to} style={{ color: "var(--accent)", textDecoration: "none" }}>
            {other.label}
          </Link>.
        </div>
      </div>
    </div>
  );
}
