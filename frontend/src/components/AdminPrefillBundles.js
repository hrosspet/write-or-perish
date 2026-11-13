import React, { useCallback, useEffect, useState } from "react";
import api from "../api";

// "Make ready from prepared material" (#698): load a pre-fill bundle the
// lab prepared (public tweets + the profile drafted from them) into the
// account with the bundle's X id, or into a new account under its
// handle. Metadata only: counts, dates, states. Hidden while bundles are
// switched off on the server.

const POLL_MS = 2000;

// What a load's error code means, for the admin.
const ERRORS = {
  checksum: "the bundle's checksum does not match its manifest",
  no_bundle: "the bundle is gone from storage",
  excluded: "the X id is on the exclusion list",
  prefill_declined: "the person said no to starting from their tweets",
  ai_opt_out: "the account is set to no AI",
  x_id_mismatch: "the account's X id is not the bundle's",
  deleted: "the account is deleted or being deleted",
  no_account: "the account no longer exists",
  stalled: "stopped: three runs in a row ended without progress",
  format: "a bundle format this server does not load",
  foreign_author: "an entry in the bundle is by another account",
  own_writing: "the account has writing of its own",
  signed_in: "someone has signed in to the account",
};

const n = (v) => (v == null ? "?" : Number(v).toLocaleString());
const day = (iso) => (iso ? iso.slice(0, 10) : "?");

export function bundleCheckLabel(c) {
  if (!c || !c.bundle) return null;
  const b = c.bundle;
  const k = b.counts || {};
  const parts = [
    `@${b.handle} · X id ${c.x_id}`,
    `exported ${day(b.exported_at)}`,
    `entries up to ${day(b.newest_entry_at)}`,
    `${n(k.nodes)} entries`,
    `${n(k.profiles)} profile versions${b.profile_generated_by ? ` (${b.profile_generated_by})` : ""}`,
  ];
  if (k.intentions) parts.push(`${n(k.intentions)} intentions`);
  const a = c.account;
  if (a && !a.exists) {
    parts.push(a.username_taken_by_user_id
      ? `new account (@${a.username} is taken by user ${a.username_taken_by_user_id}: a free variant)`
      : `new account @${a.username}`);
  } else if (a) {
    parts.push(`into @${a.username} (${a.sign_in === "never" ? "never signed in" : "signed in"}` +
      `${a.own_writing ? ", has own writing" : ""}${a.approved ? ", let in" : ""})`);
  }
  return parts.join(" · ");
}

export function bundleLoadLabel(l) {
  if (!l) return null;
  if (l.status === "queued") return "Queued…";
  if (l.status === "running") {
    if (l.stage === "links") return `Linking replies ${n(l.links_done)} / ${n(l.nodes_total)}`;
    if (l.stage === "nodes") return `Loading entries ${n(l.nodes_done)} / ${n(l.nodes_total)}`;
    return "Preparing…";
  }
  if (l.status === "done") {
    return `Done: @${l.username} — ${n(l.created)} entries` +
      `${l.skipped ? ` (${n(l.skipped)} already there)` : ""}, ` +
      `${n(l.profiles)} profile versions, ${n(l.artifacts)} artifacts, ` +
      `${n(l.relinked)} linked; ${l.approved ? "let in" : "not let in yet"}`;
  }
  if (l.status === "failed") return `Failed: ${ERRORS[l.error] || l.error}`;
  if (l.status === "refused") return `Not loaded on their Yes: ${ERRORS[l.error] || l.error}`;
  return l.status;
}

