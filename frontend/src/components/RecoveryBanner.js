import React from 'react';

function PulsingDot({ color = 'var(--accent)' }) {
  return (
    <span style={{
      display: 'inline-block',
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      background: color,
      animation: 'recoveryPulseDot 1.5s ease-in-out infinite',
    }}>
      <style>{`
        @keyframes recoveryPulseDot {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </span>
  );
}

/**
 * Recovery banner shown when an interrupted voice recording is detected.
 *
 * @param {Object} props
 * @param {Object} props.draft - The interrupted draft object
 * @param {string|null} props.recoveryState - null | 'transcribing' | 'done'
 * @param {Function} props.onRecover - Called when user clicks "Recover transcript"
 * @param {Function} props.onDiscard - Called when user clicks "Discard" or "Done"
 * @param {React.ReactNode} props.children - Optional content to render above the banner (e.g. animation)
 */
export default function RecoveryBanner({ draft, recoveryState, onRecover, onDiscard, children }) {
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
        marginBottom: '24px',
        textAlign: 'center',
      }}>
        {recoveryState === 'transcribing' ? 'Recovering your recording...' :
         recoveryState === 'done' ? 'Recording recovered' :
         `Unfinished ${labelText} recording`}
      </p>

      {recoveryState === 'done' && draft.content && (
        <div style={{
          maxWidth: '400px',
          width: '100%',
          padding: '16px',
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: '8px',
          marginBottom: '24px',
          maxHeight: '200px',
          overflow: 'auto',
        }}>
          <p style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            color: 'var(--text-secondary)',
            lineHeight: 1.6,
            margin: 0,
            whiteSpace: 'pre-wrap',
          }}>
            {draft.content}
          </p>
        </div>
      )}

      {recoveryState === 'transcribing' && <PulsingDot />}

      {!recoveryState && (
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.8rem',
          color: 'var(--text-muted)',
          marginBottom: '32px',
        }}>
          {draft.chunk_count} audio chunk{draft.chunk_count !== 1 ? 's' : ''} saved
        </p>
      )}

      <div style={{ display: 'flex', gap: '16px' }}>
        {!recoveryState && (
          <>
            <button
              onClick={onRecover}
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
              Recover transcript
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
          </>
        )}
        {recoveryState === 'done' && (
          <button
            onClick={onDiscard}
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
            Done
          </button>
        )}
      </div>
    </>
  );
}
