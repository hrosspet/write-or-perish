import React from 'react';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime } from '../utils/date';

const buttonStyle = {
  padding: '6px 14px',
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  color: 'var(--text-secondary)',
  fontFamily: 'var(--sans)',
  fontSize: '0.8rem',
  fontWeight: 400,
  cursor: 'pointer',
};

const noteStyle = {
  margin: '0 0 10px',
  fontFamily: 'var(--sans)',
  fontSize: '0.85rem',
  fontWeight: 300,
  color: 'var(--text-primary)',
  lineHeight: 1.5,
};

function useCopy() {
  const { addToast } = useToast();
  return async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      addToast('Copied your text');
    } catch (err) {
      addToast("Couldn't copy; select the text and copy it instead");
    }
  };
}

/**
 * An edit form's Save was refused: the text changed since the form was
 * opened (#799). Says what to do and copies the form's text; the form keeps
 * the text and stays open.
 */
export function NewerVersionNotice({ message, text }) {
  const copy = useCopy();
  return (
    <div role="alert" style={{
      border: '1px solid var(--accent-dim)', borderRadius: '8px',
      padding: '14px 16px', margin: '12px 0', background: 'var(--bg-card)',
    }}>
      <p style={noteStyle}>{message}</p>
      <button type="button" onClick={() => copy(text)} style={buttonStyle}>
        Copy my text
      </button>
    </div>
  );
}

/**
 * An unsaved edit written on an older version of the entry (#761, #799):
 * shown below the editor, which holds the entry as it is now (or what the
 * person typed since), to copy from or discard. It is never loaded into the
 * editor, so its Save can't put the older text over the newer one.
 */
export function OlderDraftBox({ draft, formShowsNewerText, onDiscard }) {
  const copy = useCopy();
  const saved = draft.updated_at ? formatDateTime(draft.updated_at) : '';
  return (
    <div style={{ marginTop: '12px' }} data-testid="older-draft">
      <p style={{ ...noteStyle, color: 'var(--text-muted)', fontSize: '0.8rem' }}>
        {saved ? `Your unsaved edit from ${saved}.` : 'Your unsaved edit.'}
        {' The entry changed after it, so the form shows '}
        {formShowsNewerText ? 'what you typed since.' : 'the entry as it is now.'}
        {' Copy what you need into it.'}
      </p>
      <textarea
        readOnly
        value={draft.content}
        aria-label="Your unsaved edit"
        style={{
          width: '100%', minHeight: '120px', background: 'var(--bg-surface)',
          border: '1px dashed var(--border)', borderRadius: '8px',
          color: 'var(--text-secondary)', fontFamily: 'var(--sans)',
          fontSize: '0.85rem', fontWeight: 300, padding: '12px 14px',
          lineHeight: 1.6, resize: 'vertical', boxSizing: 'border-box',
        }}
      />
      <div style={{ marginTop: '8px', display: 'flex', gap: '8px' }}>
        <button type="button" onClick={() => copy(draft.content)} style={buttonStyle}>Copy</button>
        <button type="button" onClick={onDiscard} style={buttonStyle}>Discard</button>
      </div>
    </div>
  );
}
