import React from "react";
import { Link } from "react-router-dom";

// "Terms · Privacy": the footer line of the landing, login and Account
// pages (#440).
export default function LegalLinks({ style }) {
  const linkStyle = { color: "inherit", textDecoration: "none" };
  return (
    <nav
      aria-label="Legal"
      style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.8rem",
        color: "var(--text-muted)", display: "flex", gap: "0.6rem",
        justifyContent: "center", ...style,
      }}
    >
      <Link to="/terms" style={linkStyle}>Terms</Link>
      <span aria-hidden="true">·</span>
      <Link to="/privacy" style={linkStyle}>Privacy</Link>
    </nav>
  );
}
