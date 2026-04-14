import React from 'react';
import { useOnlineStatus } from '../hooks/useOnlineStatus';

/**
 * Renders "You're offline" text when the browser is offline.
 * Renders nothing when online.
 *
 * Props:
 *  - style: optional override styles
 */
export function OfflineBanner({ style }) {
  const isOnline = useOnlineStatus();

  if (isOnline) return null;

  return (
    <p style={{
      fontFamily: 'var(--sans)',
      fontSize: '0.85rem',
      color: 'var(--text-muted)',
      marginBottom: '16px',
      opacity: 0.8,
      ...style,
    }}>
      You're offline
    </p>
  );
}

export default OfflineBanner;
