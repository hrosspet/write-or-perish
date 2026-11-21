import React, { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import PrefillConsentCard from "../components/PrefillConsentCard";
import api from "../api";
import SubscriptionSection from "../components/SubscriptionSection";
import DeleteAccountDialog from "../components/DeleteAccountDialog";
import { downloadAccountData, EXPORT_FAILED_MESSAGE } from "../utils/dataExport";
import { SUBSCRIBE_X_LOGIN_MESSAGES, xLoginOutcome } from "../utils/xLogin";

const backendUrl = process.env.REACT_APP_BACKEND_URL || "";

// An account signed in by email without X (#714): Loore may have prepared
// an account for this person, keyed by their X account, which only Sign
// in with X finds. Shown above the levels, before a trial can start. The
// button is Connect X: an X account with a prepared account leads to the
// question at /prepared-account; any other is connected here, as Connect
// X on the Account page does.
export const PREPARED_ACCOUNT_LINE =
  "Did someone prepare a Loore account for you? Sign in with X to open it.";

function PreparedAccountLine() {
  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border)",
      borderRadius: 12, padding: "1.25rem 1.5rem", marginBottom: "2rem",
    }}>
      <p style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
        color: "var(--text-primary)", lineHeight: 1.6, margin: "0 0 1rem",
      }}>
        {PREPARED_ACCOUNT_LINE}
      </p>
      <a
        href={`${backendUrl}/auth/x/connect`}
        style={{
          display: "inline-flex", alignItems: "center", gap: "8px",
          padding: "10px 18px", borderRadius: 6,
          border: "1px solid var(--accent)", color: "var(--accent)",
          fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.9rem",
          textDecoration: "none",
        }}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
        </svg>
        Sign in with X
      </a>
    </div>
  );
}

// Payments on (#438; Peter, 2026-11-12: "people shouldn't be let in until
// they have a subscription"): an account that never had a subscription sees
// only this page, whatever the address. The levels, the code field and the
// trial; signing out; downloading its data; deleting the account. The
// server answers every other
// route with no_subscription, so nothing else could load anyway. Only this
// page for now (Peter, 2026-11-12: "the initiation experience still remains
// to be designed, so for now let's go with just the subscribe page").
export default function SubscribePage() {
  const { user, setUser } = useUser();
  const location = useLocation();
  const navigate = useNavigate();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [linkSent, setLinkSent] = useState(false);
  const [exportFailed, setExportFailed] = useState(false);
  const closeDelete = useCallback(() => setDeleteOpen(false), []);

  // Connect X comes back to /account?x_login=<outcome>#x, which is this
  // page for an account without a subscription: read the outcome once,
  // then drop it from the address so a reload doesn't repeat it.
  const [xMsg] = useState(
    () => SUBSCRIBE_X_LOGIN_MESSAGES[xLoginOutcome(location.search)] || null);
  useEffect(() => {
    if (xLoginOutcome(location.search) !== null) {
      navigate({ pathname: location.pathname, hash: location.hash }, { replace: true });
    }
  }, [location.search, location.pathname, location.hash, navigate]);

  // The account's own data as one file (Peter, 2026-11-13: "yes, Ben
  // should be able to export"), the menu's export: nothing else of the
  // app opens for it.
  const downloadData = async () => {
    setExportFailed(!(await downloadAccountData()));
  };

  const requestDeletion = async (typed) => {
    const res = await api.post("/account/delete", { confirm: typed });
    setDeleteOpen(false);
    if (res.data.status === "scheduled") {
      window.location.assign(
        `/account-deleted?on=${encodeURIComponent(res.data.delete_on || "")}`);
      return;
    }
    setLinkSent(true);
  };

  if (!user) return null;

  const quiet = {
    background: "none", border: "none", padding: 0, cursor: "pointer",
    color: "var(--text-muted)", fontFamily: "var(--sans)", fontWeight: 300,
    fontSize: "0.85rem", textDecoration: "none",
  };

  return (
    <div style={{ maxWidth: 600, margin: "0 auto", padding: "4rem 1.5rem" }}>
      <h2 style={{
        fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.6rem",
        color: "var(--text-primary)", margin: "0 0 0.75rem",
      }}>
        Welcome to Loore
      </h2>
      <p style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.95rem",
        color: "var(--text-secondary)", lineHeight: 1.7, margin: "0 0 2rem",
      }}>
        Your account has no active subscription yet. Loore opens as soon as one starts.
      </p>

      {xMsg && (
        <p role="status" style={{
          fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
          lineHeight: 1.6, margin: "0 0 1.5rem",
          color: xMsg.type === "error" ? "var(--accent)" : "var(--text-secondary)",
        }}>
          {xMsg.text}
        </p>
      )}

      {user.email && !user.twitter_login && <PreparedAccountLine />}

      {/* "Start from your tweets?" (#697): with payments on, a new account
          comes here, not to the waitlist that asked it before. Renders null
          unless X sign-in and unanswered. */}
      <PrefillConsentCard delayHint="Already on X?"
        style={{ maxWidth: "none", marginBottom: "2rem" }} />

      <SubscriptionSection user={user} setUser={setUser} />

      {linkSent && (
        <p role="status" style={{ ...quiet, cursor: "default", lineHeight: 1.6 }}>
          We sent a link to confirm the deletion to {user.email}.
        </p>
      )}
      {exportFailed && (
        <p role="alert" style={{ ...quiet, cursor: "default", lineHeight: 1.6 }}>
          {EXPORT_FAILED_MESSAGE}
        </p>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "1.5rem", marginTop: "3rem" }}>
        <a href={`${backendUrl}/auth/logout`} style={quiet}>Sign out</a>
        <button type="button" style={quiet} onClick={downloadData}>
          Download your data
        </button>
        <button type="button" style={quiet} onClick={() => setDeleteOpen(true)}>
          Delete my account
        </button>
      </div>
      <DeleteAccountDialog
        open={deleteOpen}
        username={user.username}
        email={user.email}
        info={user.account_deletion}
        xConnected={!!user.data_deletion?.x_connected}
        writingDeletionAt={null}
        onConfirm={requestDeletion}
        onClose={closeDelete}
      />
    </div>
  );
}
