import React, { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import api from "../api";
import useSubmitShortcut from "../hooks/useSubmitShortcut";
import {
  LEVEL_NAMES, formatDay, formatPrice, freeMonthLine, rememberSubscription,
  subscriptionStatusLine,
} from "../utils/subscription";

// The Subscription section of Settings (#438). Rendered only while the
// server has payments on (user.payments_enabled). Checkout and the
// "manage or cancel" page are Stripe's: this section only sends the user
// there and shows what comes back. The iPhone app has no such section
// (it shows the state only, and nothing to buy).

const LIVE = ["trialing", "active", "past_due"];

const LEVEL_NOTES = {
  standard: "Loore with AI replies, Voice mode and your profile kept up to date.",
  supporter: "The same Loore. What you pay above Standard keeps a lower price open for people who can't afford it.",
  // Offered without a code only to alpha testers (#438).
  discounted: "The same Loore, at the price for alpha testers.",
};

// One trial per card (#697): a trial whose card had one on another
// account was cancelled before anything was charged.
export const TRIAL_CARD_REFUSED_TEXT = "This card has already been used for a "
  + "free trial, so there is no trial here. You can still subscribe "
  + "without one: the first payment is due at checkout.";

const RETURN_NOTICES = {
  success: { type: "success", text: "Thank you. Your subscription is starting; this page shows it in a moment." },
  cancelled: { type: "info", text: "Checkout was closed. Nothing was charged." },
};

const labelStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 400,
  fontSize: "0.85rem",
  color: "var(--text-secondary)",
  marginBottom: "4px",
};

const helperStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.78rem",
  color: "var(--text-muted)",
  marginTop: "6px",
  lineHeight: 1.6,
};

