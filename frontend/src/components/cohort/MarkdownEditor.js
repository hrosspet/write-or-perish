import React, { useRef, useState } from 'react';
import MarkdownBody from '../MarkdownBody';
import useSubmitShortcut, { submitShortcutHint } from '../../hooks/useSubmitShortcut';

// A plain textarea with a markdown preview (the first cohort's report, the
// group introduction, and the founder's letters in Admin). "Write" and
// "Preview" switch between the two; Cmd/Ctrl+Enter runs `onSubmit` (the
// form's main button) when it is given and enabled.

const tabStyle = (active) => ({
  minHeight: '36px', padding: '0 14px', borderRadius: '18px',
  border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
  background: active ? 'var(--bg-card)' : 'transparent',
  color: active ? 'var(--text-primary)' : 'var(--text-secondary)',
  fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.85rem',
  cursor: 'pointer',
});

export const editorTextareaStyle = {
  width: '100%', boxSizing: 'border-box', minHeight: '320px',
  background: 'var(--bg-input)', border: '1px solid var(--border)',
  borderRadius: '8px', color: 'var(--text-primary)',
  fontFamily: 'var(--sans)', fontSize: '0.95rem', fontWeight: 300,
  padding: '16px', lineHeight: 1.65, resize: 'vertical',
};

export default function MarkdownEditor({
  id, label, value, onChange, onSubmit, submitEnabled = true,
  disabled = false, minHeight = '320px', placeholder = '',
  // What the shortcut does, for the hint under the field ("saves").
  shortcutHint = null,
}) {
  const [preview, setPreview] = useState(false);
  const ref = useRef(null);
  useSubmitShortcut(ref, onSubmit, !!onSubmit && submitEnabled && !disabled && !preview);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        {label && (
          <label htmlFor={id} style={{
            fontFamily: 'var(--sans)', fontWeight: 400, fontSize: '0.9rem',
            color: 'var(--text-primary)', marginRight: 'auto',
          }}>
            {label}
          </label>
        )}
        <button type="button" aria-pressed={!preview} style={tabStyle(!preview)}
          onClick={() => setPreview(false)}>
          Write
        </button>
        <button type="button" aria-pressed={preview} style={tabStyle(preview)}
          onClick={() => setPreview(true)}>
          Preview
        </button>
      </div>
      {preview ? (
        <div data-testid={`${id}-preview`} style={{
          minHeight, boxSizing: 'border-box', padding: '16px 18px',
          border: '1px solid var(--border)', borderRadius: '8px',
          background: 'var(--bg-surface)', color: 'var(--text-primary)',
          fontFamily: 'var(--reading-serif)', fontSize: '1.05rem', lineHeight: 1.65,
        }}>
          {value.trim()
            ? <MarkdownBody>{value}</MarkdownBody>
            : <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>Nothing written.</span>}
        </div>
      ) : (
        <textarea
          id={id} ref={ref} value={value} disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          style={{ ...editorTextareaStyle, minHeight }}
        />
      )}
      {onSubmit && shortcutHint && !preview && (
        <span style={{ fontFamily: 'var(--sans)', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          {submitShortcutHint()} {shortcutHint}
        </span>
      )}
    </div>
  );
}
