import React, { useState } from "react";
import api from "../api";

/**
 * The rebuild question after a re-import that set entries already in Loore
 * to None (#625, Peter 2026-11-11): "we should ask the user whether to
 * rebuild the profile, explaining that the import data are possibly
 * included in their current profile (not necessarily ...)".
 *
 * Design rules:
 *  - The server decides whether to ask (ask_profile_rebuild in the import's
 *    result): only when the profile in use may hold those entries.
 *  - Two equal answers, nothing preselected (as the both-providers
 *    question, #599). "Rebuild" sends the server's token back; "Keep" and
 *    closing the result without an answer send nothing and change nothing.
 *  - The iPhone app shows the same texts in the same place (ImportModel.swift).
 */

export function profileRebuildText(entries) {
  const many = entries !== 1;
  const count = `${entries.toLocaleString()} ${many ? "entries" : "entry"}`;
  return {
    title: "Rebuild your profile?",
    what: `This import set ${count} that ${many ? "were" : "was"} already in Loore to None, so AI no longer reads ${many ? "them" : "it"}.`,
    maybe: `Your current profile may still include what ${many ? "they say" : "it says"}, if it was built while AI could read ${many ? "them" : "it"}. It may also not: for example, if you set ${many ? "them" : "it"} to None soon after you first imported ${many ? "them" : "it"}.`,
    how: `A rebuild reads your writing again from the oldest of ${many ? "these entries" : "this entry"} on, without ${many ? "them" : "it"}. The part of your profile from before ${many ? "them" : "it"} stays as it is.`,
    yes: "Rebuild my profile",
    no: "Keep my profile as it is",
    savedYes: `Your profile will be rebuilt without ${many ? "these entries" : "this entry"}.`,
    savedNo: "Your profile stays as it is.",
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

/** ``question``: the import result's ask_profile_rebuild ({token, entries}). */
export default function ProfileRebuildQuestion({ question }) {
  const [busy, setBusy] = useState(false);
  const [answered, setAnswered] = useState(null); // true | false | null
  const [error, setError] = useState("");
  const t = profileRebuildText(question.entries || 0);

  const rebuild = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api.post("/import/profile-rebuild", { token: question.token });
      setAnswered(true);
    } catch (e) {
      setError(e?.response?.data?.error || "Couldn't start the rebuild. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  if (answered !== null) {
    return (
      <p role="status" style={{ ...bodyStyle, margin: 0 }}>
        {answered ? t.savedYes : t.savedNo}
      </p>
    );
  }

  return (
    <div>
      <p style={{
        fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.2rem",
        color: "var(--text-primary)", margin: "0 0 0.7rem",
      }}>{t.title}</p>
      <p style={bodyStyle}>{t.what}</p>
      <p style={bodyStyle}>{t.maybe}</p>
      <p style={{ ...bodyStyle, marginBottom: "1.3rem" }}>{t.how}</p>
      <div style={{ display: "flex", gap: "0.7rem", flexWrap: "wrap" }}>
        <button
          type="button"
          disabled={busy}
          onClick={rebuild}
          style={answerButtonStyle(busy)}
        >
          {busy ? "Starting…" : t.yes}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setAnswered(false)}
          style={answerButtonStyle(busy)}
        >
          {t.no}
        </button>
      </div>
      {error && (
        <div role="alert" style={{ color: "var(--accent)", marginTop: "0.8rem", fontFamily: "var(--sans)", fontSize: "0.85rem" }}>
          {error}
        </div>
      )}
    </div>
  );
}
