import React, { useEffect, useState } from "react";
import api from "../api";
import { formatDay, formatPrice } from "../utils/subscription";

// The invite on the Account page (#439). It sits here and nowhere else: no
// reminders, no counts, no names (Peter, 2026-10-02/09). One personal
// invite a month (Peter, 2026-11-13: "one invite a month, no accumulation
// of invites"): it is made for one person, by X handle or email address,
// and its link works for that person's sign-up only. The open invite shows
// with its link, Copy and Withdraw; nothing lists past ones.

const labelStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 400,
  fontSize: "0.85rem",
  color: "var(--text-secondary)",
  marginBottom: "4px",
};

const textStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.92rem",
  color: "var(--text-primary)",
  lineHeight: 1.6,
};

const helperStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.78rem",
  color: "var(--text-muted)",
  marginTop: "6px",
  lineHeight: 1.6,
};

const buttonStyle = (disabled) => ({
  padding: "8px 16px",
  borderRadius: "6px",
  border: "1px solid var(--accent)",
  background: "none",
  color: "var(--accent)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.85rem",
  whiteSpace: "nowrap",
  cursor: disabled ? "default" : "pointer",
  opacity: disabled ? 0.4 : 1,
});

const inputStyle = {
  flex: 1,
  padding: "10px 12px",
  borderRadius: "6px",
  border: "1px solid var(--border)",
  backgroundColor: "var(--bg-input)",
  color: "var(--text-primary)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.9rem",
  boxSizing: "border-box",
  minWidth: 0,
};

// "1 December": the day the next month's invite comes (UTC).
function dayAndMonth(isoDay) {
  const d = new Date(`${isoDay}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return isoDay;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "long", timeZone: "UTC" });
}

export default function InviteSection() {
  const [state, setState] = useState(null);
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    api.get("/referrals/invite")
      .then((res) => { if (alive) setState(res.data); })
      .catch(() => { if (alive) setError("Could not load your invite. Please try again."); });
    return () => { alive = false; };
  }, []);

  const make = async (e) => {
    e.preventDefault();
    if (!to.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post("/referrals/invite", { to: to.trim() });
      setState(res.data);
      setTo("");
      setCopied(false);
    } catch (ex) {
      setError(ex.response?.data?.error || "Could not make the invite. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const withdraw = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.delete("/referrals/invite");
      setState(res.data);
    } catch (ex) {
      setError("Could not withdraw the invite. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const copy = async (url) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch (ex) {
      setCopied(false);
    }
  };

  const credit = state && state.credit
    ? formatPrice(state.credit.cents, state.credit.currency || "usd")
    : null;
  const invite = state && state.invite;
  const who = invite ? (invite.kind === "x" ? `@${invite.to}` : invite.to) : null;

  return (
    <div id="invite" style={{ marginBottom: "1.25rem", scrollMarginTop: "72px" }}>
      <div style={labelStyle}>Invite a friend</div>
      <div style={textStyle}>
        You can invite one person a month: someone you think Loore is right for.
        {credit ? ` When they make their first payment, you both get ${credit} off your next payment.` : ""}
      </div>
      {invite ? (
        <>
          <div style={{ ...textStyle, marginTop: "10px" }}>
            {`Your invite for ${who} works until ${formatDay(invite.expires_at)}. Send them this link:`}
          </div>
          <div style={{ display: "flex", gap: "8px", marginTop: "6px" }}>
            <input
              type="text"
              readOnly
              value={invite.url}
              aria-label="Your invite link"
              onFocus={(e) => e.target.select()}
              style={inputStyle}
            />
            <button type="button" onClick={() => copy(invite.url)} style={buttonStyle(false)}>
              {copied ? "Copied" : "Copy"}
            </button>
            <button type="button" onClick={withdraw} disabled={busy} style={buttonStyle(busy)}>
              Withdraw
            </button>
          </div>
          <div style={helperStyle}>
            The link works only for them, when they sign up with that {invite.kind === "x" ? "X account" : "email address"}. Withdraw it to invite someone else instead.
          </div>
        </>
      ) : state && state.next_invite_on ? (
        <div style={{ ...textStyle, marginTop: "10px" }}>
          {`You've used this month's invite. A new one is available on ${dayAndMonth(state.next_invite_on)}.`}
        </div>
      ) : state && state.can_invite ? (
        <form onSubmit={make} style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
          <input
            type="text"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="@handle or name@example.com"
            aria-label="Your friend's X handle or email address"
            autoComplete="off"
            style={inputStyle}
          />
          <button type="submit" disabled={busy || !to.trim()} style={buttonStyle(busy || !to.trim())}>
            Make invite
          </button>
        </form>
      ) : state ? (
        <div style={helperStyle}>Invites open once your account is active.</div>
      ) : null}
      {error && <div role="status" style={{ ...helperStyle, color: "var(--error, #c0392b)" }}>{error}</div>}
      <div style={helperStyle}>
        Your friend signs up as anyone does and may wait for approval. You'll get a note when the credit arrives.
      </div>
    </div>
  );
}