const textStyle = {
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.92rem",
  color: "var(--text-primary)",
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

const levelButtonStyle = (disabled) => ({
  flex: "1 1 220px",
  textAlign: "left",
  padding: "14px 16px",
  borderRadius: "8px",
  border: "1px solid var(--border)",
  background: "var(--bg-card)",
  cursor: disabled ? "default" : "pointer",
  opacity: disabled ? 0.5 : 1,
  color: "var(--text-primary)",
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
  fontSize: "0.95rem",
  boxSizing: "border-box",
  minWidth: 0,
};

const noticeColor = {
  success: "var(--accent)",
  info: "var(--text-secondary)",
  error: "var(--error, #c0392b)",
};

export default function SubscriptionSection({ user, setUser }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(() => {
    const kind = new URLSearchParams(location.search).get("subscription");
    return RETURN_NOTICES[kind] || null;
  });
  const [code, setCode] = useState("");
  const codeRef = useRef(null);

  const applyState = useCallback((data) => {
    setState(data);
    if (data && data.subscription) {
      setUser((prev) => {
        if (!prev) return prev;
        const next = { ...prev, subscription: data.subscription };
        rememberSubscription(next);
        return next;
      });
    }
  }, [setUser]);

  // Load the state; back from Checkout, read the subscription from Stripe
  // at once instead of waiting for the webhook, then drop the query.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const sessionId = params.get("session_id");
    const kind = params.get("subscription");
    let cancelled = false;
    const load = kind === "success"
      ? api.post("/billing/sync", sessionId ? { session_id: sessionId } : {})
      : api.get("/billing");
    load
      .then((res) => { if (!cancelled) applyState(res.data); })
      .catch(() => {
        if (cancelled) return;
        api.get("/billing").then((res) => applyState(res.data)).catch(() => {
          setNotice({ type: "error", text: "Could not load your subscription." });
        });
      });
    if (kind) {
      navigate({ pathname: location.pathname, hash: location.hash }, { replace: true });
    }
    return () => { cancelled = true; };
    // Once per visit: the query is dropped right away.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const goTo = async (path, body) => {
    setBusy(true);
    setNotice(null);
    try {
      const res = await api.post(path, body);
      if (res.data && res.data.url) {
        window.location.assign(res.data.url);
        return;
      }
      setBusy(false);
    } catch (e) {
      setBusy(false);
      setNotice({
        type: "error",
        text: (e.response && e.response.data && e.response.data.message)
          || "Something went wrong. Please try again.",
      });
    }
  };

  const subscribe = (level) => goTo("/billing/checkout", { level });
  const applyCode = () => {
    if (!code.trim() || busy) return;
    goTo("/billing/checkout", { code: code.trim() });
  };
  const manage = () => goTo("/billing/portal", {});

  useSubmitShortcut(codeRef, applyCode, !busy && !!code.trim());

  const sub = (state && state.subscription) || user.subscription || {};
  const live = LIVE.includes(sub.status);
  const statusLine = subscriptionStatusLine(sub);
  const levels = (state && state.levels) || [];
  const contact = (state && state.contact_email) || "info@loore.org";
  // The alpha free month (#438): the discounted level is offered without
  // a code, and the free month takes the trial's place.
  const alpha = !!sub.alpha_tester;
  const reduced = levels.find((l) => l.level === "discounted");
  const freeLine = alpha
    ? freeMonthLine(sub, reduced && formatPrice(reduced.amount_cents, reduced.currency))
    : null;
  const freeUntil = formatDay(sub.free_month_until);

  return (
    <div id="subscription" style={{ marginBottom: "1.25rem", scrollMarginTop: "72px" }}>
      <div style={labelStyle}>Subscription</div>
      {notice && (
        <div role="status" style={{ ...helperStyle, color: noticeColor[notice.type], marginTop: 0, marginBottom: "8px" }}>
          {notice.text}
        </div>
      )}

      {/* A live subscription shows first, also on a plan set by hand:
          it renews until it is cancelled. */}
      {live ? (
        <>
          {sub.plan_set_by_hand && (
            <div style={{ ...textStyle, marginBottom: "6px" }}>
              Your account has the paid features already ({user.plan}), so this subscription is not needed for them.
            </div>
          )}
          <div style={textStyle}>{statusLine}</div>
          <div style={{ marginTop: "10px" }}>
            <button type="button" onClick={manage} disabled={busy} style={buttonStyle(busy)}>
              {sub.status === "past_due" ? "Update card or cancel" : "Manage or cancel"}
            </button>
          </div>
          <div style={helperStyle}>
            Switch between Standard and Supporter, change your card, see invoices or cancel on Stripe's page. Moving up is charged for the rest of the month at once; moving down takes effect when the month you paid for ends. A cancelled subscription runs to the end of the period you paid for.
          </div>
        </>
      ) : alpha && sub.free_month_upcoming ? (
        // An alpha tester before the free month (#438): nothing changes
        // until it starts, and nothing can be bought before then.
        <div style={textStyle}>{freeLine}</div>
      ) : sub.plan_set_by_hand ? (
        <div style={textStyle}>
          Your account has the paid features already ({user.plan}). There is nothing to pay.
        </div>
      ) : (
        <>
          {alpha ? (
            <>
              {/* An alpha tester: the free month, then the price without
                  a code. No card until they subscribe. */}
              <div style={{ ...textStyle, marginBottom: "8px" }}>{freeLine}</div>
              <div style={{ ...textStyle, color: "var(--text-secondary)", marginBottom: "10px" }}>
                {sub.free_month_active
                  ? `If you subscribe now, the first payment is on ${freeUntil}, when your free month ends. Until then nothing is charged.`
                  : "The first payment is due at checkout."}
              </div>
            </>
          ) : (
            <>
              {statusLine && <div style={{ ...textStyle, marginBottom: "8px" }}>{statusLine}</div>}
              <div style={{ ...textStyle, color: "var(--text-secondary)", marginBottom: "10px" }}>
                {sub.trial_available && sub.trial_days
                  ? `The first ${sub.trial_days} days are free. Your card is charged when they end, unless you cancel before.`
                  : sub.state === "none" && sub.trial_card_refused
                    ? TRIAL_CARD_REFUSED_TEXT
                  : sub.state === "none"
                    ? "This account's free trial was used, so the first payment is due at checkout."
                    : "Subscribe to keep AI replies, Voice mode and profile updates."}
              </div>
            </>
          )}
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            {levels.map((l) => (
              <button
                key={l.level}
                type="button"
                onClick={() => subscribe(l.level)}
                disabled={busy}
                style={levelButtonStyle(busy)}
              >
                <span style={{ display: "block", fontFamily: "var(--serif)", fontSize: "1.1rem", fontWeight: 400 }}>
                  {LEVEL_NAMES[l.level] || l.level}
                </span>
                <span style={{ display: "block", fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 300, color: "var(--accent)", margin: "2px 0 6px" }}>
                  {formatPrice(l.amount_cents, l.currency)} a month, plus tax where it applies
                </span>
                <span style={{ display: "block", fontFamily: "var(--sans)", fontSize: "0.78rem", fontWeight: 300, color: "var(--text-muted)", lineHeight: 1.5 }}>
                  {LEVEL_NOTES[l.level]}
                </span>
              </button>
            ))}
          </div>
          {!alpha && (
          <>
          <div style={helperStyle}>
            Need a lower price? Write to {contact} for a code. No proof needed.
          </div>
          <form
            onSubmit={(e) => { e.preventDefault(); applyCode(); }}
            style={{ display: "flex", gap: "8px", marginTop: "8px" }}
          >
            <input
              ref={codeRef}
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="Code"
              aria-label="Code"
              autoComplete="off"
              style={inputStyle}
            />
            <button type="submit" disabled={busy || !code.trim()} style={buttonStyle(busy || !code.trim())}>
              Use code
            </button>
          </form>
          </>
          )}
        </>
      )}
    </div>
  );
}
