import React, { useEffect, useState } from "react";
import api from "../api";

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

// What each count of the dry run means, in the order shown. Keys the
// backend adds later are listed after these under their own name.
export const PURGE_COUNT_LABELS = [
  ["node", "Entries and AI replies deleted"],
  ["node_tombstoned", "Entries kept as empty placeholders (other people replied below)"],
  ["others_replies_kept", "Other people's replies kept"],
  ["node_version", "Edit-history versions"],
  ["node_transcript_chunk", "Transcript chunks"],
  ["tts_chunk", "Speech audio chunks"],
  ["thread", "Thread names"],
  ["node_context_artifact", "Session context links"],
  ["node_embedding", "Search embeddings of entries"],
  ["draft", "Drafts"],
  ["share_draft", "Shares"],
  ["user_profile", "Profile versions"],
  ["user_recent_context", "Recent-context summaries"],
  ["user_todo", "Todo list versions"],
  ["user_artifact", "Document versions (memory, intentions, ...)"],
  ["artifact_view", "Document views"],
  ["user_prompt", "Prompt versions"],
  ["user_feedback", "Feedback sent"],
  ["user_notification", "Notifications"],
  ["poll_response", "Poll answers"],
  ["external_item", "Saved references"],
  ["external_item_embedding", "Search embeddings of references"],
  ["feed_pick", "Read picks and quotes"],
  ["feed_render", "Read renders"],
  ["reference_action", "Reference opens, marks and verdicts"],
  ["external_account", "X connection (deleted, not revoked at X)"],
  ["files", "Files on disk (audio, imports, X dumps)"],
  ["api_cost_log", "Cost rows (kept, moved to loore-erased without the name)"],
  ["node.linked_node_id", "Other users' links to these entries (cleared)"],
  ["node.continuation_node_id", "Other users' continuations of these entries (cleared)"],
  ["draft.node_id", "Other users' drafts editing these entries (cleared)"],
  ["draft.parent_id", "Other users' drafts replying to these entries (cleared)"],
  ["draft.llm_node_id", "Other users' drafts waiting on these replies (cleared)"],
  ["share_draft.source_node_id", "Other users' shares from these entries (cleared)"],
  ["share_draft.public_node_id", "Other users' published shares on these entries (cleared)"],
  ["celery_tasks_revoked", "Queued tasks revoked"],
  ["provider_batches_cancelled", "Provider batches cancelled"],
  ["profile_batch_job", "Profile batch jobs changed"],
  ["poll_draft_batch_job", "Poll-draft batch jobs changed"],
  ["external_digest_batch_job", "Digest batch jobs changed"],
  ["recent_context_batch_job", "Recent-context batch jobs changed"],
];

export function purgeCountRows(counts) {
  if (!counts) return [];
  const known = new Set(PURGE_COUNT_LABELS.map(([k]) => k));
  const rows = PURGE_COUNT_LABELS
    .filter(([k]) => counts[k])
    .map(([k, label]) => [k, label, counts[k]]);
  Object.keys(counts).sort().forEach((k) => {
    if (!known.has(k) && counts[k]) rows.push([k, k, counts[k]]);
  });
  return rows;
}

/**
 * Admin "Purge data" (#268): delete all of one user's data at once. Opens
 * with a dry run, so the admin sees what goes, what stays and whose rows
 * change before anything happens; the purge needs the username typed.
 * Counts only, never content. AI and system accounts are refused by the
 * backend, and the refusal is shown here.
 */
function PurgeDataDialog({ user, onClose, onStarted }) {
  const [state, setState] = useState({ loading: true });
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!user) return undefined;
    let alive = true;
    setTyped("");
    setState({ loading: true });
    api.post(`/admin/users/${user.id}/purge_data?dry_run=1`)
      .then((res) => alive && setState({ counts: res.data.counts, job: res.data.job }))
      .catch((e) => alive && setState({
        error: e.response?.data?.error || "Could not count this user's data.",
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
  const rows = purgeCountRows(state.counts);

  const purge = async () => {
    if (!matches || busy) return;
    setBusy(true);
    try {
      const res = await api.post(`/admin/users/${user.id}/purge_data`,
        { confirm_username: typed.trim() });
      setState((s) => ({ ...s, started: res.data.job }));
      if (onStarted) onStarted(res.data.job);
    } catch (e) {
      setState((s) => ({ ...s, error: e.response?.data?.error || "The purge did not start." }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={cardStyle} role="dialog" aria-modal="true">
        <h2 style={titleStyle}>Purge all data of @{user.username}?</h2>
        <p style={bodyStyle}>
          Deletes everything this person wrote or recorded, the AI replies
          they asked for, their profile, documents, saved references, imports
          and files, at once (no grace period). The account, its login and
          settings stay. This cannot be undone.
        </p>
        {state.loading && <p style={bodyStyle}>Counting…</p>}
        {state.error && <p style={{ ...bodyStyle, color: "var(--error)" }}>{state.error}</p>}
        {!state.loading && !state.counts && (
          <button onClick={onClose} style={{ ...buttonBaseStyle, cursor: "pointer", color: "var(--text-secondary)" }}>
            Close
          </button>
        )}
        {state.job && state.job.status === "scheduled" && (
          <p style={bodyStyle}>
            The user asked for this deletion; it is due on {new Date(state.job.scheduled_for).toLocaleDateString()}.
            Purging now brings it forward.
          </p>
        )}
        {state.counts && (
          <table style={{ ...bodyStyle, width: "100%", borderCollapse: "collapse" }}>
            <tbody>
              {rows.length === 0 && (
                <tr><td>Nothing to delete.</td></tr>
              )}
              {rows.map(([key, label, n]) => (
                <tr key={key}>
                  <td style={{ padding: "2px 8px 2px 0" }}>{label}</td>
                  <td style={{ padding: "2px 0", textAlign: "right", whiteSpace: "nowrap" }}>{n}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {state.started ? (
          <>
            <p style={bodyStyle}>
              Purge started (job {state.started.id}, {state.started.status}). It runs in the
              background; the Users table shows when it is done.
            </p>
            <button onClick={onClose} style={{ ...buttonBaseStyle, cursor: "pointer", color: "var(--text-secondary)" }}>
              Close
            </button>
          </>
        ) : state.counts && (
          <>
            <label htmlFor="purge-confirm" style={{ ...bodyStyle, display: "block", marginBottom: "6px" }}>
              Type the username, <strong>{user.username}</strong>, to confirm.
            </label>
            <input
              id="purge-confirm"
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
                onClick={purge}
                disabled={!matches || busy}
                style={{
                  ...buttonBaseStyle,
                  color: "var(--error)",
                  cursor: matches && !busy ? "pointer" : "default",
                  opacity: matches && !busy ? 1 : 0.45,
                }}
              >
                {busy ? "Starting…" : "Purge now"}
              </button>
              <button onClick={onClose} style={{ ...buttonBaseStyle, cursor: "pointer", color: "var(--text-secondary)" }}>
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default PurgeDataDialog;
