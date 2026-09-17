import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import api from "../api";
import { emailState } from "../utils/emailState";

// Where the link in the "Confirm your email" mail lands (#260). The page
// hands the link's token to POST /dashboard/email/confirm, which only
// accepts it inside a session of the account that asked for the change.
// Opening the link therefore never signs anyone in and, without that
// session, changes nothing: a stranger at a mistyped address, a mail
// scanner, or the owner of an address someone else asked for can do nothing
// with it. Not behind ProtectedRoute, because a waitlisted (unapproved)
// signup confirms their address here too.
export default function ConfirmEmailPage() {
  const { user, setUser, loading } = useUser();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const token = searchParams.get("token") || "";
  const [result, setResult] = useState(null); // { ok, email? , reason?, text? }
  // One POST per visit: effects run twice under StrictMode, and the user
  // context changes (re-running the effect) when the confirmation lands.
  const started = useRef(false);

  const confirm = useCallback(() => {
    setResult(null);
    api.post("/dashboard/email/confirm", { token })
      .then((res) => {
        setUser((prev) => (prev ? { ...prev, ...emailState(res.data) } : prev));
        setResult({ ok: true, email: res.data.email });
      })
      .catch((err) => {
        setResult({
          ok: false,
          reason: err.response?.data?.reason || "failed",
          text: err.response?.data?.error
            || "Could not confirm the address. Please try again.",
        });
      });
  }, [token, setUser]);

  useEffect(() => {
    if (loading || !user || !token || started.current) return;
    started.current = true;
    confirm();
  }, [loading, user, token, confirm]);

  const back = user && user.approved
    ? { to: "/account#email", label: "Back to your account" }
    : { to: "/alpha-thank-you", label: "Continue" };

  let heading;
  let body;
  let action = null;
  if (loading) {
    heading = "Confirming your email";
    body = "One moment.";
  } else if (!token) {
    heading = "This link is incomplete";
    body = "Open the link from the confirmation email again, or request a new one.";
    if (user) action = <Link to={back.to} style={linkStyle}>{back.label} &rarr;</Link>;
  } else if (!user) {
    const returnUrl = encodeURIComponent(location.pathname + location.search);
    heading = "Sign in to confirm";
    body = "A new sign-in email can only be confirmed from inside the account "
      + "that asked for it. Sign in the way you usually do and you will come "
      + "straight back here.";
    action = <Link to={`/login?returnUrl=${returnUrl}`} style={linkStyle}>Sign in &rarr;</Link>;
  } else if (!result) {
    heading = "Confirming your email";
    body = "One moment.";
  } else if (result.ok) {
    heading = "Email confirmed";
    body = (
      <>
        <strong style={strongStyle}>{result.email}</strong> is now your
        sign-in address.
      </>
    );
    action = <Link to={back.to} style={linkStyle}>{back.label} &rarr;</Link>;
  } else {
    heading = "Not confirmed";
    body = result.reason === "other_account"
      ? `${result.text} You are signed in as @${user.username}.`
      : result.text;
    // "failed" = no answer from the server (network, 5xx). Confirming is
    // idempotent, so trying again is safe.
    action = result.reason === "failed"
      ? <button type="button" onClick={confirm} style={retryStyle}>Try again</button>
      : <Link to={back.to} style={linkStyle}>{back.label} &rarr;</Link>;
  }

  return (
    <div style={{
      minHeight: "70vh", display: "flex", flexDirection: "column",
      alignItems: "center", justifyContent: "center",
      padding: "4rem 1.5rem", textAlign: "center",
    }}>
      <h1 style={{
        fontFamily: "var(--serif)", fontWeight: 300,
        fontSize: "clamp(1.6rem, 4vw, 2.2rem)", lineHeight: 1.2,
        color: "var(--text-primary)", maxWidth: 520, marginBottom: "1.2rem",
      }}>
        {heading}
      </h1>
      <p style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1rem",
        lineHeight: 1.8, color: "var(--text-secondary)", maxWidth: 460,
        marginBottom: "2rem", overflowWrap: "anywhere",
      }}>
        {body}
      </p>
      {action}
    </div>
  );
}

const linkStyle = {
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
  color: "var(--accent)", textDecoration: "none",
  borderBottom: "1px solid var(--accent-glow)", paddingBottom: 2,
};

const retryStyle = {
  ...linkStyle, background: "none", border: "none",
  borderBottom: linkStyle.borderBottom, padding: "0 0 2px 0", cursor: "pointer",
};

const strongStyle = { color: "var(--text-primary)", fontWeight: 400 };
