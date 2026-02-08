import React from "react";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function LandingPage() {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "70vh",
        padding: "20px",
        textAlign: "center",
        fontFamily: "sans-serif",
      }}
    >
      <h1 style={{ fontSize: "2rem", marginBottom: "1.5rem" }}>
        Loore: Uncover your lore. Self-author. Connect to the greater whole. Transcend.
      </h1>
      <a
        href={`${backendUrl}/auth/login`}
        style={{
          fontSize: "1.3rem",
          padding: "12px 32px",
          borderRadius: "6px",
          backgroundColor: "#1e1e1e",
          color: "#e0e0e0",
          border: "1px solid #333",
          textDecoration: "none",
          cursor: "pointer",
          transition: "background-color 0.3s",
        }}
        onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = "#333")}
        onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = "#1e1e1e")}
      >
        Sign up for Alpha
      </a>
    </div>
  );
}

export default LandingPage;