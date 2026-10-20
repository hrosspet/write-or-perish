import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';

const DEFAULT_MESSAGE =
  "You've reached your monthly usage limit for the free alpha. " +
  "It resets at the start of next month.";

/**
 * Fixed top banner shown when a cost-incurring action is refused because the
 * user hit their monthly spend cap (HTTP 402, issue #85 follow-up). Driven by
 * the global "loore:spend-capped" event dispatched from the api.js response
 * interceptor, so every cost action (LLM, TTS, profile generation) surfaces it
 * uniformly. Persists until dismissed — the cap holds for the rest of the month.
 */
export default function SpendCapBanner() {
  const [message, setMessage] = useState(null);
  const ref = useRef(null);

  useEffect(() => {
    const onCapped = (e) =>
      setMessage((e.detail && e.detail.message) || DEFAULT_MESSAGE);
    window.addEventListener('loore:spend-capped', onCapped);
    return () => window.removeEventListener('loore:spend-capped', onCapped);
  }, []);

  // Toasts share this bottom-center spot. Publish the banner's height so the
  // toast stack sits above it instead of on top of it (#341: a refused
  // record or upload press shows a toast while the banner is up).
  useLayoutEffect(() => {
    const root = document.documentElement;
    const h = message && ref.current ? ref.current.offsetHeight : 0;
    root.style.setProperty('--spendcap-banner-offset', h ? `${h + 8}px` : '0px');
    return () => root.style.setProperty('--spendcap-banner-offset', '0px');
  }, [message]);

  if (!message) return null;

  return (
    <div ref={ref} style={{
      position: 'fixed',
      // Bottom-center, sitting above the mobile floating audio player when
      // present (GlobalAudioPlayer publishes its height as
      // --floating-player-offset; 0px when absent) — matches the toast
      // placement so the player is never occluded. #28.
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
        animation: 'spendcap-in 0.25s ease-out',
      }}>
        <span style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.7rem',
          letterSpacing: '0.12em',
          textTransform: 'uppercase',
          color: 'var(--accent)',
          whiteSpace: 'nowrap',
        }}>Limit reached</span>
        <span style={{
          flex: 1,
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          lineHeight: 1.5,
          color: 'var(--text-secondary)',
        }}>{message}</span>
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
      <style>{`
        @keyframes spendcap-in {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
      `}</style>
    </div>
  );
}
