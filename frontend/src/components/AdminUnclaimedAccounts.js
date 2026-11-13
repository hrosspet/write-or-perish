import React, { useState, useEffect, useCallback } from "react";
import api from "../api";
import { formatDate } from "../utils/date";

// The pre-filled accounts nobody claimed (#685; Peter, 2026-11-13): an
// account Loore opened and pre-filled from a person's public posts, that
// nobody has signed in to, is deleted with everything in it the period
// after it was opened. The box lists what the nightly run deletes (or,
// while it is a dry run, would delete), what waits for its date and what
// is excluded, says for each why and from which date the period counts,
// and lets the admin keep accounts longer: one, a selection, or every
// upcoming one (people are invited in waves).

const cell = { border: "1px solid var(--border)", padding: "4px 8px" };
const small = { padding: "2px 8px", fontSize: "0.85em", borderRadius: "4px" };

const day = (iso) => formatDate(iso, { relative: false, fallback: "—" });

const UPCOMING = ["due", "waiting"];

export function statusLabel(a, { dryRun, tonight }) {
  if (a.status === "due") {
    if (!tonight) return "due; a later night (the nightly limit)";
    return dryRun ? "would be deleted tonight (dry run)" : "deleted tonight";
  }
  if (a.status === "waiting") return "waiting";
  return "never deleted";
}

const askUntil = (what, current = "") => window.prompt(
  `Keep ${what} until which day (YYYY-MM-DD), or "forever"? Leave it empty to clear the Keep.`,
  current,
);

export default function AdminUnclaimedAccounts() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(() => new Set());

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

  const keepOne = async (a) => {
    const current = a.kept_for_good ? "forever" : (a.keep_until || "").slice(0, 10);
    const answer = askUntil(`@${a.username}`, current);
    if (answer === null) return;
    try {
      await api.put(`/admin/users/${a.user_id}/unclaimed_keep`, { until: answer.trim() || null });
      load();
    } catch (err) {
      setError(err.response?.data?.error || "Could not save the Keep.");
    }
  };

  const keepSelected = async () => {
    const ids = [...selected];
    if (!ids.length) return;
    const answer = askUntil(`the ${ids.length} selected account${ids.length === 1 ? "" : "s"}`);
    if (answer === null) return;
    try {
      await api.post("/admin/unclaimed_accounts/keep", { user_ids: ids, until: answer.trim() || null });
      setSelected(new Set());
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
  const upcomingIds = accounts.filter((a) => UPCOMING.includes(a.status)).map((a) => a.user_id);
  const allUpcoming = upcomingIds.length > 0 && upcomingIds.every((id) => selected.has(id));

  const toggle = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };
  const toggleAllUpcoming = () => setSelected(allUpcoming ? new Set() : new Set(upcomingIds));

  return (
    <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid var(--border)" }}>
      <h2 style={{ marginTop: 0 }}>Unclaimed pre-filled accounts</h2>
      <div style={{ color: "var(--text-secondary)", fontSize: "0.9em" }}>
        Deleted with everything in them {data.period_days} days after they were pre-filled,
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
        <>
          <div style={{ marginTop: "8px", display: "flex", gap: "12px", alignItems: "center" }}>
            <label>
              <input
                type="checkbox"
                checked={allUpcoming}
                onChange={toggleAllUpcoming}
                disabled={!upcomingIds.length}
              />{" "}
              Select all upcoming ({upcomingIds.length})
            </label>
            <button style={small} onClick={keepSelected} disabled={!selected.size}>
              Keep selected ({selected.size})…
            </button>
          </div>
          <table style={{ marginTop: "8px", borderCollapse: "collapse", fontSize: "0.9em" }}>
            <thead>
              <tr>
                <th style={cell}></th>
                <th style={cell}>Account</th>
                <th style={cell}>Counted from</th>
                <th style={cell}>Deleted on</th>
                <th style={cell}>Status</th>
                <th style={cell}>Why</th>
                <th style={cell}></th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => {
                const keepable = a.status !== "kept" || a.kept_for_good;
                return (
                  <tr key={a.user_id}>
                    <td style={cell}>
                      {keepable && (
                        <input
                          type="checkbox"
                          aria-label={`select @${a.username}`}
                          checked={selected.has(a.user_id)}
                          onChange={() => toggle(a.user_id)}
                        />
                      )}
                    </td>
                    <td style={cell}>@{a.username} ({a.user_id})</td>
                    <td style={cell} title={a.counted_from || undefined}>
                      {day(a.opened_at)}{a.counted_from ? ` (${a.counted_from})` : ""}
                    </td>
                    <td style={cell}>{a.kept_for_good ? "kept for good" : day(a.delete_on)}</td>
                    <td style={cell}>{statusLabel(a, { dryRun, tonight: tonightIds.has(a.user_id) })}</td>
                    <td style={cell}>{a.why}</td>
                    <td style={cell}>
                      {keepable && (
                        <button
                          style={small}
                          onClick={() => keepOne(a)}
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
        </>
      )}
    </div>
  );
}
