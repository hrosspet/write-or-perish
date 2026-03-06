import React from 'react';

/**
 * Recovery banner shown when an interrupted voice recording is detected.
 *
 * @param {Object} props
 * @param {Object} props.draft - The interrupted draft object
 * @param {Function} props.onContinue - Called when user clicks "Continue recording"
 * @param {Function} props.onDiscard - Called when user clicks "Discard"
 * @param {React.ReactNode} props.children - Optional content to render above the banner (e.g. animation)
 */
export default function RecoveryBanner({ draft, onContinue, onDiscard, children }) {
  const labelText = draft.label || 'Voice';

  return (
    <>
      {children}

      <p style={{
        fontFamily: 'var(--serif)',
        fontStyle: 'italic',
        fontSize: 'clamp(1rem, 2vw, 1.3rem)',
        fontWeight: 300,
        color: 'var(--text-secondary)',
        marginBottom: '32px',
        textAlign: 'center',
      }}>
        {`Unfinished ${labelText} recording`}
      </p>

      <div style={{ display: 'flex', gap: '16px' }}>
        <button
          onClick={onContinue}
          style={{
            padding: '10px 24px',
            background: 'transparent',
            border: '1px solid var(--accent)',
            borderRadius: '6px',
            color: 'var(--accent)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            cursor: 'pointer',
            transition: 'all 0.2s',
          }}
        >
          Continue recording
        </button>
        <button
          onClick={onDiscard}
          style={{
            padding: '10px 24px',
            background: 'transparent',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            cursor: 'pointer',
            transition: 'all 0.2s',
          }}
        >
          Discard
        </button>
      </div>
    </>
  );
}
