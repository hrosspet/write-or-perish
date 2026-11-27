import React, { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';
import CopyButton from './CopyButton';
import XLookupConfirmDialog from './XLookupConfirmDialog';
import { CircleConfirmDialog, accentButton, plainButton, quietButton } from './CircleDialogs';
import MarkdownEditor from './cohort/MarkdownEditor';
import useSubmitShortcut from '../hooks/useSubmitShortcut';
import { formatDateTime } from '../utils/date';
import { errorSentence, fromLocalInput, toLocalInput } from '../utils/cohort';

// Admin → Cohorts (the first cohort, docs/experiments/FIRST-COHORT.md,
// "Your side"): the founder creates the cohort and edits it, adds an
// invitation (an X handle and his letter), copies its link to send it
// himself, and follows each invitation by its state and times, with Call
// done, Admit and Close. The server answers states, timestamps and the
// founder's own letters only: nothing anyone wrote in Loore is here.

const inputStyle = {
  width: '100%', boxSizing: 'border-box', minHeight: '42px', padding: '0 12px',
  borderRadius: '6px', border: '1px solid var(--border)',
  backgroundColor: 'var(--bg-input)', color: 'var(--text-primary)',
  fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.95rem',
};

const labelStyle = {
  display: 'block', fontFamily: 'var(--sans)', fontWeight: 400,
  fontSize: '0.85rem', color: 'var(--text-primary)', marginBottom: '6px',
};

const hintStyle = {
  fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.8rem',
  lineHeight: 1.5, color: 'var(--text-secondary)', margin: '6px 0 0',
};

const boxStyle = {
  border: '1px solid var(--border)', borderRadius: '10px',
  padding: '20px', marginBottom: '24px', display: 'flex',
  flexDirection: 'column', gap: '16px', maxWidth: '760px',
};

const h2Style = {
  margin: 0, fontFamily: 'var(--serif)', fontWeight: 400,
  fontSize: '1.5rem', color: 'var(--text-primary)',
};

const errorStyle = { color: 'var(--error)', fontSize: '0.9rem', margin: 0 };
const noticeStyle = { color: 'var(--text-secondary)', fontSize: '0.9rem', margin: 0 };

const smallButton = {
  minHeight: '34px', padding: '0 12px', borderRadius: '6px',
  border: '1px solid var(--border-hover)', background: 'transparent',
  color: 'var(--text-primary)', fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '0.82rem', cursor: 'pointer',
};

export const STATE_LABELS = {
  invited: 'Invited',
  opened: 'Opened',
  claimed: 'Signed in',
  interview_started: 'Interview begun',
  report_drafted: 'Report drafted',
  report_sent: 'Report sent',
  report_skipped: 'Sent nothing',
  call_done: 'Call done',
  admitted: 'Admitted',
  declined: 'Declined',
  closed: 'Closed',
};

const TIMES = [
  ['created_at', 'Invited'],
  ['first_opened_at', 'Opened'],
  ['claimed_at', 'Signed in'],
  ['interview_started_at', 'Interview begun'],
  ['report_drafted_at', 'Report drafted'],
  ['report_sent_at', 'Report sent'],
  ['report_skipped_at', 'Sent nothing'],
  ['call_done_at', 'Call done'],
  ['admitted_at', 'Admitted'],
  ['declined_at', 'Declined'],
  ['closed_at', 'Closed'],
];

const EMPTY_FORM = {
  name: '', founder_name: '', about: '', booking_url: '', call_url: '',
  next_call_at: '', question: '', free_until: '',
};

export function formFromCohort(c) {
  if (!c) return { ...EMPTY_FORM };
  return {
    name: c.name || '',
    founder_name: c.founder_name || '',
    about: c.about || '',
    booking_url: c.booking_url || '',
    call_url: c.call_url || '',
    next_call_at: toLocalInput(c.next_call_at),
    question: c.question || '',
    free_until: c.free_until || '',
  };
}

// The body of POST / PUT /api/admin/cohorts: the next call goes as UTC
// (with a Z); an empty field clears it.
export function cohortBody(form) {
  return {
    name: form.name.trim(),
    founder_name: form.founder_name.trim(),
    about: form.about,
    booking_url: form.booking_url.trim(),
    call_url: form.call_url.trim(),
    next_call_at: fromLocalInput(form.next_call_at),
    question: form.question,
    free_until: form.free_until || null,
  };
}

// Which buttons an invitation's row offers.
export function invitationActions(inv) {
  const live = !inv.declined_at && !inv.closed_at;
  const claimed = !!inv.username || !!inv.claimed_at;
  return {
    callDone: live && claimed && !inv.call_done_at,
    admit: live && claimed && !inv.admitted_at,
    close: live,
    editLetter: live && !claimed,
    // A "Not for me" given before anyone accepted the invitation can be
    // undone: the link shows the letter again.
    reopen: !!inv.declined_at && !inv.closed_at && !claimed,
  };
}

function viewerZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (e) {
    return '';
  }
}

