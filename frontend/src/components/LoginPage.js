import React, { useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function LoginPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { user, loading } = useUser();

  const returnUrl = searchParams.get("returnUrl") || "/dashboard";

  // If already logged in, redirect to returnUrl
  useEffect(() => {
    if (!loading && user) {
      navigate(returnUrl, { replace: true });
    }
  }, [user, loading, returnUrl, navigate]);

  const handleTwitterLogin = () => {
    // Pass the returnUrl to the backend via the next parameter
    const nextParam = encodeURIComponent(returnUrl);
    window.location.href = `${backendUrl}/auth/login?next=${nextParam}`;
  };

  if (loading) {
    return (
      <div style={{ padding: "40px", textAlign: "center" }}>
        <p>Loading...</p>
      </div>
    );
  }

  // If user is logged in, show nothing while redirecting
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
          backgroundColor: "#1e1e1e",
          padding: "40px",
          borderRadius: "8px",
          border: "1px solid #333",
          maxWidth: "400px",
          width: "100%",
          textAlign: "center"
        }}
      >
        <h1 style={{ color: "#e0e0e0", marginBottom: "10px" }}>
          Welcome to Write or Perish
        </h1>
        <p style={{ color: "#888", marginBottom: "30px" }}>
          Sign in to continue
        </p>

        <button
          onClick={handleTwitterLogin}
          style={{
            width: "100%",
            padding: "12px 20px",
            backgroundColor: "#1DA1F2",
            color: "white",
            border: "none",
            borderRadius: "4px",
            fontSize: "16px",
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

        <button
          disabled
          style={{
            width: "100%",
            padding: "12px 20px",
            backgroundColor: "#333",
            color: "#666",
            border: "1px solid #444",
            borderRadius: "4px",
            fontSize: "16px",
            cursor: "not-allowed",
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
          Sign in with Email (Coming Soon)
        </button>
      </div>
    </div>
  );
}

export default LoginPage;
