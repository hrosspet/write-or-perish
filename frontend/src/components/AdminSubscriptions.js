import React, { useEffect, useState } from "react";
import api from "../api";
import { formatDate } from "../utils/date";

// Admin: subscriptions and discount codes (#438). The tab exists only
// while the server has payments on. Subscriptions show what Loore keeps
// (level, state, dates); payment details stay at Stripe.

const cell = { border: "1px solid var(--border)", padding: "6px 8px", whiteSpace: "nowrap", fontSize: "0.9em" };
const th = { ...cell, textAlign: "left", color: "var(--text-secondary)", fontWeight: 400 };
const inputStyle = {
  padding: "8px 10px",
  borderRadius: "6px",
  border: "1px solid var(--border)",
  backgroundColor: "var(--bg-input)",
  color: "var(--text-primary)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.9rem",
};
const buttonStyle = {
  padding: "8px 14px",
  borderRadius: "6px",
  border: "1px solid var(--accent)",
  background: "none",
  color: "var(--accent)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.85rem",
  cursor: "pointer",
};
const h2 = { fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.3rem", margin: "24px 0 12px" };

const day = (iso) => (iso ? formatDate(iso) : "—");

export default function AdminSubscriptions() {
  const [subs, setSubs] = useState(null);
  const [codes, setCodes] = useState(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({ code: "", max_uses: "", expires_at: "", note: "" });
  const [saving, setSaving] = useState(false);

  const loadCodes = () => api.get("/admin/discount-codes")
    .then((res) => setCodes(res.data.codes))
    .catch(() => setErr("Failed to fetch discount codes."));

  useEffect(() => {
    api.get("/admin/subscriptions")
      .then((res) => setSubs(res.data.subscriptions))
      .catch(() => setErr("Failed to fetch subscriptions."));
    loadCodes();
  }, []);

  const createCode = async (e) => {
    e.preventDefault();
    setSaving(true);
    setErr("");
    try {
      await api.post("/admin/discount-codes", form);
      setForm({ code: "", max_uses: "", expires_at: "", note: "" });
      await loadCodes();
    } catch (ex) {
      setErr(ex.response?.data?.error || "Could not create the code.");
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (c) => {
    try {
      await api.patch(`/admin/discount-codes/${c.id}`, { active: !c.active });
      await loadCodes();
    } catch (ex) {
      setErr("Could not change the code.");
    }
  };

  return (
    <div>
      {err && <div style={{ color: "var(--error, #c0392b)", marginBottom: "12px" }}>{err}</div>}

      <h2 style={h2}>Subscriptions</h2>
      {subs === null ? <div>Loading…</div> : subs.length === 0 ? (
        <div style={{ color: "var(--text-muted)" }}>No subscriptions yet.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={th}>user</th>
                <th style={th}>plan</th>
                <th style={th}>level</th>
                <th style={th}>status</th>
                <th style={th}>trial ends</th>
                <th style={th}>period ends</th>
                <th style={th}>cancelling</th>
                <th style={th}>free month</th>
              </tr>
            </thead>
            <tbody>
              {subs.map((s) => (
                <tr key={s.user_id}>
                  <td style={cell}>{s.username}</td>
                  <td style={cell}>{s.plan}</td>
                  <td style={cell}>{s.level || "—"}</td>
                  <td style={cell}>{s.status}</td>
                  <td style={cell}>{day(s.trial_end)}</td>
                  <td style={cell}>{day(s.current_period_end)}</td>
                  <td style={cell}>{s.cancel_at_period_end ? "yes" : ""}</td>
                  {/* Alpha testers (#438): the free month, the same dates
                      for everyone. */}
                  <td style={cell}>{s.alpha_tester ? `${day(s.free_month_start)} – ${day(s.free_month_until)}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 style={h2}>Discount codes</h2>
      <form onSubmit={createCode} style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center", marginBottom: "12px" }}>
        <input style={inputStyle} placeholder="Code (empty: generated)" aria-label="Code"
          value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
        <input style={{ ...inputStyle, width: "110px" }} placeholder="Max uses" aria-label="Max uses" inputMode="numeric"
          value={form.max_uses} onChange={(e) => setForm({ ...form, max_uses: e.target.value })} />
        <input style={inputStyle} type="date" aria-label="Expires on"
          value={form.expires_at} onChange={(e) => setForm({ ...form, expires_at: e.target.value })} />
        <input style={{ ...inputStyle, minWidth: "200px" }} placeholder="Note" aria-label="Note"
          value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
        <button type="submit" disabled={saving} style={buttonStyle}>Create code</button>
      </form>
      {codes === null ? <div>Loading…</div> : codes.length === 0 ? (
        <div style={{ color: "var(--text-muted)" }}>No codes yet.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={th}>code</th>
                <th style={th}>uses</th>
                <th style={th}>expires</th>
                <th style={th}>note</th>
                <th style={th}>active</th>
              </tr>
            </thead>
            <tbody>
              {codes.map((c) => (
                <tr key={c.id}>
                  <td style={{ ...cell, fontFamily: "monospace" }}>{c.code}</td>
                  <td style={cell}>{c.uses}{c.max_uses ? ` / ${c.max_uses}` : ""}</td>
                  <td style={cell}>{c.expires_on || "never"}</td>
                  <td style={{ ...cell, whiteSpace: "normal" }}>{c.note || ""}</td>
                  <td style={cell}>
                    <button type="button" onClick={() => toggle(c)} style={{ ...buttonStyle, padding: "2px 10px" }}>
                      {c.active ? "on" : "off"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
