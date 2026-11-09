import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../api";
import { useUser } from "../contexts/UserContext";

/**
 * The both-providers question (#599, Peter 2026-11-05): may background
 * tasks use both Anthropic's and OpenAI's models, or only the provider of
 * the user's default (preferred) model? Today the answer covers building
 * the profile: with a yes, a build with a lot of writing to read runs its
 * versions on GPT-6 Luna and its merges on Opus 5.5.
 *
 * Design rules (decided 2026-11-05):
 *  - Two equal buttons, nothing preselected. Closing without choosing
 *    records nothing: "we don't know" is not "no", and the next occasion
 *    asks again.
 *  - Where and when it is asked is the server's call
 *    (user.ask_both_providers, backend/utils/profile_providers.py); the
 *    pages decide only whether now is a good moment.
 *  - The answer can be changed any time under Background tasks on the
 *    Account page.
 */

export const PROVIDER_NAMES = { anthropic: "Anthropic", openai: "OpenAI" };

export function ownProviderName(user) {
  return PROVIDER_NAMES[user?.profile_provider] || "your default model's provider";
}

// Every text of the question in one place.
export function bothProvidersText(own) {
  return {
    title: "Use both Anthropic and OpenAI?",
    ask: `Loore does some work in the background, such as building your profile from your writing. Is it fine with you if we use both Anthropic and OpenAI for this, or do you want to stick to ${own}?`,
    both: "In our tests, using both gave the best results. With both, your writing can be read by OpenAI's and Anthropic's models.",
    one: `With ${own} only, all of it stays with ${own}, as it does now.`,
    note: "Replies use your default model either way.",
    yes: "Use both",
    no: `Keep to ${own}`,
    savedYes: "Saved: Anthropic and OpenAI.",
    savedNo: `Saved: ${own} only.`,
  };
}

const bodyStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.92rem",
  lineHeight: 1.65,
  color: "var(--text-secondary)",
  margin: "0 0 0.9rem",
};

const noteStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.8rem",
  lineHeight: 1.6,
  color: "var(--text-muted)",
  margin: "0.9rem 0 0",
};

// Both buttons look the same: neither answer is the expected one.
const answerButtonStyle = (busy) => ({
  flex: "1 1 0",
  minWidth: 0,
  padding: "11px 14px",
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "transparent",
  color: "var(--text-primary)",
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 400,
  letterSpacing: "0.02em",
  cursor: busy ? "not-allowed" : "pointer",
  opacity: busy ? 0.6 : 1,
  transition: "border-color 0.2s ease, color 0.2s ease",
});

const changeLink = (
  <>
    You can change this under{" "}
    <Link to="/account#background-tasks" style={{ color: "var(--accent)" }}>
      Background tasks
    </Link>{" "}
    on the Account page.
  </>
);

/**
 * The question itself: text and the two answers. Used inside the dialog
 * below and inside the import result. ``onAnswered(value)`` runs after the
 * answer is saved; the user object is updated from the server's reply.
 */
export function BothProvidersQuestion({ onAnswered, title = false }) {
  const { user, setUser } = useUser() || {};
  const [busy, setBusy] = useState(null); // true | false | null
  const [error, setError] = useState("");
  const [answered, setAnswered] = useState(null);
  const own = ownProviderName(user);
  const t = bothProvidersText(own);

  const answer = async (value) => {
    if (busy !== null) return;
    setBusy(value);
    setError("");
    try {
      const res = await api.put("/dashboard/user", { profile_both_providers: value });
      if (res.data.user && setUser) setUser(res.data.user);
      setAnswered(value);
      if (onAnswered) onAnswered(value);
    } catch (e) {
      setError("Couldn't save your answer. Please try again.");
    } finally {
      setBusy(null);
    }
  };

  if (answered !== null) {
    return (
      <p role="status" style={{ ...bodyStyle, margin: 0 }}>
        {answered ? t.savedYes : t.savedNo} {changeLink}
      </p>
    );
  }

  return (
    <div>
      {title && (
        <p style={{
          fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.2rem",
          color: "var(--text-primary)", margin: "0 0 0.7rem",
        }}>{t.title}</p>
      )}
      <p style={bodyStyle}>{t.ask}</p>
      <p style={bodyStyle}>{t.both}</p>
      <p style={{ ...bodyStyle, marginBottom: "1.3rem" }}>{t.one}</p>
      <div style={{ display: "flex", gap: "0.7rem", flexWrap: "wrap" }}>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => answer(true)}
          style={answerButtonStyle(busy !== null)}
        >
          {busy === true ? "Saving…" : t.yes}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => answer(false)}
          style={answerButtonStyle(busy !== null)}
        >
          {busy === false ? "Saving…" : t.no}
        </button>
      </div>
      {error && (
        <div role="alert" style={{ color: "var(--accent)", marginTop: "0.8rem", fontFamily: "var(--sans)", fontSize: "0.85rem" }}>
          {error}
        </div>
      )}
      <p style={noteStyle}>{t.note} {changeLink}</p>
    </div>
  );
}

const overlayStyle = {
  position: "fixed",
  top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  // Above the nav (1000) and its dropdown (1001), like the other dialogs.
  zIndex: 1100,
};

const cardStyle = {
  position: "relative",
  background: "var(--bg-card)",
  border: "1px solid var(--border)",
  borderRadius: "12px",
  padding: "2rem",
  width: "460px",
  maxWidth: "calc(100vw - 32px)",
  maxHeight: "90vh",
  overflowY: "auto",
  boxSizing: "border-box",
};

/**
 * The question as a dialog (on opening Loore, after a preferred-model
 * change). ``onClose(answered)``: Escape, the backdrop and the close mark
 * close it with answered = false, which records nothing.
 */
export default function BothProvidersDialog({ onClose }) {
  const { user } = useUser() || {};
  const [answered, setAnswered] = useState(false);
  const t = bothProvidersText(ownProviderName(user));

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose(answered);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, answered]);

  return (
    <div onClick={() => onClose(answered)} style={overlayStyle}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="both-providers-title"
        onClick={(e) => e.stopPropagation()}
        style={cardStyle}
      >
        <button
          type="button"
          aria-label="Close"
          onClick={() => onClose(answered)}
          style={{
            position: "absolute", top: 12, right: 14,
            background: "none", border: "none", cursor: "pointer",
            color: "var(--text-muted)", fontSize: "1.3rem", lineHeight: 1,
            padding: 4,
          }}
        >
          ×
        </button>
        <h2 id="both-providers-title" style={{
          fontFamily: "var(--serif)", fontSize: "1.4rem", fontWeight: 400,
          color: "var(--text-primary)", margin: "0 1.5rem 1rem 0",
        }}>
          {t.title}
        </h2>
        <BothProvidersQuestion onAnswered={() => setAnswered(true)} />
        {answered && (
          <div style={{ marginTop: "1.3rem", textAlign: "right" }}>
            <button
              type="button"
              onClick={() => onClose(true)}
              style={{
                ...answerButtonStyle(false), flex: "none",
                padding: "9px 22px", color: "var(--accent)",
                border: "1px solid var(--accent)",
              }}
            >
              Done
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
