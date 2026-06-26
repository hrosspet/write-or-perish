import React from 'react';
import MarkdownBody from './MarkdownBody';
import { parseIntentions, statusState } from '../utils/intentions';

/**
 * IntentionsView — structured renderer for the intentions artifact.
 *
 * The artifact is a flat list (one "## <name>" entry per intention, grouped
 * under "# Endorsed" / "# Inferred" sections), so default markdown rendering
 * turns it into a wall of large display headings. This renders it in the same
 * visual language as the Todo list: accent uppercase section headers with a
 * count, and clean entry rows (status dot, name, body, dated notes) separated
 * by subtle dividers.
 *
 * The markdown structure is unchanged — it stays clean for the AI to maintain
 * and editable in the raw view. If the content doesn't parse into the expected
 * shape (no "## " entries), we fall back to plain markdown so nothing is lost.
 *
 * parseIntentions / statusState live in utils/intentions (no react-markdown
 * import) so the parser is unit-testable in isolation.
 */

function StatusDot({ state }) {
  const base = {
    width: '15px', height: '15px', borderRadius: '50%',
    flexShrink: 0, marginTop: '4px', boxSizing: 'border-box',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: '0.55rem', fontWeight: 700, color: 'var(--bg-deep)',
  };
  if (state === 'fulfilled') {
    // Achieved — the brightest, fully-resolved amber, marked done (the app's
    // checked-item vocabulary). Reads as "active, brightened into completion".
    return <span style={{ ...base, background: 'var(--accent)' }}>✓</span>;
  }
  if (state === 'released') {
    return <span style={{ ...base, border: '1.5px solid var(--border-hover)', opacity: 0.5 }} />;
  }
  if (state === 'inferred') {
    return <span style={{ ...base, border: '1.5px dashed var(--border-hover)' }} />;
  }
  // active — a held, living intention (dim amber, no marker)
  return <span style={{ ...base, background: 'var(--accent-dim)' }} />;
}

function IntentionEntry({ entry }) {
  const state = statusState(entry.status);
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: '12px',
      padding: '14px 0', borderBottom: '1px solid var(--bg-surface)',
    }}>
      <StatusDot state={state} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontFamily: 'var(--serif)', fontSize: '1.16rem', fontWeight: 600,
          lineHeight: 1.3, color: 'var(--text-primary)',
          // Cormorant is a high-contrast display serif; at this small size the
          // app's global grayscale font-smoothing thins its hairlines on the
          // dark bg. A same-color hairline shadow fattens the strokes just
          // enough to render crisply without changing the letterforms.
          textShadow: '0 0 0.45px currentColor',
          opacity: state === 'released' ? 0.6 : 1,
        }}>
          {entry.name}
        </div>
        {entry.status && (
          <div style={{
            fontFamily: 'var(--sans)', fontSize: '0.72rem', fontWeight: 300,
            letterSpacing: '0.03em', color: 'var(--text-muted)',
            marginTop: '3px',
          }}>
            {entry.status}
          </div>
        )}
        {entry.body.length > 0 && (
          <div style={{
            fontFamily: 'var(--sans)', fontSize: '0.9rem', fontWeight: 300,
            lineHeight: 1.6, color: 'var(--text-secondary)', marginTop: '7px',
          }}>
            {entry.body.join(' ')}
          </div>
        )}
        {entry.notes.length > 0 && (
          <ul style={{
            listStyle: 'none', margin: '8px 0 0', padding: 0,
            borderLeft: '1px solid var(--border)', paddingLeft: '12px',
          }}>
            {entry.notes.map((n, i) => (
              <li key={i} style={{
                fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
                lineHeight: 1.55, color: 'var(--text-muted)', marginBottom: '3px',
              }}>
                {n}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function IntentionsView({ content }) {
  const sections = parseIntentions(content);
  const hasEntries = sections.some((s) => s.entries.length > 0);

  // Format drift / unexpected content — don't lose anything, render raw.
  if (!hasEntries) {
    return <MarkdownBody>{content}</MarkdownBody>;
  }

  return (
    <div>
      {sections.map((section, i) => (
        <div key={i} style={{ marginBottom: '2.5rem' }}>
          {section.title && (
            <div style={{
              fontFamily: 'var(--sans)', fontSize: '0.86rem', fontWeight: 600,
              letterSpacing: '0.16em', textTransform: 'uppercase',
              color: 'var(--accent)', opacity: 0.82, marginBottom: '1.1rem',
              display: 'flex', alignItems: 'center', gap: '10px',
            }}>
              <span>{section.title}</span>
              <span style={{
                color: 'var(--text-muted)', fontWeight: 300,
                fontSize: '0.74rem', letterSpacing: '0.05em', textTransform: 'none',
              }}>
                {section.entries.length}
              </span>
            </div>
          )}
          {section.entries.map((entry, j) => (
            <IntentionEntry key={j} entry={entry} />
          ))}
        </div>
      ))}
    </div>
  );
}
