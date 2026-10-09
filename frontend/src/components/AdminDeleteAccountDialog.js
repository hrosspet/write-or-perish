import React, { useEffect, useState } from "react";
import api from "../api";
import { purgeCountRows } from "./PurgeDataDialog";

const overlayStyle = {
  position: "fixed",
  top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1100,
};

const cardStyle = {
  background: "var(--bg-card)",
  border: "1px solid var(--border)",
  borderRadius: "12px",
  padding: "2rem",
  width: "520px",
  maxWidth: "90vw",
  maxHeight: "90vh",
  overflowY: "auto",
  boxSizing: "border-box",
};

const titleStyle = {
  fontFamily: "var(--serif)",
  fontSize: "1.4rem",
  fontWeight: 400,
  color: "var(--text-primary)",
  margin: 0,
  marginBottom: "1rem",
};

const bodyStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 300,
  color: "var(--text-secondary)",
  lineHeight: 1.6,
  margin: "0 0 0.9rem",
};

const buttonBaseStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.9rem",
  fontWeight: 400,
  padding: "10px 16px",
  borderRadius: "6px",
  textAlign: "left",
  background: "var(--bg-deep)",
  border: "1px solid var(--border)",
};

// The identity layer's counts (#269), after the purge's.
export const IDENTITY_COUNT_LABELS = [
  ["user", "Account row (login, email, X id, settings)"],
  ["node_reattributed", "Placeholders moved to loore-erased"],
  ["username_history", "Former usernames (reserved, then released)"],
  ["api_token", "API tokens"],
  ["changelog_read_state", "Update read marks"],
  ["node.pinned_by", "Pins on other entries (cleared)"],
  ["poll.created_by", "Polls created (creator cleared)"],
];

function CountTable({ rows }) {
  return (
    <table style={{ ...bodyStyle, width: "100%", borderCollapse: "collapse" }}>
      <tbody>
        {rows.map(([key, label, n]) => (
          <tr key={key}>
            <td style={{ padding: "2px 8px 2px 0" }}>{label}</td>
            <td style={{ padding: "2px 0", textAlign: "right", whiteSpace: "nowrap" }}>{n}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Admin "Delete account" (#269): the account and all its data, at once.
 * Opens with a dry run: the blast radius first (public writing, replies
 * to other people's entries), then what the purge and the identity
 * layer delete. The deletion needs the username typed. Counts only,
 * never content. AI and system accounts and the last admin are refused
 * by the backend, and the refusal is shown here.
 */
function AdminDeleteAccountDialog({ user, onClose, onStarted }) {
  const [state, setState] = useState({ loading: true });
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!user) return undefined;
    let alive = true;
    setTyped("");
    setState({ loading: true });
    api.post(`/admin/users/${user.id}/delete_account?dry_run=1`)
      .then((res) => alive && setState({ dry: res.data }))
      .catch((e) => alive && setState({
        error: e.response?.data?.error || "Could not count this account's data.",
      }));
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => {
      alive = false;
      window.removeEventListener("keydown", onKey);
    };
  }, [user, onClose]);

  if (!user) return null;

  const matches = typed.trim() === user.username;
  const dry = state.dry;
  const blast = dry?.blast_radius || {};
  const identityRows = IDENTITY_COUNT_LABELS
    .filter(([k]) => dry?.identity?.[k])
    .map(([k, label]) => [k, label, dry.identity[k]]);

  const remove = async () => {
    if (!matches || busy) return;
    setBusy(true);
    try {
      const res = await api.post(`/admin/users/${user.id}/delete_account`,
        { confirm_username: typed.trim() });
      setState((s) => ({ ...s, started: res.data.job }));
      if (onStarted) onStarted(res.data.job);
    } catch (e) {
      setState((s) => ({ ...s, error: e.response?.data?.error || "The deletion did not start." }));
    } finally {
      setBusy(false);
    }
  };

  const close = (
    <button onClick={onClose} style={{ ...buttonBaseStyle, cursor: "pointer", color: "var(--text-secondary)" }}>
      Close
    </button>
  );

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle} role="dialog" aria-modal="true">
        <h2 style={titleStyle}>Delete the account @{user.username}?</h2>
        <p style={bodyStyle}>
          Deletes the account and everything in it, at once (no grace
          period): its writing and recordings, the AI replies it asked for,
          profile, documents, saved references, imports and files, then the
          login, email, X id and settings. The username stays reserved.
          Cost rows are kept without the name. This cannot be undone.
        </p>
        {state.loading && <p style={bodyStyle}>Counting…</p>}
        {state.error && <p style={{ ...bodyStyle, color: "var(--error)" }}>{state.error}</p>}
        {!state.loading && !dry && close}
        {dry && (
          <>
            <p style={{ ...bodyStyle, color: "var(--text-primary)" }}>
              Seen by others: {blast.public_nodes || 0} public{" "}
              {blast.public_nodes === 1 ? "entry" : "entries"} and{" "}
              {blast.replies_to_others || 0}{" "}
              {blast.replies_to_others === 1 ? "reply" : "replies"} to other
              people's entries.
            </p>
            {dry.job && dry.job.status === "scheduled" && (
              <p style={bodyStyle}>
                {dry.job.delete_account
                  ? "The user asked to delete this account"
                  : "The user asked to delete all their writing"}; it is due on{" "}
                {new Date(dry.job.scheduled_for).toLocaleDateString()}. Deleting now
                brings it forward.
              </p>
            )}
            <CountTable rows={purgeCountRows(dry.counts)} />
            <CountTable rows={identityRows} />
          </>
        )}
        {state.started ? (
          <>
            <p style={bodyStyle}>
              Deletion started (job {state.started.id}, {state.started.status}). The
              account is hidden now; the row leaves the Users table when the job is done.
            </p>
            {close}
          </>
        ) : dry && (
          <>
            <label htmlFor="delete-account-admin-confirm" style={{ ...bodyStyle, display: "block", marginBottom: "6px" }}>
              Type the username, <strong>{user.username}</strong>, to confirm.
            </label>
            <input
              id="delete-account-admin-confirm"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              style={{
                width: "100%", padding: "8px 10px", marginBottom: "1rem",
                borderRadius: "6px", border: "1px solid var(--border)",
                background: "var(--bg-input)", color: "var(--text-primary)",
                boxSizing: "border-box",
              }}
            />
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              <button
                onClick={remove}
                disabled={!matches || busy}
                style={{
                  ...buttonBaseStyle,
                  color: "var(--error)",
                  cursor: matches && !busy ? "pointer" : "default",
                  opacity: matches && !busy ? 1 : 0.45,
                }}
              >
                {busy ? "Starting…" : "Delete account now"}
              </button>
              <button onClick={onClose} style={{ ...buttonBaseStyle, cursor: "pointer", color: "var(--text-secondary)" }}>
                Keep the account
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default AdminDeleteAccountDialog;
