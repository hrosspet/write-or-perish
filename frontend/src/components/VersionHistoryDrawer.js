import React, { useEffect } from 'react';

/**
 * VersionHistoryDrawer — Slides in from the right.
 *
 * Props:
 *   isOpen, onClose, title,
 *   versions: [{ id, version_number, created_at, generated_by, tokens_used }],
 *   selectedVersionId, onSelectVersion,
 *   versionContent (string|null — the loaded content for selectedVersionId),
 *   onRevert (optional — called with version id)
 */
export default function VersionHistoryDrawer({
  isOpen,
  onClose,
  title = "Version History",
  versions = [],
  selectedVersionId,
  onSelectVersion,
  versionContent,
  onRevert,
}) {
  // Close on Escape
  useEffect(() => {
    if (!isOpen) return;
    const handleKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [isOpen, onClose]);

  const formatDate = (iso) => {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const generatedByLabel = (g, genType) => {
    if (g === 'user' || g === 'manual') return 'Manual edit';
    if (g === 'revert') return 'Reverted';
    if (g === 'orient_session') return 'Orient session';
    if (g === 'import') return 'Imported';
    if (genType === 'update') return `Auto-updated (${g})`;
    if (genType === 'iterative') return `Iterative build (${g})`;
    if (genType === 'integration') return `Integrated profile (${g})`;
    return `Auto-generated (${g})`;
  };

  return (
    <>
      {/* Backdrop */}
      {isOpen && (
        <div
          onClick={onClose}
          style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.4)',
            zIndex: 1100,
          }}
        />
      )}

      {/* Drawer */}
      <div style={{
        position: 'fixed',
        top: 0,
        right: 0,
        bottom: 0,
        width: '420px',
        maxWidth: '90vw',
        background: 'var(--bg-surface)',
        borderLeft: '1px solid var(--border)',
        boxShadow: '-8px 0 32px rgba(0,0,0,0.3)',
        zIndex: 1101,
        transform: isOpen ? 'translateX(0)' : 'translateX(100%)',
        transition: 'transform 0.25s ease',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{
          padding: '20px 24px',
          borderBottom: '1px solid var(--border)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <h3 style={{
            fontFamily: 'var(--serif)',
            fontSize: '1.1rem',
            fontWeight: 400,
            color: 'var(--text-primary)',
            margin: 0,
          }}>
            {title}
          </h3>
          <button
            onClick={onClose}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text-muted)', fontSize: '18px', padding: '4px',
            }}
          >
            &times;
          </button>
        </div>

        {/* Content area — split: version list + preview */}
        <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
          {/* Version list */}
          <div style={{ flex: selectedVersionId ? '0 0 auto' : '1', maxHeight: selectedVersionId ? '200px' : 'none', overflow: 'auto', borderBottom: selectedVersionId ? '1px solid var(--border)' : 'none' }}>
            {versions.map((v, i) => (
              <button
                key={v.id}
                onClick={() => onSelectVersion(v.id)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '12px',
                  width: '100%',
                  padding: '14px 24px',
                  background: selectedVersionId === v.id ? 'var(--bg-card)' : 'transparent',
                  border: 'none',
                  borderBottom: '1px solid var(--border)',
                  cursor: 'pointer',
                  textAlign: 'left',
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{
                    fontFamily: 'var(--sans)',
                    fontSize: '0.85rem',
                    fontWeight: 400,
                    color: 'var(--text-primary)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                  }}>
                    <span>v{v.version_number}</span>
                    {i === 0 && (
                      <span style={{
                        fontSize: '0.65rem',
                        color: 'var(--accent)',
                        background: 'var(--accent-subtle)',
                        padding: '1px 6px',
                        borderRadius: '4px',
                      }}>
                        current
                      </span>
                    )}
                  </div>
                  <div style={{
                    fontFamily: 'var(--sans)',
                    fontSize: '0.75rem',
                    fontWeight: 300,
                    color: 'var(--text-muted)',
                    marginTop: '2px',
                  }}>
                    {formatDate(v.created_at)} &middot; {generatedByLabel(v.generated_by, v.generation_type)}
                    {v.source_tokens_used > 0 && (
                      <> &middot; ~{v.source_tokens_used.toLocaleString()} source tokens</>
                    )}
                  </div>
                </div>
              </button>
            ))}
          </div>

          {/* Version content preview */}
          {selectedVersionId && (
            <div style={{ flex: 1, overflow: 'auto', padding: '20px 24px' }}>
              {versionContent ? (
                <pre style={{
                  fontFamily: 'var(--sans)',
                  fontSize: '0.8rem',
                  fontWeight: 300,
                  color: 'var(--text-secondary)',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  lineHeight: 1.6,
                  margin: 0,
                }}>
                  {versionContent}
                </pre>
              ) : (
                <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Loading...</p>
              )}

              {onRevert && versions.findIndex(v => v.id === selectedVersionId) > 0 && (
                <button
                  onClick={() => onRevert(selectedVersionId)}
                  style={{
                    marginTop: '16px',
                    padding: '8px 16px',
                    background: 'none',
                    border: '1px solid var(--border)',
                    borderRadius: '6px',
                    color: 'var(--text-muted)',
                    fontFamily: 'var(--sans)',
                    fontSize: '0.8rem',
                    cursor: 'pointer',
                  }}
                >
                  Revert to this version
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
