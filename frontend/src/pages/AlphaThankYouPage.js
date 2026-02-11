import React, { useState } from "react";
import { Link } from "react-router-dom";
import Fade from "../utils/Fade";
import { useUser } from "../contexts/UserContext";
import api from "../api";

export default function AlphaThankYouPage() {
  const { user, setUser } = useUser();
  const [email, setEmail] = useState(user?.email || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const needsEmail = user && (!user.email || user.email.trim() === "");

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const response = await api.put("/dashboard/user", { email });
      setUser(response.data.user);
      setSubmitted(true);
      setLoading(false);
    } catch (err) {
      console.error(err);
      setError("Error updating email. Please try again.");
      setLoading(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh", display: "flex", flexDirection: "column",
      alignItems: "center", justifyContent: "center",
      padding: "4rem 2rem", textAlign: "center",
      position: "relative",
    }}>
      {/* Warm glow */}
      <div style={{
        position: "absolute", top: "30%", left: "50%",
        transform: "translate(-50%, -50%)",
        width: 500, height: 500, borderRadius: "50%",
        background: "radial-gradient(circle, var(--accent-glow) 0%, transparent 65%)",
        pointerEvents: "none", opacity: 0.4,
      }} />

      <Fade>
        <div style={{
          fontFamily: "var(--serif)", fontSize: "2.5rem", color: "var(--accent)",
          marginBottom: "2rem", opacity: 0.6, lineHeight: 1,
        }}>{"\u2726"}</div>
      </Fade>
      <Fade delay={0.1}>
        <h1 style={{
          fontFamily: "var(--serif)", fontWeight: 300,
          fontSize: "clamp(2rem, 5vw, 3.2rem)", lineHeight: 1.2,
          color: "var(--text-primary)", maxWidth: 600, marginBottom: "1.5rem",
        }}>
          You're part of this now.
        </h1>
      </Fade>
      <Fade delay={0.2}>
        <p style={{
          fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
          lineHeight: 1.8, color: "var(--text-secondary)", maxWidth: 480,
          marginBottom: "3rem",
        }}>
          Thank you for signing up for the Loore Alpha. We're letting people in
          gradually — to keep things intimate and to give each person the attention
          they deserve as they begin.
        </p>
      </Fade>

      {/* Email form for users without email */}
      {needsEmail && !submitted && (
        <Fade delay={0.25}>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 12, padding: "2rem 2.2rem", maxWidth: 460,
            textAlign: "left", marginBottom: "2rem",
          }}>
            <div style={{
              fontFamily: "var(--sans)", fontSize: "0.7rem", letterSpacing: "0.15em",
              textTransform: "uppercase", color: "var(--accent)", opacity: 0.7,
              marginBottom: "1rem",
            }}>Get notified</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
              lineHeight: 1.7, color: "var(--text-secondary)", marginBottom: "1.2rem",
            }}>
              Leave your email and we'll let you know when your spot opens up.
            </p>
            <form onSubmit={handleSubmit}>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="your@email.com"
                style={{
                  width: "100%",
                  padding: "10px 12px",
                  backgroundColor: "var(--bg-deep)",
                  color: "var(--text-primary)",
                  border: "1px solid var(--border)",
                  borderRadius: "8px",
                  fontFamily: "var(--sans)",
                  fontSize: "16px",
                  boxSizing: "border-box",
                  marginBottom: "1rem",
                }}
              />
              {error && <div style={{ color: "var(--accent)", marginBottom: "0.8rem", fontSize: "0.88rem" }}>{error}</div>}
              <button
                type="submit"
                disabled={loading}
                style={{
                  border: "1px solid var(--accent)",
                  color: "var(--accent)",
                  background: "transparent",
                  padding: "10px 24px",
                  borderRadius: "6px",
                  fontFamily: "var(--sans)",
                  fontSize: "0.88rem",
                  fontWeight: 400,
                  cursor: loading ? "not-allowed" : "pointer",
                  opacity: loading ? 0.6 : 1,
                }}
              >
                {loading ? "Submitting..." : "Submit"}
              </button>
            </form>
          </div>
        </Fade>
      )}

      <Fade delay={0.28}>
        <div style={{
          background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 12, padding: "2rem 2.2rem", maxWidth: 460,
          textAlign: "left", marginBottom: "3rem",
        }}>
          <div style={{
            fontFamily: "var(--sans)", fontSize: "0.7rem", letterSpacing: "0.15em",
            textTransform: "uppercase", color: "var(--accent)", opacity: 0.7,
            marginBottom: "1.2rem",
          }}>What happens next</div>

          <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
            {[
              { num: "01", text: "We'll send you an email when your spot opens up. It shouldn't be long." },
              { num: "02", text: "You'll get a warm welcome with everything you need to begin." },
              { num: "03", text: "Start journaling — by text or voice — and let your lore unfold." },
            ].map(({ num, text }) => (
              <div key={num} style={{ display: "flex", gap: "1rem", alignItems: "flex-start" }}>
                <span style={{
                  fontFamily: "var(--serif)", fontSize: "1.1rem", fontWeight: 300,
                  color: "var(--accent)", opacity: 0.5, flexShrink: 0, marginTop: 2,
                }}>{num}</span>
                <p style={{
                  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
                  lineHeight: 1.7, color: "var(--text-secondary)",
                }}>{text}</p>
              </div>
            ))}
          </div>
        </div>
      </Fade>

      <Fade delay={0.36}>
        <p style={{
          fontFamily: "var(--serif)", fontWeight: 300, fontStyle: "italic",
          fontSize: "1.2rem", lineHeight: 1.5, color: "var(--text-muted)",
          maxWidth: 420, marginBottom: "2.5rem",
        }}>
          In the meantime — you might notice yourself already paying closer attention
          to the story you're living. That's the process beginning.
        </p>
      </Fade>

      <Fade delay={0.42}>
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center", gap: "1rem",
        }}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
            color: "var(--text-secondary)", marginBottom: "0.3rem",
          }}>
            You can learn more about where Loore is headed.
          </p>
          <Link to="/vision" style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.88rem",
            color: "var(--accent)", textDecoration: "none",
            borderBottom: "1px solid var(--accent-glow)",
            paddingBottom: 2,
          }}>
            Read the vision &rarr;
          </Link>
        </div>
      </Fade>
    </div>
  );
}
