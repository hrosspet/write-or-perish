import React from 'react';

// Another node is loading while the current page stays (NodeDetailWrapper,
// useNodePrefetch): a thin accent ring, turning, on a small disc in the
// middle of the screen. Clicks pass through, so another node can still be
// picked (the newest one opens). The iOS app's SpinnerRing on the same disc.
const RING_R = 8;
const RING_C = 2 * Math.PI * RING_R;

export default function NodeOpeningSpinner() {
  return (
    <div
      role="status"
      aria-label="Loading node"
      style={{
        position: "fixed",
        top: "50%",
        left: "50%",
        transform: "translate(-50%, -50%)",
        zIndex: 50,
        padding: "11px",
        lineHeight: 0,
        borderRadius: "50%",
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        pointerEvents: "none",
        animation: "nodeOpeningFadeIn 0.12s ease-out",
      }}
    >
      <style>{`@keyframes nodeOpeningFadeIn { from { opacity: 0; } }`}</style>
      <svg width="18" height="18" viewBox="0 0 18 18" style={{ animation: "spin 0.9s linear infinite" }}>
        <circle
          cx="9" cy="9" r={RING_R}
          fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round"
          strokeDasharray={`${0.72 * RING_C} ${RING_C}`}
        />
      </svg>
    </div>
  );
}
