import React from 'react';
import MarkdownBody from '../MarkdownBody';
import { STEP_ORDER, stepIndex } from '../../utils/cohort';

// Shared look of /cohort: the Circles pages' type and buttons
// (CircleDialogs), one column, left-aligned.

export { accentButton, plainButton, quietButton } from '../CircleDialogs';

export const pageStyle = {
  padding: '3rem clamp(1.25rem, 5vw, 2rem) 4rem', maxWidth: '720px',
  margin: '0 auto', fontFamily: 'var(--sans)', fontWeight: 300,
  color: 'var(--text-primary)', display: 'flex', flexDirection: 'column',
  gap: '28px', boxSizing: 'border-box',
};

export const h1Style = {
  margin: 0, fontFamily: 'var(--serif)', fontWeight: 500,
  fontSize: '2.25rem', lineHeight: 1.15, color: 'var(--text-primary)',
};

export const h2Style = {
  margin: 0, fontFamily: 'var(--serif)', fontWeight: 500,
  fontSize: '1.6rem', lineHeight: 1.2, color: 'var(--text-primary)',
};

export const textStyle = {
  margin: 0, fontSize: '0.98rem', lineHeight: 1.6,
  color: 'var(--text-secondary)', maxWidth: '600px',
};

export const smallStyle = {
  margin: 0, fontSize: '0.85rem', lineHeight: 1.55,
  color: 'var(--text-secondary)',
};

export const errorStyle = {
  margin: 0, fontSize: '0.9rem', lineHeight: 1.5, color: 'var(--error)',
};

// The sentence that names who will read something, set apart as in the
// Circles audience panel.
export const readersStyle = {
  margin: 0, fontFamily: 'var(--serif)', fontSize: '1.3rem',
  lineHeight: 1.4, color: 'var(--text-primary)',
};

export const rowStyle = {
  display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'center',
};

export const sectionStyle = {
  display: 'flex', flexDirection: 'column', gap: '12px',
};

export const linkStyle = {
  color: 'var(--accent)', textDecoration: 'none', fontSize: '0.94rem',
};

// A link that looks like the accent button (an outside link: a calendar,
// a call).
export const accentLinkStyle = {
  minHeight: '46px', padding: '0 24px', borderRadius: '6px',
  fontFamily: 'var(--sans)', fontSize: '0.94rem', fontWeight: 400,
  background: 'var(--accent)', border: '1px solid var(--accent)',
  color: 'var(--bg-deep)', textDecoration: 'none',
  display: 'inline-flex', alignItems: 'center', alignSelf: 'flex-start',
};

export function Section({ title, children, labelledBy }) {
  return (
    <section aria-labelledby={labelledBy} style={sectionStyle}>
      {title && <h2 id={labelledBy} style={h2Style}>{title}</h2>}
      {children}
    </section>
  );
}

// Where the person is among the four steps: a short ordered list, the
// current step in the text colour, the ones before it marked done.
export function StepList({ step }) {
  const at = stepIndex(step);
  if (at < 0) return null;
  return (
    <ol aria-label="Steps" style={{
      listStyle: 'none', margin: 0, padding: 0, display: 'flex',
      flexWrap: 'wrap', gap: '6px 22px', fontSize: '0.85rem',
    }}>
      {STEP_ORDER.map((s, i) => (
        <li key={s.step} aria-current={i === at ? 'step' : undefined} style={{
          color: i === at ? 'var(--text-primary)' : 'var(--text-muted)',
          fontWeight: i === at ? 400 : 300,
          display: 'flex', alignItems: 'center', gap: '6px',
        }}>
          {i < at ? (
            <svg aria-label="done" width="12" height="12" viewBox="0 0 14 14" fill="none"
              stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M2.5 7.5l3 3 6-7" />
            </svg>
          ) : (
            <span aria-hidden="true" style={{
              width: '7px', height: '7px', borderRadius: '50%',
              background: i === at ? 'var(--accent)' : 'var(--border-hover)',
            }} />
          )}
          {s.label}
        </li>
      ))}
    </ol>
  );
}

// The founder's letter, folded: the person read it on the invitation page.
export function FoldedLetter({ founder, letter }) {
  if (!letter) return null;
  return (
    <details style={{
      borderTop: '1px solid var(--border)', paddingTop: '14px',
    }}>
      <summary style={{
        cursor: 'pointer', fontSize: '0.94rem', color: 'var(--text-secondary)',
        minHeight: '32px',
      }}>
        Your letter from {founder}
      </summary>
      <div style={{
        marginTop: '14px', fontFamily: 'var(--reading-serif)',
        fontSize: '1.08rem', lineHeight: 1.7, color: 'var(--text-primary)',
      }}>
        <MarkdownBody paragraphMargin="0 0 1em">{letter}</MarkdownBody>
      </div>
    </details>
  );
}
