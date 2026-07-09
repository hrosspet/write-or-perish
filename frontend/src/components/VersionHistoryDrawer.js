import React, { useEffect, useMemo, useState } from 'react';
import { formatDate as formatDateShared } from '../utils/date';
import { computeLineDiff, refineWordDiffs, collapseUnchanged } from '../utils/diff';

/**
 * VersionHistoryDrawer — Slides in from the right.
 *
 * Props:
 *   isOpen, onClose, title,
 *   versions: [{ id, version_number, created_at, generated_by, tokens_used }],
 *   selectedVersionId, onSelectVersion,
 *   versionContent (string|null — the loaded content for selectedVersionId),
 *   previousVersionContent (optional: string when the previous version's
 *     content is loaded, null while loading / when no previous version
 *     exists, undefined when the parent doesn't supply it) — when a
 *     previous version is available the preview defaults to a line DIFF
 *     against it (versions are long and changes incremental; a full-text
 *     comparison by eye was hopeless), with a Diff/Full toggle,
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
  previousVersionContent,
  onRevert,
}) {
  const [viewMode, setViewMode] = useState('diff');

  // Diff is possible only when both sides are loaded strings. The oldest
  // version has no previous → full text only.
  const isOldest = selectedVersionId != null
    && versions.length > 0
    && versions[versions.length - 1].id === selectedVersionId;
  const diffAvailable = typeof versionContent === 'string'
    && typeof previousVersionContent === 'string'
    && !isOldest;

  const diffOps = useMemo(() => {
    if (!diffAvailable) return null;
    return refineWordDiffs(
      computeLineDiff(previousVersionContent, versionContent));
  }, [diffAvailable, previousVersionContent, versionContent]);

  // Smart default per selection: incremental changes read best as a diff,
  // but a near-total rewrite (regenerated profiles churn >50% of lines)
  // reads as a wall of red/green — default those to full text. The toggle
  // always allows both.
  const changedCount = useMemo(
    () => (diffOps ? diffOps.filter(o => o.type !== 'same').length : 0),
    [diffOps]);
  const isHeavyRewrite = diffOps
    ? changedCount > diffOps.length * 0.5 && changedCount > 40
    : false;
  useEffect(() => {
    if (diffOps) setViewMode(isHeavyRewrite ? 'full' : 'diff');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diffOps]);

  const diffRows = useMemo(() => {
    if (!diffOps || viewMode !== 'diff') return null;
    return collapseUnchanged(diffOps, 2);
  }, [diffOps, viewMode]);

  // hasSegments: word-refined lines carry the strikethrough / stronger
  // tint on the changed WORDS instead of the whole line (GitHub-style).
  const diffLineStyle = (type, hasSegments = false) => ({
    fontFamily: 'var(--sans)',
    fontSize: '0.8rem',
    fontWeight: 300,
    lineHeight: 1.6,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    padding: '0 6px',
    borderRadius: '2px',
    ...(type === 'add' && {
      color: 'var(--text-primary)',
      background: 'color-mix(in srgb, var(--success) 12%, transparent)',
    }),
    ...(type === 'del' && {
      color: 'var(--text-muted)',
      background: 'color-mix(in srgb, var(--error) 10%, transparent)',
      ...(!hasSegments && {
        textDecoration: 'line-through',
        textDecorationColor: 'color-mix(in srgb, var(--error) 45%, transparent)',
      }),
    }),
    ...(type === 'same' && {
      color: 'var(--text-muted)',
    }),
  });

  const segmentStyle = (type, changed) => (changed ? {
    borderRadius: '2px',
    ...(type === 'add' && {
      background: 'color-mix(in srgb, var(--success) 32%, transparent)',
    }),
    ...(type === 'del' && {
      background: 'color-mix(in srgb, var(--error) 28%, transparent)',
      textDecoration: 'line-through',
      textDecorationColor: 'color-mix(in srgb, var(--error) 55%, transparent)',
    }),
  } : {});

  const renderDiffLine = (row) => (
    row.segments
      ? row.segments.map((seg, j) => (
          <span key={j} style={segmentStyle(row.type, seg.changed)}>
            {seg.text}
          </span>
        ))
      : (row.text || ' ')
  );
  // Close on Escape
  useEffect(() => {
    if (!isOpen) return;
    const handleKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [isOpen, onClose]);

  // Synthetic "v0 = file default" entries arrive with null created_at, so
  // fall back to a clear label rather than rendering the epoch (#128).
  const formatDate = (iso) => formatDateShared(iso, { fallback: 'File default' });

  const generatedByLabel = (g, genType) => {
    if (g === 'user' || g === 'manual') return 'Manual edit';
    if (g === 'revert') return 'Reverted';
    if (g === 'orient_session') return 'Orient session';
    if (g === 'voice_session') return 'Voice';
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
              {(diffAvailable || isOldest || onRevert) && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: '12px',
                  marginBottom: '14px',
                }}>
                  {diffAvailable && ['diff', 'full'].map((mode) => (
                    <button
                      key={mode}
                      onClick={() => setViewMode(mode)}
                      style={{
                        background: 'none', border: 'none', cursor: 'pointer',
                        padding: '2px 0',
                        fontFamily: 'var(--sans)', fontSize: '0.75rem',
                        letterSpacing: '0.04em',
                        color: viewMode === mode ? 'var(--accent)' : 'var(--text-muted)',
                        borderBottom: viewMode === mode
                          ? '1px solid var(--accent)' : '1px solid transparent',
                      }}
                    >
                      {mode === 'diff' ? `Changes (${changedCount})` : 'Full text'}
                    </button>
                  ))}
                  {isOldest && (
                    <span style={{
                      fontFamily: 'var(--sans)', fontSize: '0.75rem',
                      color: 'var(--text-muted)', letterSpacing: '0.04em',
                    }}>
                      Initial version
                    </span>
                  )}
                  {onRevert && versions.findIndex(v => v.id === selectedVersionId) > 0 && (
                    <button
                      onClick={() => onRevert(selectedVersionId)}
                      style={{
                        marginLeft: 'auto',
                        padding: '3px 10px',
                        background: 'none',
                        border: '1px solid var(--border)',
                        borderRadius: '6px',
                        color: 'var(--text-muted)',
                        fontFamily: 'var(--sans)',
                        fontSize: '0.72rem',
                        cursor: 'pointer',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      Revert to this version
                    </button>
                  )}
                </div>
              )}

              {versionContent == null ? (
                <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Loading...</p>
              ) : diffRows ? (
                <div style={{ margin: 0 }}>
                  {diffRows.length === 0 || diffRows.every(r => r.type === 'same' || r.type === 'skip') ? (
                    <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                      No changes from the previous version.
                    </p>
                  ) : diffRows.map((row, i) => (
                    row.type === 'skip' ? (
                      <div
                        key={i}
                        style={{
                          fontFamily: 'var(--sans)', fontSize: '0.7rem',
                          color: 'var(--text-muted)', opacity: 0.7,
                          textAlign: 'center', padding: '6px 0',
                          userSelect: 'none',
                        }}
                      >
                        ⋯ {row.count} unchanged {row.count === 1 ? 'line' : 'lines'} ⋯
                      </div>
                    ) : (
                      <div key={i} style={diffLineStyle(row.type, !!row.segments)}>
                        {renderDiffLine(row)}
                      </div>
                    )
                  ))}
                </div>
              ) : (
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
              )}

            </div>
          )}
        </div>
      </div>
    </>
  );
}