function personOf(inv) {
  if (inv.x_handle && inv.username && inv.username.toLowerCase() !== inv.x_handle) {
    return `@${inv.x_handle} (Loore: @${inv.username})`;
  }
  if (inv.x_handle) return `@${inv.x_handle}`;
  if (inv.username) return `@${inv.username}`;
  return 'No handle';
}

function CohortForm({ cohort, onSaved }) {
  const [form, setForm] = useState(() => formFromCohort(cohort));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const nameRef = useRef(null);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target ? e.target.value : e }));

  const save = useCallback(async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    if (!form.name.trim()) {
      setError('name is required.');
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const res = cohort
        ? await api.put(`/admin/cohorts/${cohort.id}`, cohortBody(form))
        : await api.post('/admin/cohorts', cohortBody(form));
      setNotice(cohort ? 'Saved.' : 'Cohort created.');
      onSaved(res.data.cohort);
    } catch (err) {
      setError(errorSentence(err));
    }
    setBusy(false);
  }, [cohort, form, onSaved]);

  useSubmitShortcut(nameRef, save, !busy);
  const zone = viewerZone();

  return (
    <form onSubmit={save} style={boxStyle}>
      <h2 style={h2Style}>{cohort ? cohort.name : 'New cohort'}</h2>
      {cohort && (
        <p style={hintStyle}>
          Address of the group: {cohort.slug}.{' '}
          {Object.keys(cohort.state_counts || {}).length > 0
            ? Object.entries(cohort.state_counts)
              .map(([state, n]) => `${n} ${(STATE_LABELS[state] || state).toLowerCase()}`)
              .join(', ')
            : 'No invitations yet.'}
        </p>
      )}
      <div>
        <label htmlFor="cohort-name" style={labelStyle}>Name</label>
        <input id="cohort-name" ref={nameRef} type="text" value={form.name}
          maxLength={60} onChange={set('name')} style={inputStyle} />
        <p style={hintStyle}>Members see it as the name of the group and of its circle.</p>
      </div>
      <div>
        <label htmlFor="cohort-founder-name" style={labelStyle}>Your name, as the invited people read it</label>
        <input id="cohort-founder-name" type="text" value={form.founder_name}
          maxLength={64} onChange={set('founder_name')} style={inputStyle} />
        <p style={hintStyle}>For example on the button "Send to …". Empty: your username.</p>
      </div>
      <MarkdownEditor id="cohort-about" label="What the group is"
        value={form.about} onChange={set('about')} minHeight="160px" />
      <p style={{ ...hintStyle, marginTop: '-8px' }}>Shown on every invitation page under "What this is", and on the group page.</p>
      <div>
        <label htmlFor="cohort-booking" style={labelStyle}>Link to your calendar</label>
        <input id="cohort-booking" type="url" value={form.booking_url}
          onChange={set('booking_url')} style={inputStyle} placeholder="https://" />
        <p style={hintStyle}>Offered to a person after they sent their report, or sent nothing.</p>
      </div>
      <div>
        <label htmlFor="cohort-call" style={labelStyle}>Link to the weekly call</label>
        <input id="cohort-call" type="url" value={form.call_url}
          onChange={set('call_url')} style={inputStyle} placeholder="https://" />
      </div>
      <div>
        <label htmlFor="cohort-next-call" style={labelStyle}>Next call</label>
        <input id="cohort-next-call" type="datetime-local" value={form.next_call_at}
          onChange={set('next_call_at')} style={inputStyle} />
        <p style={hintStyle}>
          In your time zone{zone ? ` (${zone.replace(/_/g, ' ')})` : ''}. Each member sees it in their own.
        </p>
      </div>
      <div>
        <label htmlFor="cohort-question" style={labelStyle}>Question of the week</label>
        <textarea id="cohort-question" value={form.question} onChange={set('question')}
          style={{ ...inputStyle, minHeight: '90px', padding: '10px 12px', lineHeight: 1.5, resize: 'vertical' }} />
      </div>
      <div>
        <label htmlFor="cohort-free-until" style={labelStyle}>Free until</label>
        <input id="cohort-free-until" type="date" value={form.free_until}
          onChange={set('free_until')} style={{ ...inputStyle, maxWidth: '220px' }} />
        <p style={hintStyle}>
          The last free day. Until then a person who accepted an invitation has
          full access without a card. Empty: nobody gets access for free while
          payments are on.
        </p>
      </div>
      <div style={{ display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="submit" style={accentButton} disabled={busy}>
          {cohort ? 'Save' : 'Create cohort'}
        </button>
        {notice && <p role="status" style={noticeStyle}>{notice}</p>}
        {error && <p role="alert" style={errorStyle}>{error}</p>}
      </div>
    </form>
  );
}

function NewInvitation({ cohort, onAdded }) {
  const [handle, setHandle] = useState('');
  const [letter, setLetter] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState(null);
  const [xLookupAsk, setXLookupAsk] = useState(null);

  const submit = useCallback(async (xLookup = false) => {
    if (busy) return;
    if (!letter.trim()) {
      setError('letter is required.');
      return;
    }
    const cleanHandle = handle.trim().replace(/^@/, '');
    setBusy(true);
    setError('');
    setAdded(null);
    try {
      const res = await api.post(`/admin/cohorts/${cohort.id}/invitations`, {
        ...(cleanHandle ? { x_handle: cleanHandle } : {}),
        letter,
        x_lookup: xLookup,
      });
      setAdded({ handle: cleanHandle, link: `${window.location.origin}${res.data.link}` });
      setHandle('');
      setLetter('');
      onAdded();
    } catch (err) {
      const data = (err.response && err.response.data) || {};
      // The archive does not have the handle: the only other source of its
      // X id is a paid X read. Asked here, as the whitelist asks.
      if (!xLookup && (data.reason === 'not-in-archive' || data.reason === 'archive-error')) {
        setXLookupAsk({ handle: cleanHandle, message: data.error, costUsd: data.x_lookup_cost_usd });
      } else {
        setError(errorSentence(err));
      }
    }
    setBusy(false);
  }, [busy, cohort.id, handle, letter, onAdded]);

  return (
    <div style={boxStyle}>
      <h2 style={h2Style}>Add an invitation</h2>
      <div>
        <label htmlFor="invitation-handle" style={labelStyle}>X handle</label>
        <input id="invitation-handle" type="text" value={handle} placeholder="@handle"
          autoCapitalize="none" autoCorrect="off" spellCheck={false}
          onChange={(e) => setHandle(e.target.value)} style={{ ...inputStyle, maxWidth: '320px' }} />
        <p style={hintStyle}>
          Approves the handle as the whitelist does; only that X account can accept
          the invitation. An invitation without a handle, which any signed-in account
          holding the link can accept, is made only where COHORT_ALLOW_OPEN_INVITATIONS
          is on (for trying it locally).
        </p>
      </div>
      <MarkdownEditor id="invitation-letter" label="Letter"
        value={letter} onChange={setLetter} minHeight="260px"
        onSubmit={() => submit(false)} submitEnabled={!busy && !!letter.trim()}
        shortcutHint="adds the invitation" />
      <div style={{ display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="button" style={accentButton} disabled={busy || !letter.trim()}
          onClick={() => submit(false)}>
          {busy ? 'Adding…' : 'Add invitation'}
        </button>
        {error && <p role="alert" style={errorStyle}>{error}</p>}
      </div>
      {added && (
        <div role="status" style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <p style={noticeStyle}>
            Invitation {added.handle ? `for @${added.handle} ` : ''}added. Loore sends nothing: send this link yourself.
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <code style={{ fontSize: '0.82rem', wordBreak: 'break-all', color: 'var(--text-primary)' }}>{added.link}</code>
            <CopyButton text={added.link} title="Copy link" />
          </div>
        </div>
      )}
      <XLookupConfirmDialog
        open={!!xLookupAsk}
        handle={xLookupAsk && xLookupAsk.handle}
        message={xLookupAsk && xLookupAsk.message}
        costUsd={xLookupAsk && xLookupAsk.costUsd}
        onClose={() => setXLookupAsk(null)}
        onConfirm={() => {
          setXLookupAsk(null);
          submit(true);
        }}
      />
    </div>
  );
}

function LetterEditor({ cohortId, invitation, onDone, onCancel }) {
  const [letter, setLetter] = useState(invitation.letter || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const save = async () => {
    setBusy(true);
    setError('');
    try {
      await api.put(`/admin/cohorts/${cohortId}/invitations/${invitation.id}`, { letter });
      onDone();
    } catch (err) {
      setError(errorSentence(err));
      setBusy(false);
    }
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', padding: '12px 0' }}>
      <MarkdownEditor id={`invitation-letter-${invitation.id}`} label={`Letter to ${personOf(invitation)}`}
        value={letter} onChange={setLetter} minHeight="220px"
        onSubmit={save} submitEnabled={!busy && !!letter.trim()} shortcutHint="saves" />
      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="button" style={plainButton} disabled={busy || !letter.trim()} onClick={save}>
          Save letter
        </button>
        <button type="button" style={quietButton} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        {error && <p role="alert" style={errorStyle}>{error}</p>}
      </div>
    </div>
  );
}

function InvitationTable({ cohort, invitations, onChanged }) {
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState('');
  const [closeAsk, setCloseAsk] = useState(null);
  const [editing, setEditing] = useState(null);

  const act = async (inv, action) => {
    setBusyId(inv.id);
    setError('');
    try {
      await api.post(`/admin/cohorts/${cohort.id}/invitations/${inv.id}/${action}`);
      await onChanged();
    } catch (err) {
      setError(errorSentence(err));
    }
    setBusyId(null);
  };

  if (invitations.length === 0) {
    return <p style={noticeStyle}>No invitations yet.</p>;
  }

  const cell = {
    padding: '10px 10px', borderTop: '1px solid var(--border)',
    verticalAlign: 'top', fontSize: '0.85rem', textAlign: 'left',
  };

  return (
    <div style={{ overflowX: 'auto' }}>
      {error && <p role="alert" style={{ ...errorStyle, marginBottom: '10px' }}>{error}</p>}
      <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: '760px' }}>
        <thead>
          <tr>
            {['Person', 'State', 'Times', 'Link', ''].map((h) => (
              <th key={h} style={{ ...cell, borderTop: 'none', fontWeight: 400, color: 'var(--text-secondary)' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {invitations.map((inv) => {
            const can = invitationActions(inv);
            const busy = busyId === inv.id;
            const link = `${window.location.origin}${inv.link}`;
            return (
              <React.Fragment key={inv.id}>
                <tr>
                  <td style={cell}>{personOf(inv)}</td>
                  <td style={{ ...cell, color: 'var(--text-primary)' }}>{STATE_LABELS[inv.state] || inv.state}</td>
                  <td style={{ ...cell, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
                    {TIMES.filter(([key]) => inv[key]).map(([key, label]) => (
                      <div key={key}>{label} {formatDateTime(inv[key])}</div>
                    ))}
                  </td>
                  <td style={cell}>
                    <CopyButton text={link} title="Copy link" />
                  </td>
                  <td style={{ ...cell, whiteSpace: 'nowrap' }}>
                    <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                      {can.editLetter && (
                        <button type="button" style={smallButton} disabled={busy}
                          onClick={() => setEditing(editing === inv.id ? null : inv.id)}>
                          Edit letter
                        </button>
                      )}
                      {can.callDone && (
                        <button type="button" style={smallButton} disabled={busy}
                          onClick={() => act(inv, 'call_done')}>
                          Call done
                        </button>
                      )}
                      {can.admit && (
                        <button type="button" style={smallButton} disabled={busy}
                          onClick={() => act(inv, 'admit')}>
                          Admit
                        </button>
                      )}
                      {can.close && (
                        <button type="button" style={smallButton} disabled={busy}
                          onClick={() => setCloseAsk(inv)}>
                          Close
                        </button>
                      )}
                      {can.reopen && (
                        <button type="button" style={smallButton} disabled={busy}
                          onClick={() => act(inv, 'reopen')}>
                          Reopen
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
                {editing === inv.id && (
                  <tr>
                    <td colSpan={5} style={{ ...cell, borderTop: 'none' }}>
                      <LetterEditor cohortId={cohort.id} invitation={inv}
                        onCancel={() => setEditing(null)}
                        onDone={async () => { setEditing(null); await onChanged(); }} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
      <CircleConfirmDialog
        open={!!closeAsk}
        title={`Close the invitation for ${closeAsk ? personOf(closeAsk) : ''}?`}
        body={closeAsk && closeAsk.admitted_at
          ? 'They leave the group: they are taken out of every member\'s group circle and no longer read what the group shares. Their free period ends now. Loore tells nobody.'
          : 'The invitation ends now: it can no longer be accepted or continued, and the free period ends for the person who accepted it. A report they sent stays theirs to take back. Loore tells nobody.'}
        note="This can't be undone from this page."
        confirmLabel="Close invitation"
        busy={!!busyId}
        onClose={() => setCloseAsk(null)}
        onConfirm={async () => {
          const inv = closeAsk;
          setCloseAsk(null);
          await act(inv, 'close');
        }}
      />
    </div>
  );
}

export default function AdminCohorts() {
  const [cohorts, setCohorts] = useState(null);
  const [selected, setSelected] = useState(null); // a cohort id, or 'new'
  const [invitations, setInvitations] = useState([]);
  const [error, setError] = useState('');

  const loadCohorts = useCallback(() => api.get('/admin/cohorts')
    .then((res) => {
      const list = res.data.cohorts || [];
      setCohorts(list);
      setSelected((current) => current || (list.length ? list[0].id : 'new'));
      return list;
    })
    .catch((err) => setError(errorSentence(err, 'Cohorts could not be loaded.'))), []);

  useEffect(() => { loadCohorts(); }, [loadCohorts]);

  const cohort = cohorts && selected !== 'new'
    ? cohorts.find((c) => c.id === selected) || null : null;
  const cohortId = cohort ? cohort.id : null;

  const loadInvitations = useCallback(async () => {
    if (!cohortId) {
      setInvitations([]);
      return;
    }
    try {
      const res = await api.get(`/admin/cohorts/${cohortId}/invitations`);
      setInvitations(res.data.invitations || []);
    } catch (err) {
      setError(errorSentence(err, 'Invitations could not be loaded.'));
    }
  }, [cohortId]);

  useEffect(() => { loadInvitations(); }, [loadInvitations]);

  // Invitations change the counts on the cohort.
  const refresh = useCallback(async () => {
    await loadInvitations();
    await loadCohorts();
  }, [loadInvitations, loadCohorts]);

  const onSaved = useCallback((saved) => {
    setCohorts((list) => {
      const others = (list || []).filter((c) => c.id !== saved.id);
      return [...others, saved].sort((a, b) => a.id - b.id);
    });
    setSelected(saved.id);
  }, []);

  if (error && !cohorts) return <p role="alert" style={errorStyle}>{error}</p>;
  if (!cohorts) return <p style={noticeStyle}>Loading…</p>;

  const bubble = (active) => ({
    padding: '6px 14px', borderRadius: '16px', cursor: 'pointer',
    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
    background: active ? 'var(--bg-card)' : 'none',
    color: active ? 'var(--text-primary)' : 'var(--text-muted)',
    fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
  });

  return (
    <div>
      <p style={{ ...noticeStyle, marginBottom: '16px', maxWidth: '760px' }}>
        States and times only. Nobody's writing is shown here: a report reaches you
        under Shared with you, when its writer sends it.
      </p>
      {cohorts.length > 0 && (
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '20px' }}>
          {cohorts.map((c) => (
            <button key={c.id} type="button" style={bubble(selected === c.id)}
              onClick={() => setSelected(c.id)}>
              {c.name}
            </button>
          ))}
          <button type="button" style={bubble(selected === 'new')} onClick={() => setSelected('new')}>
            New cohort
          </button>
        </div>
      )}
      {error && <p role="alert" style={{ ...errorStyle, marginBottom: '12px' }}>{error}</p>}
      {/* Keyed by the cohort: its fields are read once when it is
          opened, so reloading the counts never drops unsaved edits. */}
      <CohortForm key={cohortId || 'new'} cohort={cohort} onSaved={onSaved} />
      {cohort && (
        <>
          <NewInvitation cohort={cohort} onAdded={refresh} />
          <div style={{ ...boxStyle, maxWidth: 'none' }}>
            <h2 style={h2Style}>Invitations</h2>
            <InvitationTable cohort={cohort} invitations={invitations} onChanged={refresh} />
          </div>
        </>
      )}
    </div>
  );
}
