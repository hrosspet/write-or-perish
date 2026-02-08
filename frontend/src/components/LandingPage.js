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
        Loore: <em>Uncover your lore. Self-author. Connect. Transcend.</em>
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
          marginBottom: "2.5rem",
        }}
        onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = "#333")}
        onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = "#1e1e1e")}
      >
        Sign up for Alpha
      </a>
      <div
        style={{
          maxWidth: "700px",
          textAlign: "left",
          lineHeight: "1.8",
          fontSize: "1.05rem",
          color: "#b0b0b0",
        }}
      >
        <p>
          Loore is a tool for self-authoring — helping you uncover the story you're living, see through your own blind spots, and gradually connect your personal becoming to the greater whole.
        </p>
        <p>
          You are already living a story. But most of it runs beneath awareness — patterns inherited, narratives distorted, intentions half-formed. Loore helps you uncover your own lore: the actual shape of your life, not just the story you tell yourself.
        </p>
        <p>
          Through effortless journaling and AI reflection, you surface what's hidden, name what's vague, and begin to author yourself more deliberately.
        </p>
        <p>
          This is private, first. Sacred, even. But as you clarify who you are and what you're for, sharing becomes natural — not performance, but offering. Your lore becomes part of a larger weave.
        </p>
        <p>
          AI is rapidly gaining agency. Loore helps you gain yours. Self-author. Connect. Transcend.
        </p>
      </div>
    </div>
  );
}

export default LandingPage;