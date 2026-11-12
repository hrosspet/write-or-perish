import React, { useCallback, useState } from "react";
import { createPortal } from "react-dom";
import api from "../api";
import DeleteAccountDialog from "./DeleteAccountDialog";

// "Delete my account" as a short text, a button and the dialog (#269), for
// the pages where the Account page can't be reached: the waitlist page
// (every account can delete itself) and the Terms dialog, where a person
// who doesn't accept new Terms can leave (#440). The same dialog and
// request as the Account page.
//
// dialogZIndex: the dialog opens above another dialog (the Terms dialog's
// z-index is 10000). It is then rendered into <body>, outside that
// dialog's stacking context.
export default function DeleteAccountBlock({ user, style, dialogZIndex }) {
  const [open, setOpen] = useState(false);
  const [linkSent, setLinkSent] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const info = user?.account_deletion;
  if (!info) return null;

  const requestDeletion = async (typed) => {
    const res = await api.post("/account/delete", { confirm: typed });
    setOpen(false);
    if (res.data.status === "scheduled") {
      // Signed out on the server: a full load drops the app's state.
      window.location.assign(
        `/account-deleted?on=${encodeURIComponent(res.data.delete_on || "")}`);
      return;
    }
    setLinkSent(true);
  };

  const days = info.grace_days || 30;
  const minutes = Math.round((info.link_expires_in || 3600) / 60);
  const dialog = (
    <DeleteAccountDialog
      open={open}
      username={user.username}
      email={user.email}
      info={info}
      xConnected={!!user.data_deletion?.x_connected}
      writingDeletionAt={user.data_deletion?.status === "scheduled" ? user.data_deletion.purge_at : null}
      onConfirm={requestDeletion}
      onClose={close}
      zIndex={dialogZIndex}
    />
  );
  return (
    <div style={style}>
      <p style={deleteTextStyle}>
        {linkSent
          ? `Check your email: Loore sent a confirmation link to ${user.email}. `
            + "Nothing changes until you open it and confirm there. "
            + `The link works for ${minutes} minutes.`
          : "Deletes your account at once and signs you out everywhere. If "
            + "you change your mind, you can still restore it by signing in "
            + `within ${days} days; after that it is deleted forever, with `
            + "everything in it."}
      </p>
      {!linkSent && (info.refusal ? (
        <p style={deleteTextStyle}>{info.refusal.message}</p>
      ) : (
        <button type="button" onClick={() => setOpen(true)} style={deleteButtonStyle}>
          Delete my account…
        </button>
      ))}
      {dialogZIndex && open ? createPortal(dialog, document.body) : dialog}
    </div>
  );
}

const deleteTextStyle = {
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.82rem",
  lineHeight: 1.6, color: "var(--text-muted)", marginBottom: "0.8rem",
};

const deleteButtonStyle = {
  padding: "8px 16px", borderRadius: "6px", border: "1px solid var(--border)",
  background: "none", color: "var(--error)", cursor: "pointer",
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem",
};
