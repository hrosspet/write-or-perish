import React, { useState, useEffect, useCallback } from "react";
import api from "../api";
import { formatDate } from "../utils/date";

// The pre-filled accounts nobody claimed (#685; Peter, 2026-11-13): an
// account Loore opened and pre-filled from a person's public posts, that
// nobody has signed in to, is deleted with everything in it the period
// after it was opened. The box lists what the nightly run deletes (or,
// while it is a dry run, would delete), what waits for its date and what
// is excluded, and lets the admin keep one account longer.

const cell = { border: "1px solid var(--border)", padding: "4px 8px" };
const small = { padding: "2px 8px", fontSize: "0.85em", borderRadius: "4px" };

const day = (iso) => formatDate(iso, { relative: false, fallback: "—" });

export function statusLabel(a, { dryRun, tonight }) {
  if (a.status === "due") {
    if (!tonight) return "due; a later night (the nightly limit)";
    return dryRun ? "would be deleted tonight (dry run)" : "deleted tonight";
  }
  if (a.status === "waiting") {
    return a.reasons.length ? `waiting: ${a.reasons.join("; ")}` : "waiting";
  }
  return `never deleted: ${a.reasons.join("; ")}`;
}

export default function AdminUnclaimedAccounts() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/admin/unclaimed_accounts");
      setData(res.data);
      setError("");
    } catch (err) {
      setError(err.response?.data?.error || "Could not load the unclaimed pre-filled accounts.");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const keep = async (a) => {
    const current = a.kept_for_good ? "forever" : (a.keep_until || "").slice(0, 10);
    const answer = window.prompt(
      `Keep @${a.username} until which day (YYYY-MM-DD), or "forever"? Leave it empty to clear the Keep.`,
      current,
    );
    if (answer === null) return;
    try {
      await api.put(`/admin/users/${a.user_id}/unclaimed_keep`, { until: answer.trim() || null });
      load();
    } catch (err) {
      setError(err.response?.data?.error || "Could not save the Keep.");
    }
  };

  if (!data) {
    return error ? <div style={{ color: "var(--error)", marginBottom: "20px" }}>{error}</div> : null;
  }

  const dryRun = !data.deletion_enabled;
  const accounts = data.accounts || [];
  const count = (s) => accounts.filter((a) => a.status === s).length;
  const due = accounts.filter((a) => a.status === "due");
  const tonightIds = new Set(due.slice(0, data.max_per_run).map((a) => a.user_id));

  return (
    <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid var(--border)" }}>
      <h2 style={{ marginTop: 0 }}>Unclaimed pre-filled accounts</h2>
      <div style={{ color: "var(--text-secondary)", fontSize: "0.9em" }}>
        Deleted with everything in them {data.period_days} days after they were opened,
        unless someone signs in.{" "}
        <strong>{dryRun ? "Dry run: nothing is deleted." : `On: at most ${data.max_per_run} a night.`}</strong>
      </div>
      <div style={{ marginTop: "6px" }}>
        Due now: {count("due")} · waiting: {count("waiting")} · excluded: {count("kept")} ·
        {" "}someone used it: {data.used_count} · opened before {day(data.record_since)} (never
        deleted automatically): {data.before_record?.count || 0}{" "}
        {accounts.length > 0 && (
          <button style={small} onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"}</button>
        )}
      </div>
      {error && <div style={{ color: "var(--error)" }}>{error}</div>}
      {open && (
        <table style={{ marginTop: "8px", borderCollapse: "collapse", fontSize: "0.9em" }}>
          <thead>
            <tr>
              <th style={cell}>Account</th>
              <th style={cell}>Opened</th>
              <th style={cell}>Deleted on</th>
              <th style={cell}>Status</th>
              <th style={cell}></th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((a) => {
              const keepable = a.status !== "kept" || a.kept_for_good;
              return (
                <tr key={a.user_id}>
                  <td style={cell}>@{a.username} ({a.user_id})</td>
                  <td style={cell}>{day(a.opened_at)}</td>
                  <td style={cell}>{a.kept_for_good ? "kept for good" : day(a.delete_on)}</td>
                  <td style={cell}>{statusLabel(a, { dryRun, tonight: tonightIds.has(a.user_id) })}</td>
                  <td style={cell}>
                    {keepable && (
                      <button
                        style={small}
                        onClick={() => keep(a)}
                        title="Keep this account past its date, e.g. when someone was promised more time"
                      >
                        Keep…
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
