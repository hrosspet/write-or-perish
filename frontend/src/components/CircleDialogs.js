import React, { useEffect, useState } from 'react';
import {
  aiReadsPrivateSentence, aiReadsSentence, atName, joinNames,
} from '../utils/circles';

// The dialogs of Circles, in the shape of the app's other dialogs
// (PublicReplyDialog, DeleteConfirmDialog).

const overlayStyle = {
  position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: 'rgba(0,0,0,0.7)',
  backdropFilter: 'blur(8px)', WebkitBackdropFilter: 'blur(8px)',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  zIndex: 1100,
};

const cardStyle = {
  background: 'var(--bg-card)',
  border: '1px solid var(--border-hover)',
  borderRadius: '12px',
  padding: '30px 32px',
  width: '480px', maxWidth: '92vw',
  display: 'flex', flexDirection: 'column', gap: '14px',
};

const titleStyle = {
  margin: 0, fontFamily: 'var(--serif)', fontWeight: 500,
  fontSize: '1.75rem', lineHeight: 1.2, color: 'var(--text-primary)',
};

const bodyStyle = {
  margin: 0, fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '1rem', lineHeight: 1.55, color: 'var(--text-primary)',
};

const smallStyle = {
  margin: 0, fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '0.875rem', lineHeight: 1.55, color: 'var(--text-secondary)',
};

const rowStyle = {
  display: 'flex', justifyContent: 'flex-end', gap: '10px',
  marginTop: '6px', flexWrap: 'wrap',
};

const baseButton = {
  minHeight: '46px', padding: '0 20px', borderRadius: '6px',
  fontFamily: 'var(--sans)', fontSize: '0.94rem', cursor: 'pointer',
};

export const quietButton = {
  ...baseButton, fontWeight: 300, background: 'transparent',
  border: '1px solid var(--border-hover)', color: 'var(--text-secondary)',
};

export const plainButton = {
  ...baseButton, fontWeight: 300, background: 'transparent',
  border: '1px solid var(--border-hover)', color: 'var(--text-primary)',
};

export const accentButton = {
  ...baseButton, padding: '0 24px', fontWeight: 400,
  background: 'var(--accent)', border: '1px solid var(--accent)',
  color: 'var(--bg-deep)',
};

function useEscape(open, onClose) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
}

function Shell({ onClose, labelledBy, children }) {
  return (
    <div style={overlayStyle} onClick={onClose}>
      <div
        role="dialog" aria-modal="true" aria-labelledby={labelledBy}
        style={cardStyle} onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

/**
 * Before a reply is sent into a shared thread: "Send this reply? <names>
 * will see it, with your name." With `ai` it is shown before an AI
 * reply is generated in the thread: the same people will see that
 * reply, and `aiReads` names the requester's own artifacts the reply's
 * prompt reads (none: no such sentence). With `onlyMe` the reply is for
 * its requester only: nobody is named, and the dialog says what the
 * prompt shared in the thread reads. "Don't ask again in this thread" is remembered by the caller
 * (per thread, per user, on this device; typed replies and AI replies
 * are remembered separately).
 */
export function CircleReplyDialog({ open, names, ai = false, aiReads = null, onlyMe = false, onClose, onConfirm }) {
  const [skip, setSkip] = useState(false);
  useEscape(open, onClose);
  useEffect(() => { if (open) setSkip(false); }, [open]);
  if (!open) return null;
  const who = joinNames(names);
  const reads = ai ? aiReadsSentence(aiReads) : '';
  const privateReads = aiReadsPrivateSentence(aiReads);
  let body;
  if (ai && onlyMe) {
    body = `Only you will see it.${privateReads ? ` ${privateReads}` : ''}`;
  } else if (ai) {
    body = `${who} will see it, marked with your name.${reads ? ` ${reads}` : ''}`;
  } else {
    body = `${who} will see it, with your name.`;
  }
  return (
    <Shell onClose={onClose} labelledBy="circle-reply-title">
      <h2 id="circle-reply-title" style={titleStyle}>
        {ai ? 'Generate an AI reply?' : 'Send this reply?'}
      </h2>
      <p style={bodyStyle}>
        {body}
      </p>
      <label style={{
        display: 'flex', alignItems: 'center', gap: '12px', minHeight: '44px',
        fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.875rem',
        color: 'var(--text-secondary)', cursor: 'pointer',
      }}>
        <input
          type="checkbox"
          checked={skip}
          onChange={(e) => setSkip(e.target.checked)}
          style={{ width: '20px', height: '20px', accentColor: 'var(--accent)', margin: 0 }}
        />
        Don't ask again in this thread
      </label>
      <div style={rowStyle}>
        <button type="button" style={quietButton} onClick={onClose}>Cancel</button>
        <button type="button" style={accentButton} onClick={() => onConfirm(skip)}>
          {ai ? 'Generate' : 'Send reply'}
        </button>
      </div>
    </Shell>
  );
}

/**
 * Adding a person to a circle: Cancel, "Also show earlier entries",
 * "Add <name>". The default (the accent button) is new entries only:
 * the person reads what is shared with the circle from now on. "Also
 * show earlier entries" opens the entries already shared with the
 * circle too (the real count is stated); it is offered only when there
 * are such entries. onChoice(includeExisting: boolean).
 */
export function AddCircleMemberDialog({ open, name: rawName, circleName, sharedCount = 0, busy = false, onClose, onChoice }) {
  useEscape(open, onClose);
  if (!open) return null;
  const name = atName(rawName);
  const fromNowOn = `${name} will read every entry you share with ${circleName} from now on.`;
  const earlier = sharedCount === 0
    ? 'You have not shared anything with it yet.'
    : `The ${sharedCount === 1 ? 'entry' : `${sharedCount} entries`} you have already shared with it ${sharedCount === 1 ? 'stays' : 'stay'} closed to ${name} unless you choose "Also show earlier entries".`;
  return (
    <Shell onClose={onClose} labelledBy="circle-add-title">
      <h2 id="circle-add-title" style={titleStyle}>
        Add {name} to {circleName}?
      </h2>
      <p style={bodyStyle}>{`${fromNowOn} ${earlier}`}</p>
      <p style={smallStyle}>
        {name} gets a note in Loore the first time there is something to
        read. You can remove {name} at any time; the entries are closed
        to {name} from that moment.
      </p>
      <div style={rowStyle}>
        <button type="button" style={quietButton} onClick={onClose} disabled={busy}>
          Cancel
        </button>
        {sharedCount > 0 && (
          <button type="button" style={plainButton} disabled={busy}
            onClick={() => onChoice(true)}>
            Also show earlier entries
          </button>
        )}
        <button type="button" style={accentButton} disabled={busy}
          onClick={() => onChoice(false)}>
          Add {name}
        </button>
      </div>
    </Shell>
  );
}

/** A plain yes/no with the app's dialog look (delete a circle, hide a person). */
export function CircleConfirmDialog({ open, title, body, confirmLabel, busy = false, onClose, onConfirm }) {
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <Shell onClose={onClose} labelledBy="circle-confirm-title">
      <h2 id="circle-confirm-title" style={titleStyle}>{title}</h2>
      <p style={bodyStyle}>{body}</p>
      <div style={rowStyle}>
        <button type="button" style={quietButton} onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" style={accentButton} onClick={onConfirm} disabled={busy}>
          {confirmLabel}
        </button>
      </div>
    </Shell>
  );
}