export default function AdminPrefillBundles({ onLoaded }) {
  const [enabled, setEnabled] = useState(false);
  const [query, setQuery] = useState("");
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [approve, setApprove] = useState(true);
  const [force, setForce] = useState(false);
  const [load, setLoad] = useState(null);
  const [recent, setRecent] = useState([]);

  const fetchRecent = useCallback(async () => {
    try {
      const res = await api.get("/admin/prefill-bundles/loads");
      setRecent(res?.data?.loads || []);
    } catch (e) {
      console.warn("Could not list bundle loads", e);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get("/admin/prefill-bundles/config");
        if (!cancelled && res?.data?.enabled) {
          setEnabled(true);
          fetchRecent();
        }
      } catch (e) {
        // Switched off or not deployed: the section stays hidden.
      }
    })();
    return () => { cancelled = true; };
  }, [fetchRecent]);

  useEffect(() => {
    if (!load || !["queued", "running"].includes(load.status)) return undefined;
    const timer = setInterval(async () => {
      try {
        const res = await api.get(`/admin/prefill-bundles/loads/${load.id}`);
        setLoad(res.data.load);
        if (!["queued", "running"].includes(res.data.load.status)) {
          fetchRecent();
          if (res.data.load.status === "done" && onLoaded) onLoaded();
        }
      } catch (e) {
        console.warn("Load status check failed", e);
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [load, fetchRecent, onLoaded]);

  if (!enabled) return null;

  const runCheck = async () => {
    setChecking(true);
    setError("");
    setCheck(null);
    setForce(false);
    try {
      const res = await api.get("/admin/prefill-bundles/check", { params: { q: query.trim() } });
      setCheck(res.data);
    } catch (e) {
      const data = e.response?.data || {};
      setCheck(data.bundle ? data : null);
      setError(data.error || "Check failed.");
    } finally {
      setChecking(false);
    }
  };

  const start = async () => {
    setError("");
    try {
      const res = await api.post("/admin/prefill-bundles/loads",
        { q: query.trim(), force, approve });
      setLoad(res.data.load);
      fetchRecent();
    } catch (e) {
      setError(e.response?.data?.error || "Could not start the load.");
    }
  };

  const busy = load && ["queued", "running"].includes(load.status);
  const refusal = check?.refusal;

  return (
    <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid var(--border)" }}>
      <h2>Make ready from prepared material</h2>
      <p style={{ fontSize: "0.85em", color: "var(--text-secondary)", marginTop: 0 }}>
        Loads the lab's bundle for an X account (public tweets and the profile drafted from them)
        into the account with that X id, or a new one under its handle. No model runs.
      </p>
      <input
        type="text"
        value={query}
        onChange={(e) => { setQuery(e.target.value); setCheck(null); }}
        onKeyDown={(e) => { if (e.key === "Enter") runCheck(); }}
        placeholder="Handle or X id"
        aria-label="Handle or X id"
        style={{ padding: "8px", marginRight: "10px" }}
        disabled={busy}
      />
      <button onClick={runCheck} disabled={checking || busy || !query.trim()}>
        {checking ? "Checking…" : "Check"}
      </button>{" "}
      <label style={{ fontSize: "0.85em", whiteSpace: "nowrap" }}>
        <input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)} />{" "}
        Let them in when it is ready
      </label>{" "}
      {refusal && refusal.forceable && (
        <label style={{ fontSize: "0.85em", whiteSpace: "nowrap" }}>
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />{" "}
          Load anyway ({ERRORS[refusal.code] || refusal.code})
        </label>
      )}{" "}
      <button
        onClick={start}
        disabled={busy || !check || !!check.error || (refusal && !(refusal.forceable && force))}
      >
        Make ready
      </button>
      {check && (
        <div style={{ fontSize: "0.85em", color: "var(--text-secondary)", marginTop: "6px" }}>
          {bundleCheckLabel(check)}
          {refusal && !refusal.forceable && ` — ${refusal.message}`}
        </div>
      )}
      {error && <div style={{ color: "var(--error)", marginTop: "6px" }}>{error}</div>}
      {load && (
        <div style={{
          fontSize: "0.85em", marginTop: "6px",
          color: load.status === "failed" ? "var(--error)" : "var(--text-secondary)",
        }}>
          {bundleLoadLabel(load)}
        </div>
      )}
      {recent.length > 0 && (
        <details style={{ marginTop: "8px", fontSize: "0.85em" }}>
          <summary>Recent loads ({recent.length})</summary>
          <ul style={{ margin: "6px 0", paddingLeft: "18px" }}>
            {recent.map((l) => (
              <li key={l.id}>
                {day(l.created_at)} · {l.origin === "self" ? "their Yes" : "admin"} · @{l.username} (X id {l.x_id}) — {bundleLoadLabel(l)}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
