import React, { useCallback, useState } from "react";
import { useUser } from "../contexts/UserContext";
import PrefillConsentCard from "../components/PrefillConsentCard";
import api from "../api";
import SubscriptionSection from "../components/SubscriptionSection";
import DeleteAccountDialog from "../components/DeleteAccountDialog";

const backendUrl = process.env.REACT_APP_BACKEND_URL || "";

// Payments on (#438; Peter, 2026-11-12: "people shouldn't be let in until
// they have a subscription"): an account that never had a subscription sees
// only this page, whatever the address. The levels, the code field and the
// trial; signing out; deleting the account. The server answers every other
// route with no_subscription, so nothing else could load anyway. Only this
// page for now (Peter, 2026-11-12: "the initiation experience still remains
// to be designed, so for now let's go with just the subscribe page").
export default function SubscribePage() {
  const { user, setUser } = useUser();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [linkSent, setLinkSent] = useState(false);
  const closeDelete = useCallback(() => setDeleteOpen(false), []);

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
      <div style={{ display: "flex", gap: "1.5rem", marginTop: "3rem" }}>
        <a href={`${backendUrl}/auth/logout`} style={quiet}>Sign out</a>
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
