import React from 'react';

/**
 * DiffRows — a line diff, as the version history shows it: added lines
 * tinted green, removed ones red (struck through unless word-refined),
 * long unchanged runs folded into "⋯ N unchanged lines ⋯".
 *
 * Props:
 *   rows: display rows from utils/diff (collapseUnchanged over
 *     refineWordDiffs): { type: 'same'|'add'|'del', text, segments? } or
 *     { type: 'skip', count }
 *   emptyText: shown when no row is a change
 *
 * Shared by VersionHistoryDrawer and the "Show changes" of an artifact
 * write in a reply's Actions taken (#604).
 */

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
    : (row.text || ' ')
);

export default function DiffRows({
  rows,
  emptyText = 'No changes from the previous version.',
}) {
  return (
    <div style={{ margin: 0 }}>
      {rows.length === 0 || rows.every(r => r.type === 'same' || r.type === 'skip') ? (
        <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
          {emptyText}
        </p>
      ) : rows.map((row, i) => (
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
          <div key={i} data-diff={row.type} style={diffLineStyle(row.type, !!row.segments)}>
            {renderDiffLine(row)}
          </div>
        )
      ))}
    </div>
  );
}
