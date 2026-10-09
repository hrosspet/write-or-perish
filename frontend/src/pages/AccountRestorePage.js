import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../api";
import { formatDeletionDate } from "../utils/dataDeletion";
import {
  choiceRowStyle, choiceStyle, headingStyle, linkStyle, pageStyle, textStyle,
} from "./accountPageStyles";

// Where a sign-in into a deleted account lands during its grace period
// (#269). The person is not signed in yet: the browser's session only
// holds the question, for a few minutes. Both choices are shown.
export default function AccountRestorePage() {
  const [state, setState] = useState({ loading: true });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api.get("/account/restore")
      .then((res) => alive && setState({ offer: res.data }))
      .catch(() => alive && setState({ none: true }));
    return () => { alive = false; };
  }, []);

  const restore = async () => {
    setBusy(true);
    try {
      const res = await api.post("/account/restore");
      // Signed in now: a full load starts the app with the account.
      window.location.assign(res.data.next || "/");
    } catch (e) {
      setState((s) => ({
        ...s,
        error: e.response?.data?.error || "Could not restore the account. Please try again.",
      }));
      setBusy(false);
    }
  };

  const keepDeleted = async () => {
    setBusy(true);
    try {
      await api.post("/account/restore/decline");
    } catch (e) {
      // Nothing to undo: the offer expires on its own.
    }
    setState({ declined: true });
    setBusy(false);
  };

  let heading;
  let body;
  let action = null;
  if (state.loading) {
    heading = "One moment";
    body = "";
  } else if (state.declined) {
    heading = "Your account stays deleted";
    body = "You are signed out. Nothing else changes.";
    action = <Link to="/" style={linkStyle}>Back to Loore &rarr;</Link>;
  } else if (state.none) {
    heading = "Nothing to restore here";
    body = "Sign in again to see your account's options.";
    action = <Link to="/login" style={linkStyle}>Sign in &rarr;</Link>;
  } else if (!state.offer.restorable) {
    heading = "Your account is being deleted";
    body = `The deletion of @${state.offer.username} has started and cannot be undone.`;
    action = <Link to="/" style={linkStyle}>Back to Loore &rarr;</Link>;
  } else {
    const date = formatDeletionDate(state.offer.delete_on);
    heading = "Restore your account?";
    body = (
      <>
        You deleted @{state.offer.username}. It is hidden, and on {date} it
        is deleted with everything in it. Restore it to keep using Loore,
        or keep it deleted. Restoring it also cancels a request to delete
        all your writing, if one is waiting.
      </>
    );
    action = (
      <div style={choiceRowStyle}>
        <button type="button" onClick={restore} disabled={busy}
          style={{ ...choiceStyle, color: "var(--accent)", opacity: busy ? 0.5 : 1 }}>
          Restore my account
        </button>
        <button type="button" onClick={keepDeleted} disabled={busy}
          style={{ ...choiceStyle, opacity: busy ? 0.5 : 1 }}>
          Keep it deleted
        </button>
        {state.error && (
          <p role="alert" style={{ ...textStyle, color: "var(--error)", fontSize: "0.9rem" }}>
            {state.error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div style={pageStyle}>
      <h1 style={headingStyle}>{heading}</h1>
      {body && <p role="status" style={textStyle}>{body}</p>}
      {action}
    </div>
  );
}
