import React, { useState } from "react";
import { useUser } from "../contexts/UserContext";
import api from "../api";

/**
 * "Start from your tweets?" — the opt-in for seeding a fresh account from
 * its own public tweets (Community Archive) and generating the first
 * profile from them.
 *
 * Design rules (decided 2026-08-27):
 *  - Shown only to X-login users who haven't answered and haven't been
 *    pre-filled yet.
 *  - Two equal-weight buttons, nothing preselected. "Not now" is a real
 *    answer and is stored — it is NOT the same as dismissing.
 *  - Never a blocker: the card is one element on a page the user can
 *    simply leave. No answer = "we don't know", asked again once on
 *    /welcome, then it rests in Settings.
 */
export default function PrefillConsentCard({ style, delayHint }) {
  const { user, setUser } = useUser();
  const [busy, setBusy] = useState(null); // "yes" | "no" | null
  const [error, setError] = useState("");
  const [answered, setAnswered] = useState(null);

  // Gate on the server state — except right after answering, when the
  // user object already carries the answer but the card still owes the
  // person a confirmation line.
  if (!answered && (!user || !user.twitter_login || user.prefill_consent || user.prefilled_handle)) {
    return null;
  }

  const answer = async (value) => {
    setBusy(value);
    setError("");
    try {
      const res = await api.put("/dashboard/user", { prefill_consent: value });
      setUser(res.data.user);
      setAnswered(value);
    } catch (err) {
      console.error(err);
      setError("Couldn't save your answer. Please try again.");
    } finally {
      setBusy(null);
    }
  };

  const btn = (extra) => ({
    flex: 1,
    padding: "11px 18px",
    borderRadius: 6,
    fontFamily: "var(--sans)",
    fontSize: "0.9rem",
    fontWeight: 400,
    letterSpacing: "0.03em",
    cursor: busy ? "not-allowed" : "pointer",
    opacity: busy ? 0.6 : 1,
    background: "transparent",
    transition: "all 0.3s ease",
    ...extra,
  });

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border)",
      borderRadius: 12, padding: "2rem 2.2rem", maxWidth: 460,
      textAlign: "left", ...style,
    }}>
      <div style={{
        fontFamily: "var(--sans)", fontSize: "0.7rem", letterSpacing: "0.15em",
        textTransform: "uppercase", color: "var(--accent)", opacity: 0.7,
        marginBottom: "1rem",
      }}>{delayHint || "While you wait"}</div>

      {answered ? (
        <p style={{
          fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
          lineHeight: 1.7, color: "var(--text-secondary)",
        }}>
          {answered === "yes"
            ? "Noted. We'll seed your Loore from your public tweets and have a first profile waiting when you come in."
            : "Noted. You'll start from a blank page — you can always seed from your tweets later, from Settings."}
        </p>
      ) : (
        <>
          <p style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "1.35rem", lineHeight: 1.35,
            color: "var(--text-primary)", marginBottom: "0.8rem",
          }}>
            Start from what you've already written?
          </p>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
            lineHeight: 1.7, color: "var(--text-secondary)", marginBottom: "1.4rem",
          }}>
            With your okay, we'll bring in your <strong style={{ fontWeight: 400, color: "var(--text-primary)" }}>public tweets</strong> as
            @{user.username}, keep them private in your Loore, and write a first
            version of your profile from them — so you don't begin from a blank page.
            Only your own public posts; nothing is published.
          </p>
          <div style={{ display: "flex", gap: "0.7rem" }}>
            <button
              type="button"
              disabled={!!busy}
              onClick={() => answer("yes")}
              style={btn({ border: "1px solid var(--accent)", color: "var(--accent)" })}
            >
              {busy === "yes" ? "Saving…" : "Yes, seed from my tweets"}
            </button>
            <button
              type="button"
              disabled={!!busy}
              onClick={() => answer("no")}
              style={btn({ border: "1px solid var(--border)", color: "var(--text-secondary)" })}
            >
              {busy === "no" ? "Saving…" : "Not now"}
            </button>
          </div>
          {error && (
            <div style={{ color: "var(--error)", marginTop: "0.8rem", fontSize: "0.85rem" }}>{error}</div>
          )}
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.78rem",
            color: "var(--text-muted)", marginTop: "1rem", marginBottom: 0,
          }}>
            You can change your mind later in Settings.
          </p>
        </>
      )}
    </div>
  );
}
