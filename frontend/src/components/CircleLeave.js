import React, { useState } from 'react';
import api from '../api';
import { CircleConfirmDialog } from './CircleDialogs';
import {
  leaveEntryBody, leaveNote, leavePersonBody, leavePersonTitle,
  leavePreviewRequest, leaveRequest,
} from '../utils/circles';

// A reader leaves (Peter, 2026-11-23). The button asks the server what
// leaving would close, shows it in a dialog, and leaves on Leave. The
// author gets no note; afterwards the reader reads what a removed
// reader reads, and nobody sees their name among the readers.

const FAILED = 'That did not work. Try again.';

const quietButton = {
  minHeight: '44px', padding: '0 6px', border: 0, background: 'transparent',
  color: 'var(--text-secondary)', fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '0.8rem', cursor: 'pointer', flexShrink: 0,
};

const errorStyle = {
  fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.8rem',
  color: 'var(--error)',
};

function useLeave(target, onLeft) {
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [openError, setOpenError] = useState('');

  const open = async () => {
    setError('');
    setOpenError('');
    setBusy(true);
    try {
      const res = await leavePreviewRequest(api, target);
      setPreview(res.data);
    } catch (err) {
      setOpenError(err.response?.data?.error || FAILED);
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      const res = await leaveRequest(api, target);
      setPreview(null);
      onLeft?.(res.data);
    } catch (err) {
      setError(err.response?.data?.error || FAILED);
    } finally {
      setBusy(false);
    }
  };

  return {
    preview, busy, error, openError, open, confirm,
    close: () => { setPreview(null); setError(''); },
  };
}

function LeaveButton({ leave }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
      {leave.openError && (
        <span role="alert" style={errorStyle}>{leave.openError}</span>
      )}
      <button type="button" style={quietButton} onClick={leave.open}
        disabled={leave.busy}>
        Leave
      </button>
    </span>
  );
}

/** "Leave" on the banner of a shared entry, for a reader. */
export function LeaveThreadButton({ entryId, onLeft }) {
  const leave = useLeave({ entry_id: entryId }, onLeft);
  return (
    <>
      <LeaveButton leave={leave} />
      <CircleConfirmDialog
        open={!!leave.preview}
        title="Leave this thread?"
        body={leaveEntryBody(leave.preview)}
        note={leaveNote(leave.preview?.author)}
        error={leave.error}
        confirmLabel="Leave"
        busy={leave.busy}
        onClose={leave.close}
        onConfirm={leave.confirm}
      />
    </>
  );
}

/** "Leave" beside a person the reader hid (whose entries they cannot open). */
export function LeavePersonButton({ username, onLeft }) {
  const leave = useLeave({ username }, onLeft);
  return (
    <>
      <LeaveButton leave={leave} />
      <CircleConfirmDialog
        open={!!leave.preview}
        title={leavePersonTitle(username)}
        body={leavePersonBody(username)}
        note={leaveNote(username)}
        error={leave.error}
        confirmLabel="Leave"
        busy={leave.busy}
        onClose={leave.close}
        onConfirm={leave.confirm}
      />
    </>
  );
}
