import React, { useState } from 'react';
import api from "../api";

function AlphaModal({ user, onUpdate }) {
  const [email, setEmail] = useState(user.email || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      // Update the user's email via the same update endpoint.
      const response = await api.put("/dashboard/user", { email });
      setLoading(false);
      onUpdate(response.data.user);  // Update the user info in context.
    } catch (err) {
      console.error(err);
      setError("Error updating email. Please try again.");
      setLoading(false);
    }
  };

  const modalStyle = {
    position: "fixed",
    top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: "rgba(0,0,0,0.7)",
    backdropFilter: "blur(8px)",
    display:"flex",
    alignItems:"center",
    justifyContent:"center",
    zIndex: 2000
  };

  const contentStyle = {
    backgroundColor: "var(--bg-card)",
    border: "1px solid var(--border)",
    padding: "2rem",
    borderRadius: "12px",
    width: "400px",
    maxWidth: "90vw",
    color: "var(--text-primary)",
    fontFamily: "var(--sans)",
    fontWeight: 300,
  };

  // If the user already provided an email, show the thank-you message
  if (user.email && user.email.trim() !== "") {
    return (
      <div style={modalStyle}>
        <div style={contentStyle}>
          <h2 style={{ fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.4rem" }}>Alpha Release</h2>
          <p style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
            Thank you for providing your email. You've been added to our waiting list.
            We will contact you at <strong style={{ color: "var(--text-primary)" }}>{user.email}</strong> once your account is approved.
          </p>
        </div>
      </div>
    );
  }

  // Otherwise, prompt for the email.
  return (
    <div style={modalStyle}>
      <div style={contentStyle}>
        <h2 style={{ fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.4rem" }}>Alpha Release</h2>
        <p style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
          Thank you for signing up for our limited alpha release! You've been added to the waiting list.
          If you'd like to be notified when your account is approved, please provide your email below.
        </p>
        <form onSubmit={handleSubmit}>
          <div>
            <label htmlFor="email" style={{
              fontFamily: "var(--sans)",
              fontWeight: 400,
              fontSize: "0.75rem",
              textTransform: "uppercase",
              letterSpacing: "0.12em",
              color: "var(--text-muted)",
            }}>Email (optional)</label>
            <input
              type="email"
              id="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              style={{
                width: "100%",
                padding: "10px 12px",
                marginTop: "8px",
                backgroundColor: "var(--bg-deep)",
                color: "var(--text-primary)",
                border: "1px solid var(--border)",
                borderRadius: "8px",
                fontFamily: "var(--sans)",
                fontSize: "16px",
                boxSizing: "border-box",
              }}
            />
          </div>
          {error && <div style={{ color: "var(--accent)", marginTop: "10px" }}>{error}</div>}
          <div style={{ marginTop: "20px" }}>
            <button
              type="submit"
              disabled={loading}
              style={{
                borderColor: "var(--accent)",
                color: "var(--accent)",
                padding: "10px 20px",
              }}
            >
              {loading ? "Submitting..." : "Submit"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default AlphaModal;
