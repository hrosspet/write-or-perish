import React, { useState, useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

const ERROR_MESSAGES = {
  invalid_or_expired: "This sign-in link is invalid or has expired. Please request a new one.",
  link_already_used: "This sign-in link has already been used. Please request a new one.",
};

function LoginPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { user, loading } = useUser();

  const returnUrl = searchParams.get("returnUrl") || "/dashboard";
  const errorCode = searchParams.get("error");

  const [showEmailForm, setShowEmailForm] = useState(false);
  const [emailInput, setEmailInput] = useState("");
  const [emailLoading, setEmailLoading] = useState(false);
  const [emailSent, setEmailSent] = useState(false);
  const [emailError, setEmailError] = useState("");

  // If already logged in, redirect to returnUrl
  useEffect(() => {
    if (!loading && user) {
      navigate(returnUrl, { replace: true });
    }
  }, [user, loading, returnUrl, navigate]);

  const handleTwitterLogin = () => {
    const nextParam = encodeURIComponent(returnUrl);
    window.location.href = `${backendUrl}/auth/login?next=${nextParam}`;
  };

  const handleEmailSubmit = async (e) => {
    e.preventDefault();
    setEmailError("");
    setEmailLoading(true);

    try {
      const res = await fetch(`${backendUrl}/auth/magic-link/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: emailInput, next_url: returnUrl }),
      });
      const data = await res.json();
      if (!res.ok) {
        setEmailError(data.error || "Something went wrong. Please try again.");
      } else {
        setEmailSent(true);
      }
    } catch {
      setEmailError("Network error. Please try again.");
    } finally {
      setEmailLoading(false);
    }
  };

  if (loading) {
    return (
      <div style={{ padding: "40px", textAlign: "center", color: "var(--text-muted)" }}>
        <p>Loading...</p>
      </div>
    );
  }

  if (user) {
    return null;
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "calc(100vh - 100px)",
        padding: "40px"
      }}
    >
      <div
        style={{
          backgroundColor: "var(--bg-card)",
          padding: "40px",
          borderRadius: "10px",
          border: "1px solid var(--border)",
          maxWidth: "400px",
          width: "100%",
          textAlign: "center"
        }}
      >
        <h1 style={{
          fontFamily: "var(--serif)",
          fontWeight: 300,
          fontSize: "1.8rem",
          color: "var(--text-primary)",
          marginBottom: "10px"
        }}>
          Welcome to Loore
        </h1>
        <p style={{ color: "var(--text-muted)", marginBottom: "30px", fontFamily: "var(--sans)", fontWeight: 300 }}>
          Sign in to continue
        </p>

        {errorCode && ERROR_MESSAGES[errorCode] && (
          <p style={{ color: "var(--accent)", marginBottom: "20px", fontSize: "14px", fontFamily: "var(--sans)" }}>
            {ERROR_MESSAGES[errorCode]}
          </p>
        )}

        <button
          onClick={handleTwitterLogin}
          style={{
            width: "100%",
            padding: "12px 20px",
            backgroundColor: "#1DA1F2",
            color: "white",
            border: "none",
            borderRadius: "8px",
            fontSize: "16px",
            fontFamily: "var(--sans)",
            fontWeight: 400,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "10px",
            marginBottom: "15px"
          }}
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="currentColor"
          >
            <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
          </svg>
          Sign in with X
        </button>

        {!showEmailForm && !emailSent && (
          <button
            onClick={() => setShowEmailForm(true)}
            style={{
              width: "100%",
              padding: "12px 20px",
              backgroundColor: "transparent",
              color: "var(--text-secondary)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              fontSize: "16px",
              fontFamily: "var(--sans)",
              fontWeight: 300,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "10px"
            }}
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="currentColor"
            >
              <path d="M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z" />
            </svg>
            Sign in with Email
          </button>
        )}

        {showEmailForm && !emailSent && (
          <form onSubmit={handleEmailSubmit} style={{ textAlign: "left" }}>
            <label
              htmlFor="email"
              style={{
                color: "var(--text-muted)",
                fontSize: "0.75rem",
                fontFamily: "var(--sans)",
                fontWeight: 400,
                textTransform: "uppercase",
                letterSpacing: "0.12em",
                display: "block",
                marginBottom: "6px"
              }}
            >
              Email address
            </label>
            <input
              id="email"
              type="email"
              value={emailInput}
              onChange={(e) => setEmailInput(e.target.value)}
              placeholder="you@example.com"
              required
              style={{
                width: "100%",
                padding: "10px 12px",
                backgroundColor: "var(--bg-deep)",
                color: "var(--text-primary)",
                border: "1px solid var(--border)",
                borderRadius: "8px",
                fontSize: "16px",
                fontFamily: "var(--sans)",
                marginBottom: "12px",
                boxSizing: "border-box",
              }}
            />
            {emailError && (
              <p style={{ color: "var(--accent)", fontSize: "14px", margin: "0 0 12px 0", fontFamily: "var(--sans)" }}>
                {emailError}
              </p>
            )}
            <button
              type="submit"
              disabled={emailLoading}
              style={{
                width: "100%",
                padding: "12px 20px",
                backgroundColor: emailLoading ? "var(--bg-surface)" : "var(--accent)",
                color: emailLoading ? "var(--text-muted)" : "#0e0d0b",
                border: "none",
                borderRadius: "8px",
                fontSize: "16px",
                fontFamily: "var(--sans)",
                fontWeight: 500,
                cursor: emailLoading ? "not-allowed" : "pointer",
              }}
            >
              {emailLoading ? "Sending..." : "Send Sign-in Link"}
            </button>
          </form>
        )}

        {emailSent && (
          <div style={{ marginTop: "10px" }}>
            <p style={{ fontSize: "16px", fontWeight: 400, fontFamily: "var(--serif)", color: "var(--accent)" }}>Check your inbox!</p>
            <p style={{ fontSize: "14px", color: "var(--text-muted)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              We sent a sign-in link to <strong style={{ color: "var(--text-primary)" }}>{emailInput}</strong>.
              It expires in 15 minutes.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

export default LoginPage;
