import React, { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

// An invite followed, then a sign-in as someone it was not made for (#439;
// Peter, 2026-11-13: "They should know the invitation was for somebody
// else. They should still have the option to sign up without the
// invitation, thus also without reward"). The server holds that sign-in
// and makes no account until the person chooses: sign in differently
// (nothing is kept, and the invite code stays in this browser for the
// right sign-in) or continue without the invitation. The page says only
// which kind of sign-in the invitation was made for, never for whom.

const backendUrl = process.env.REACT_APP_BACKEND_URL || "";

const KIND_HINT = {
  x: "It was made for an X account.",
  email: "It was made for an email address.",
};

const textStyle = {
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.95rem",
  color: "var(--text-secondary)", lineHeight: 1.7, margin: "0 0 1rem",
};

const buttonStyle = (primary, disabled) => ({
  padding: "10px 18px",
  borderRadius: "6px",
  border: "1px solid var(--accent)",
  background: primary ? "var(--accent)" : "none",
  color: primary ? "var(--bg-primary, #fff)" : "var(--accent)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.9rem",
  cursor: disabled ? "default" : "pointer",
  opacity: disabled ? 0.5 : 1,
});

async function post(path) {
  const res = await fetch(`${backendUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: "{}",
  });
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  return { ok: res.ok, data };
}

export default function InviteMismatchPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const kind = new URLSearchParams(location.search).get("for");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const continueWithout = async () => {
    setBusy(true);
    setError(null);
    try {
      const { ok, data } = await post("/auth/invite-choice/continue");
      if (ok && data.redirect) {
        window.location.href = data.redirect;
        return;
      }
      setError(data.error || "This sign-in has expired. Please sign in again.");
    } catch (e) {
      setError("Something went wrong. Please try again.");
    }
    setBusy(false);
  };

  const signInDifferently = async () => {
    setBusy(true);
    try {
      await post("/auth/invite-choice/cancel");
    } catch (e) {
      // Nothing was made; the sign-in page is the way on either way.
    }
    navigate("/login", { replace: true });
  };

  return (
    <div style={{ maxWidth: 600, margin: "0 auto", padding: "4rem 1.5rem" }}>
      <h2 style={{
        fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.6rem",
        color: "var(--text-primary)", margin: "0 0 0.75rem",
      }}>
        This invitation is for someone else
      </h2>
      <p style={textStyle}>
        This invitation was made for a different email address or X account.
        {KIND_HINT[kind] ? ` ${KIND_HINT[kind]}` : ""} If it was meant for you, sign in with that one. You can also continue without the invitation.
      </p>
      {error && (
        <p role="status" style={{ ...textStyle, color: "var(--error, #c0392b)" }}>{error}</p>
      )}
      <div style={{ display: "flex", gap: "12px", flexWrap: "wrap", marginTop: "1.5rem" }}>
        <button type="button" onClick={signInDifferently} disabled={busy} style={buttonStyle(false, busy)}>
          Sign in differently
        </button>
        <button type="button" onClick={continueWithout} disabled={busy} style={buttonStyle(true, busy)}>
          Continue without the invitation
        </button>
      </div>
    </div>
  );
}
