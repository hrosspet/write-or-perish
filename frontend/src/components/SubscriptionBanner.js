import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useUser } from '../contexts/UserContext';
import {
  SUBSCRIPTION_REQUIRED_EVENT, formatDay, freeMonthEndsSoon,
} from '../utils/subscription';

// The free month's notice is said once: dismissing it is remembered for
// that end date (a later date, after an admin change, says it again).
const FREE_MONTH_DISMISSED_KEY = 'loore:free-month-notice-dismissed';

/**
 * Payments (#438): shown when a reply was refused because the account has
 * no AI replies (no subscription, or one that ended). The text is the
 * server's and says why; the link goes to the Subscription section in
 * Settings. Raised only by 'loore:subscription-required', which nothing
 * dispatches while payments are off. Same placement and look as
 * SpendCapBanner, so the two never read as different systems.
 */
export default function SubscriptionBanner() {
  const [message, setMessage] = useState(null);
  const userCtx = useUser();
  const sub = userCtx?.user?.payments_enabled ? userCtx.user.subscription : null;
  const pastDue = !!(sub && sub.status === 'past_due' && sub.state === 'lapsed');
  // An alpha tester's free month ends within a week (#438), and they have
  // not subscribed: said once, so the replies don't stop unannounced.
  const freeMonthEnd = sub && freeMonthEndsSoon(sub) ? sub.free_month_until : null;
  const [freeMonthNotice, setFreeMonthNotice] = useState(false);

  useEffect(() => {
    if (!freeMonthEnd) return;
    let dismissed = null;
    try { dismissed = window.localStorage.getItem(FREE_MONTH_DISMISSED_KEY); } catch (e) { /* private mode */ }
    if (dismissed === freeMonthEnd) return;
    setFreeMonthNotice(true);
    setMessage(`Your free month ends on ${formatDay(freeMonthEnd)}. `
      + 'To keep AI replies after it, subscribe in');
  }, [freeMonthEnd]);

  const dismiss = () => {
    if (freeMonthNotice && freeMonthEnd) {
      try { window.localStorage.setItem(FREE_MONTH_DISMISSED_KEY, freeMonthEnd); } catch (e) { /* private mode */ }
      setFreeMonthNotice(false);
    }
    setMessage(null);
  };

  // A failed payment (Peter, 2026-11-12: "we should inform the user"):
  // said on every load until the card is updated.
  useEffect(() => {
    if (pastDue) {
      setMessage("Your last payment didn't go through, so Loore doesn't reply for now. "
        + 'Update your card in');
    }
  }, [pastDue]);

  useEffect(() => {
    const onRequired = (e) => {
      setFreeMonthNotice(false);
      setMessage(
        (e.detail && e.detail.message)
        || "Loore doesn't reply on an account without a subscription.");
    };
    window.addEventListener(SUBSCRIPTION_REQUIRED_EVENT, onRequired);
    return () => window.removeEventListener(SUBSCRIPTION_REQUIRED_EVENT, onRequired);
  }, []);

  if (!message) return null;

  return (
    <div role="status" style={{
      position: 'fixed',
      bottom: 'calc(24px + var(--floating-player-offset, 0px))',
      left: '50%',
      transform: 'translateX(-50%)',
      zIndex: 9999,
      width: '100%',
      maxWidth: 680,
      padding: '0 16px',
      boxSizing: 'border-box',
      pointerEvents: 'none',
    }}>
      <div style={{
        pointerEvents: 'auto',
        width: '100%',
        background: 'var(--bg-card)',
        border: '1px solid var(--accent)',
        borderRadius: 8,
        boxShadow: '0 4px 20px rgba(0,0,0,0.25)',
        padding: '12px 16px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
      }}>
        <span style={{
          flex: 1,
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          lineHeight: 1.5,
          color: 'var(--text-secondary)',
        }}>
          {message}{' '}
          <Link
            to="/account#subscription"
            onClick={dismiss}
            style={{ color: 'var(--accent)', whiteSpace: 'nowrap' }}
          >
            Settings
          </Link>
        </span>
        <button
          onClick={dismiss}
          aria-label="Dismiss"
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--text-muted)',
            fontSize: '1.2rem',
            lineHeight: 1,
            cursor: 'pointer',
            padding: '0 4px',
          }}
        >×</button>
      </div>
    </div>
  );
}
