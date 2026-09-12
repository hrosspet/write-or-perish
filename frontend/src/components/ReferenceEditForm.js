import React, { useCallback, useEffect, useRef, useState } from 'react';
import useSubmitShortcut, { submitShortcutHint } from '../hooks/useSubmitShortcut';
import { NODE_CHAR_CAP } from './SplitContentDialog';

const fieldStyle = {
  boxSizing: 'border-box',
  width: '100%',
  fontFamily: 'var(--sans)',
  fontWeight: 300,
  color: 'var(--text-primary)',
  backgroundColor: 'var(--bg-deep)',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  padding: '10px 12px',
  outline: 'none',
};

const buttonStyle = (primary) => ({
  padding: '6px 14px', borderRadius: '6px', cursor: 'pointer',
  fontFamily: 'var(--sans)', fontSize: '0.82rem', fontWeight: 400,
  background: primary ? 'var(--accent)' : 'none',
  border: primary ? '1px solid var(--accent)' : '1px solid var(--border)',
  color: primary ? 'var(--bg-deep)' : 'var(--text-muted)',
});

/**
 * Inline editor for a saved reference's title and text (#232), the
 * reference-page counterpart of editing a node. Cmd/Ctrl+Enter saves,
 * Escape cancels. The text has the same per-entry cap as a node; over
 * it, Save is disabled and the count says why.
 */
function ReferenceEditForm({ item, saving, onSave, onCancel }) {
  const [title, setTitle] = useState(item.title || '');
  const [content, setContent] = useState(item.content || '');
  const textareaRef = useRef(null);

  const overCap = content.length > NODE_CHAR_CAP;
  const changed = title.trim() !== (item.title || '') || content.trim() !== (item.content || '').trim();
  const canSave = !saving && !overCap && !!content.trim() && changed;

  const submit = useCallback(() => {
    if (!canSave) return;
    onSave({ title: title.trim(), content });
  }, [canSave, onSave, title, content]);

  useSubmitShortcut(textareaRef, submit, canSave);

  useEffect(() => {
    const el = textareaRef.current;
    if (el) el.focus();
  }, []);

  const onKeyDown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
  };

  return (
    <div onKeyDown={onKeyDown}>
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Title"
        maxLength={512}
        style={{ ...fieldStyle, fontFamily: 'var(--serif)', fontWeight: 400, fontSize: '1.3rem', marginBottom: '10px' }}
      />
      <textarea
        ref={textareaRef}
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={Math.min(30, Math.max(8, content.split('\n').length + 2))}
        style={{ ...fieldStyle, fontSize: '0.95rem', lineHeight: 1.6, resize: 'vertical' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginTop: '10px' }}>
        <span style={{ fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300, color: overCap ? 'var(--accent)' : 'var(--text-muted)' }}>
          {overCap
            ? `${content.length.toLocaleString()} characters — the limit is ${NODE_CHAR_CAP.toLocaleString()}.`
            : `${submitShortcutHint()} to save · Esc to cancel`}
        </span>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button type="button" onClick={onCancel} disabled={saving} style={buttonStyle(false)}>Cancel</button>
          <button type="button" onClick={submit} disabled={!canSave} style={{ ...buttonStyle(true), opacity: canSave ? 1 : 0.5 }}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ReferenceEditForm;
