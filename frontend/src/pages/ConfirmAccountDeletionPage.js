import React, { useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import api from "../api";
import { formatDeletionDate } from "../utils/dataDeletion";
import {
  choiceRowStyle, choiceStyle, headingStyle, linkStyle, pageStyle, textStyle,
} from "./accountPageStyles";

const backendUrl = process.env.REACT_APP_BACKEND_URL || "";
const DAY_MS = 24 * 3600 * 1000;

// Where the link in "Confirm the deletion of your Loore account" lands
// (#269). Like the email change (#260), the token only counts inside a
// session of the account it was sent for, and the page asks before it
// sends anything: opening the link (or a mail scanner fetching it)
// deletes nothing. Both choices are shown.
export default function ConfirmAccountDeletionPage() {
  const { user, loading } = useUser();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const token = searchParams.get("token") || "";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null); // { text, reason }

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post("/account/delete/confirm", { token });
      // Signed out on the server: a full load drops the app's state.
      window.location.assign(
        `/account-deleted?on=${encodeURIComponent(res.data.delete_on || "")}`);
    } catch (e) {
      setError({
        text: e.response?.data?.error || "Could not confirm. Please try again.",
        reason: e.response?.data?.reason || "failed",
      });
      setBusy(false);
    }
  };

  let heading;
  let body;
  let action = null;
  if (loading) {
    heading = "Delete your account";
    body = "One moment.";
  } else if (!token) {
    heading = "This link is incomplete";
    body = "Open the link from the email again, or ask for the deletion again on the Account page.";
  } else if (!user) {
    const returnUrl = encodeURIComponent(location.pathname + location.search);
    heading = "Sign in to confirm";
    body = "The deletion can only be confirmed from inside the account it "
      + "is for. Sign in the way you usually do, and you will come straight "
      + "back here.";
    action = <Link to={`/login?returnUrl=${returnUrl}`} style={linkStyle}>Sign in &rarr;</Link>;
  } else {
    const days = user.account_deletion?.grace_days || 30;
    const date = formatDeletionDate(new Date(Date.now() + days * DAY_MS));
    heading = `Delete @${user.username}?`;
    body = (
      <>
        When you confirm, your account is deleted and you are signed out
        everywhere. If you change your mind, you can still restore it by signing
        in until {date}; after that it is deleted forever, with
        everything in it. If you had also deleted all your writing,
        restoring your account brings your writing back too.
      </>
    );
    action = (
      <div style={choiceRowStyle}>
        <button type="button" onClick={confirm} disabled={busy}
          style={{ ...choiceStyle, color: "var(--error)", opacity: busy ? 0.5 : 1 }}>
          {busy ? "One moment…" : "Delete my account"}
        </button>
        <Link to="/account" style={{ ...choiceStyle, textDecoration: "none", textAlign: "center" }}>
          Keep my account
        </Link>
        {error && (
          <p role="alert" style={{ ...textStyle, color: "var(--error)", fontSize: "0.9rem" }}>
            {error.text}
            {error.reason === "other_account" && (
              <>
                {" "}You are signed in as @{user.username}.{" "}
                <a href={`${backendUrl}/auth/logout?next=${encodeURIComponent(location.pathname + location.search)}`}
                  style={linkStyle}>Sign out and use the other account</a>
              </>
            )}
          </p>
        )}
      </div>
    );
  }

  return (
    <div style={pageStyle}>
      <h1 style={headingStyle}>{heading}</h1>
      <p role="status" style={textStyle}>{body}</p>
      {action}
    </div>
  );
}
