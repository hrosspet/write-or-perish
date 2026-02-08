import React, { useState, useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

const ERROR_MESSAGES = {
  invalid_or_expired: "This sign-in link is invalid or has expired. Please request a new one.",
  link_already_used: "This sign-in link has already been used. Please request a new one.",
};

const loginStyles = `
  .loore-login {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    position: relative;
    padding: 2rem;
    background: var(--bg-deep);
    overflow: hidden;
  }

  .loore-login::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse 80% 60% at 50% 0%, #1a150f 0%, transparent 70%),
      radial-gradient(ellipse 50% 40% at 80% 20%, #1a130d08 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
  }

  .loore-login-grain {
    position: absolute;
    inset: 0;
    opacity: 0.03;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
    background-size: 200px;
    pointer-events: none;
  }

  .loore-login-wordmark {
    font-family: var(--serif);
    font-weight: 300;
    font-size: 1.1rem;
    letter-spacing: 0.35em;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-bottom: 2.5rem;
    position: relative;
    z-index: 1;
    opacity: 0;
    animation: loore-login-fade 1s cubic-bezier(0.22,1,0.36,1) 0.1s forwards;
  }

  .loore-login-card {
    position: relative;
    z-index: 1;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 2.5rem 2.5rem 2rem;
    max-width: 400px;
    width: 100%;
    text-align: center;
    opacity: 0;
    animation: loore-login-fade 1s cubic-bezier(0.22,1,0.36,1) 0.25s forwards;
  }

  .loore-login-card h1 {
    font-family: var(--serif);
    font-weight: 300;
    font-size: 1.8rem;
    color: var(--text-primary);
    margin: 0 0 0.5rem;
    line-height: 1.3;
  }

  .loore-login-card .login-subtitle {
    font-family: var(--sans);
    font-weight: 300;
    font-size: 0.95rem;
    color: var(--text-muted);
    margin: 0 0 2rem;
  }

  .loore-login-btn {
    width: 100%;
    padding: 13px 20px;
    border-radius: 8px;
    font-size: 15px;
    font-family: var(--sans);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    transition: all 0.3s cubic-bezier(0.22,1,0.36,1);
    position: relative;
    overflow: hidden;
  }

  .loore-login-btn-x {
    background: transparent;
    color: var(--text-primary);
    border: 1px solid var(--border);
    font-weight: 400;
    margin-bottom: 12px;
  }

  .loore-login-btn-x:hover {
    border-color: var(--border-hover);
    background: rgba(196, 149, 106, 0.06);
  }

  .loore-login-btn-email {
    background: transparent;
    color: var(--text-secondary);
    border: 1px solid var(--border);
    font-weight: 300;
  }

  .loore-login-btn-email:hover {
    border-color: var(--border-hover);
    background: rgba(196, 149, 106, 0.06);
    color: var(--text-primary);
  }

  .loore-login-btn-submit {
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--accent);
    font-weight: 400;
  }

  .loore-login-btn-submit:hover {
    background: rgba(196, 149, 106, 0.1);
    box-shadow: 0 0 20px rgba(196, 149, 106, 0.15);
  }

  .loore-login-btn-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    box-shadow: none;
  }

  .loore-login-divider {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 16px 0;
  }

  .loore-login-divider-line {
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  .loore-login-divider-text {
    font-family: var(--sans);
    font-size: 0.75rem;
    color: var(--text-muted);
    letter-spacing: 0.08em;
  }

  .loore-login-input {
    width: 100%;
    padding: 12px 14px;
    background: var(--bg-deep);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 15px;
    font-family: var(--sans);
    font-weight: 300;
    margin-bottom: 12px;
    box-sizing: border-box;
    transition: border-color 0.3s ease;
  }

  .loore-login-input:focus {
    border-color: var(--accent);
    outline: none;
  }

  .loore-login-input::placeholder {
    color: var(--text-muted);
  }

  @keyframes loore-login-fade {
    from { opacity: 0; transform: translateY(16px); }
    to { opacity: 1; transform: translateY(0); }
  }

  @media (max-width: 480px) {
    .loore-login-card {
      padding: 2rem 1.5rem 1.5rem;
    }
  }
`;

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
    <>
      <style>{loginStyles}</style>
      <div className="loore-login">
        <div className="loore-login-grain" />

        <div className="loore-login-wordmark">Loore</div>

        <div className="loore-login-card">
          <h1>Welcome back</h1>
          <p className="login-subtitle">Sign in to continue your lore</p>

          {errorCode && ERROR_MESSAGES[errorCode] && (
            <p style={{
              color: "var(--accent)",
              marginBottom: "16px",
              fontSize: "0.85rem",
              fontFamily: "var(--sans)",
              fontWeight: 300,
              lineHeight: 1.5,
            }}>
              {ERROR_MESSAGES[errorCode]}
            </p>
          )}

          <button
            onClick={handleTwitterLogin}
            className="loore-login-btn loore-login-btn-x"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
              <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
            </svg>
            Sign in with X
          </button>

          {!showEmailForm && !emailSent && (
            <>
              <div className="loore-login-divider">
                <div className="loore-login-divider-line" />
                <span className="loore-login-divider-text">or</span>
                <div className="loore-login-divider-line" />
              </div>
              <button
                onClick={() => setShowEmailForm(true)}
                className="loore-login-btn loore-login-btn-email"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z" />
                </svg>
                Sign in with Email
              </button>
            </>
          )}

          {showEmailForm && !emailSent && (
            <form onSubmit={handleEmailSubmit} style={{ textAlign: "left", marginTop: "8px" }}>
              <label
                htmlFor="email"
                style={{
                  color: "var(--text-muted)",
                  fontSize: "0.7rem",
                  fontFamily: "var(--sans)",
                  fontWeight: 400,
                  textTransform: "uppercase",
                  letterSpacing: "0.15em",
                  display: "block",
                  marginBottom: "8px"
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
                className="loore-login-input"
              />
              {emailError && (
                <p style={{
                  color: "var(--accent)",
                  fontSize: "0.85rem",
                  margin: "0 0 12px 0",
                  fontFamily: "var(--sans)",
                  fontWeight: 300,
                }}>
                  {emailError}
                </p>
              )}
              <button
                type="submit"
                disabled={emailLoading}
                className="loore-login-btn loore-login-btn-submit"
              >
                {emailLoading ? "Sending..." : "Send Sign-in Link"}
              </button>
            </form>
          )}

          {emailSent && (
            <div style={{ marginTop: "8px" }}>
              <p style={{
                fontFamily: "var(--serif)",
                fontSize: "1.2rem",
                fontWeight: 300,
                color: "var(--accent)",
                margin: "0 0 0.5rem",
              }}>
                Check your inbox
              </p>
              <p style={{
                fontSize: "0.9rem",
                color: "var(--text-muted)",
                fontFamily: "var(--sans)",
                fontWeight: 300,
                lineHeight: 1.6,
                margin: 0,
              }}>
                We sent a sign-in link to{" "}
                <strong style={{ color: "var(--text-primary)", fontWeight: 400 }}>
                  {emailInput}
                </strong>.
                <br />
                It expires in 15 minutes.
              </p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

export default LoginPage;
