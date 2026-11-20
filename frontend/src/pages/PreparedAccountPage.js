import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { skipUpdatesOnce } from "../hooks/useSessionUpdates";

// Connect X found the account Loore prepared for this X account (#714):
// an invited person who signed in by email is in a second account. The
// server holds the X proof in this browser's session and asks: open the
// prepared account, or stay. Opening it signs in to it as Sign in with X
// would; an email account that holds no writing (and never paid) is
// deleted on the way, and its address and a trial started on it go with
// the person (backend/utils/prepared_accounts.py). Nothing moves until
// the person presses Open.

const backendUrl = process.env.REACT_APP_BACKEND_URL || "";

const textStyle = {
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.95rem",
  color: "var(--text-secondary)", lineHeight: 1.7, margin: "0 0 1rem",
};

const buttonStyle = (primary, disabled) => ({
  padding: "10px 18px",
  borderRadius: "6px",
  border: "1px solid var(--accent)",
  background: primary ? "var(--accent)" : "none",
  color: primary ? "var(--bg-primary, #fff)" : "var(--accent)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.9rem",
  cursor: disabled ? "default" : "pointer",
  opacity: disabled ? 0.5 : 1,
});

async function call(path, method = "GET", body) {
  const res = await fetch(`${backendUrl}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    ...(method === "GET" ? {} : { body: JSON.stringify(body || {}) }),
  });
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  return { ok: res.ok, status: res.status, data };
}

const EXPIRED = "This question has expired. Use Sign in with X again to open the prepared account.";

// Why the account the person is in stays (the server's stays_because).
const STAYS = {
  writing: "It holds writing, so it stays as it is.",
  used: "It holds invitations you sent, poll answers or feedback, so it stays as it is.",
  paid: "It has a paid subscription, so it stays as it is.",
  kept: "It stays as it is.",
};

// What happens to the account the person is in. A deleted account is
// deleted as any account is (30 days, then for good). Its restore by
// signing in is offered only when its address stays with it: when the
// address moves to the prepared account, nothing signs in to it any more.
export function thisAccountText(q) {
  const made = q.email
    ? `You are signed in to another account, made when you signed in with ${q.email}.`
    : "You are signed in to another account.";
  if (!q.deleted) {
    const back = q.email ? ` Sign in with ${q.email} to come back to it.` : "";
    return `${made} ${STAYS[q.stays_because] || STAYS.kept}${back}`;
  }
  const parts = [`${made} It holds no writing, so opening the prepared account deletes it.`];
  if (q.email_stays) {
    parts.push(`Your address ${q.email} stays with it: if you change your mind, you can still restore it within 30 days by signing in with ${q.email}; after that it is deleted forever. The prepared account opens with Sign in with X.`);
  } else {
    parts.push("After 30 days it is deleted forever.");
  }
  if (q.email_moves) {
    parts.push(`From then on, ${q.email} signs in to the prepared account.`);
  }
  if (q.trial_ends) {
    parts.push("The trial you started here ends now, with nothing charged, and the prepared account can start its own trial with the same card.");
  }
  return parts.join(" ");
}

export default function PreparedAccountPage() {
  const navigate = useNavigate();
  const [question, setQuestion] = useState(null);
  const [error, setError] = useState(null);
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const { ok, data } = await call("/auth/prepared-account");
      if (ok) {
        setQuestion(data);
      } else {
        setExpired(true);
      }
    } catch (e) {
      setError("Something went wrong. Please reload the page.");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const open = async () => {
    setBusy(true);
    setError(null);
    try {
      const { ok, status, data } = await call(
        "/auth/prepared-account/open", "POST",
        { delete_this_account: !!question.deleted });
      if (ok && data.redirect) {
        // A full load: the app reads who is signed in again, with no
        // updates popup over the first page (as after a restore).
        skipUpdatesOnce();
        window.location.href = data.redirect;
        return;
      }
      if (status === 410) {
        setExpired(true);
      } else {
        setError(data.error || "Something went wrong. Please try again.");
        // Something changed since the question was shown: show it anew.
        if (data.code === "changed") await load();
      }
    } catch (e) {
      setError("Something went wrong. Please try again.");
    }
    setBusy(false);
  };

  const stay = async () => {
    setBusy(true);
    try {
      await call("/auth/prepared-account/stay", "POST");
    } catch (e) {
      // Nothing moved; the account page is the way on either way.
    }
    navigate("/account?x_login=cancelled#x", { replace: true });
  };

  const heading = (
    <h2 style={{
      fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.6rem",
      color: "var(--text-primary)", margin: "0 0 0.75rem",
    }}>
      An account was prepared for you
    </h2>
  );

  if (expired) {
    return (
      <div style={{ maxWidth: 600, margin: "0 auto", padding: "4rem 1.5rem" }}>
        {heading}
        <p role="status" style={textStyle}>{EXPIRED}</p>
        <button type="button" onClick={() => navigate("/", { replace: true })}
          style={buttonStyle(false, false)}>
          Back
        </button>
      </div>
    );
  }
  if (!question) {
    return error ? (
      <div style={{ maxWidth: 600, margin: "0 auto", padding: "4rem 1.5rem" }}>
        <p role="alert" style={textStyle}>{error}</p>
      </div>
    ) : null;
  }

  return (
    <div style={{ maxWidth: 600, margin: "0 auto", padding: "4rem 1.5rem" }}>
      {heading}
      <p style={textStyle}>
        Loore set up an account for the X account @{question.handle} before you joined. Sign in with X opens it.
      </p>
      <p style={textStyle}>{thisAccountText(question)}</p>
      {error && (
        <p role="alert" style={{ ...textStyle, color: "var(--accent)" }}>{error}</p>
      )}
      <div style={{ display: "flex", gap: "12px", flexWrap: "wrap", marginTop: "1.5rem" }}>
        <button type="button" onClick={stay} disabled={busy} style={buttonStyle(false, busy)}>
          Stay in this account
        </button>
        <button type="button" onClick={open} disabled={busy} style={buttonStyle(true, busy)}>
          Open the prepared account
        </button>
      </div>
    </div>
  );
}
