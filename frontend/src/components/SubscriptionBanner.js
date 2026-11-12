import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { SUBSCRIPTION_REQUIRED_EVENT } from '../utils/subscription';

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

  useEffect(() => {
    const onRequired = (e) => setMessage(
      (e.detail && e.detail.message)
      || "Loore doesn't reply on an account without a subscription.");
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
            onClick={() => setMessage(null)}
            style={{ color: 'var(--accent)', whiteSpace: 'nowrap' }}
          >
            Settings
          </Link>
        </span>
        <button
          onClick={() => setMessage(null)}
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
