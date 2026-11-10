import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import { CircleConfirmDialog } from '../components/CircleDialogs';
import { parseTimestamp } from '../utils/date';
import { possessive } from '../utils/circles';

// "Shared with you": the entries people chose to show the user, grouped
// by person, newest first. Nothing here counts or tracks reading: no
// unread marks, no numbers. At the bottom, quietly, the people whose
// entries the user chose not to see, with the way back.

const quietButton = {
  minHeight: '44px', padding: '0 6px', border: 0, background: 'transparent',
  color: 'var(--text-secondary)', fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '0.8rem', cursor: 'pointer',
};

function pad2(n) { return String(n).padStart(2, '0'); }

// "Today, 9:40" / "28 October" / "28 October 2025".
export function sharedDate(iso, now = new Date()) {
  if (!iso) return '';
  const d = parseTimestamp(iso);
  if (isNaN(d.getTime())) return '';
  if (d.toDateString() === now.toDateString()) {
    return `Today, ${d.getHours()}:${pad2(d.getMinutes())}`;
  }
  const month = d.toLocaleDateString('en-GB', { month: 'long' });
  const year = d.getFullYear() === now.getFullYear() ? '' : ` ${d.getFullYear()}`;
  return `${d.getDate()} ${month}${year}`;
}

function SharedWithYouPage() {
  const { user } = useUser();
  const [people, setPeople] = useState(null);
  const [hidden, setHidden] = useState([]);
  const [nextBefore, setNextBefore] = useState(null);
  const [error, setError] = useState('');
  const [hiding, setHiding] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback((before) => {
    api.get('/circles/shared-with-me', { params: before ? { before } : {} })
      .then((res) => {
        setHidden(res.data.hidden || []);
        setNextBefore(res.data.next_before || null);
        setPeople((current) => {
          if (!before || !current) return res.data.people || [];
          // Older entries join their person's group, or open a new one.
          const merged = current.map((p) => ({ ...p, entries: [...p.entries] }));
          (res.data.people || []).forEach((p) => {
            const group = merged.find((m) => m.username === p.username);
            if (group) group.entries.push(...p.entries);
            else merged.push(p);
          });
          return merged;
        });
      })
      .catch(() => setError(
        'This page could not be loaded. Reload it to try again.'));
  }, []);

  useEffect(() => {
    document.title = 'Shared with you · Loore';
    if (user?.circles_enabled) load();
  }, [user?.circles_enabled, load]);

  if (user && !user.circles_enabled) {
    return (
      <div style={{ padding: '120px 24px', textAlign: 'center' }}>
        <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>
          Not available.
        </p>
      </div>
    );
  }

  const hide = async (username) => {
    setBusy(true);
    try {
      await api.post('/circles/mutes', { username });
      setPeople(null);
      load();
    } catch (err) {
      setError(err.response?.data?.error || 'That did not work. Try again.');
    } finally {
      setBusy(false);
      setHiding(null);
    }
  };

  const show = async (username) => {
    try {
      await api.delete(`/circles/mutes/${encodeURIComponent(username)}`);
      setPeople(null);
      load();
    } catch (err) {
      setError(err.response?.data?.error || 'That did not work. Try again.');
    }
  };

  const meta = (entry) => {
    const when = sharedDate(entry.created_at);
    if (!entry.last_reply_by) return when;
    const who = entry.last_reply_by_me ? 'You' : entry.last_reply_by;
    return `${when}. ${who} replied.`;
  };

  return (
    <div style={{
      padding: '3rem 2rem 4rem', maxWidth: '760px', margin: '0 auto',
      fontFamily: 'var(--sans)', fontWeight: 300, color: 'var(--text-primary)',
      display: 'flex', flexDirection: 'column', gap: '30px',
      minHeight: 'calc(100vh - 60px)', boxSizing: 'border-box',
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <h1 style={{
          margin: 0, fontFamily: 'var(--serif)', fontWeight: 500,
          fontSize: '2.25rem', lineHeight: 1.15,
        }}>
          Shared with you
        </h1>
        <p style={{
          margin: 0, fontSize: '0.94rem', lineHeight: 1.55,
          color: 'var(--text-secondary)', maxWidth: '560px',
        }}>
          Entries that people chose to show you. They stay here; nothing
          is added to your own log or your profile.
        </p>
        <Link to="/circles" style={{ fontSize: '0.875rem', color: 'var(--accent)', textDecoration: 'none' }}>
          Your circles
        </Link>
      </div>

      {error && (
        <div role="alert" style={{ fontSize: '0.875rem', color: 'var(--error)' }}>
          {error}
        </div>
      )}
      {people === null && !error && (
        <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>Loading…</div>
      )}
      {people !== null && people.length === 0 && (
        <div style={{ fontSize: '0.94rem', lineHeight: 1.55, color: 'var(--text-secondary)' }}>
          Nothing is shared with you right now.
        </div>
      )}

      {(people || []).map((person) => (
        <section key={person.username}
          style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div style={{
            display: 'flex', justifyContent: 'space-between',
            alignItems: 'baseline', gap: '12px',
            borderBottom: '1px solid var(--border)', paddingBottom: '8px',
          }}>
            <h2 style={{
              margin: 0, fontFamily: 'var(--sans)', fontWeight: 400,
              fontSize: '0.94rem', color: 'var(--accent)',
            }}>
              {person.username}
            </h2>
            <button type="button" style={quietButton}
              onClick={() => setHiding(person.username)}>
              Stop showing {possessive(person.username)} entries
            </button>
          </div>
          {person.entries.map((entry) => (
            <Link key={entry.id} to={`/node/${entry.id}`} style={{
              textDecoration: 'none', display: 'flex',
              flexDirection: 'column', gap: '6px', minHeight: '44px',
              color: 'var(--text-primary)',
            }}>
              <span style={{
                fontFamily: 'var(--serif)', fontWeight: 500,
                fontSize: '1.5rem', lineHeight: 1.25,
              }}>
                {entry.title || 'Untitled entry'}
              </span>
              {entry.preview && (
                <span style={{ fontSize: '0.94rem', lineHeight: 1.55, color: 'var(--text-secondary)' }}>
                  {entry.preview}
                </span>
              )}
              <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                {meta(entry)}
              </span>
            </Link>
          ))}
        </section>
      ))}

      {nextBefore && (
        <button type="button" onClick={() => load(nextBefore)}
          style={{
            alignSelf: 'flex-start', minHeight: '46px', padding: '0 20px',
            borderRadius: '6px', border: '1px solid var(--border-hover)',
            background: 'transparent', color: 'var(--text-primary)',
            fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.94rem',
            cursor: 'pointer',
          }}>
          Show older entries
        </button>
      )}

      <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: '18px' }}>
        <p style={{
          margin: 0, fontFamily: 'var(--serif)', fontStyle: 'italic',
          fontSize: '1.125rem', color: 'var(--text-secondary)',
        }}>
          Nobody sees whether you have read an entry.
        </p>
        {hidden.length > 0 && (
          <section aria-label="People you have hidden" style={{
            borderTop: '1px solid var(--border)', paddingTop: '14px',
            display: 'flex', flexDirection: 'column', gap: '4px',
          }}>
            <h2 style={{
              margin: 0, fontFamily: 'var(--sans)', fontWeight: 400,
              fontSize: '0.7rem', letterSpacing: '0.14em',
              textTransform: 'uppercase', color: 'var(--text-secondary)',
            }}>
              People you have hidden
            </h2>
            <p style={{ margin: 0, fontSize: '0.8rem', lineHeight: 1.5, color: 'var(--text-secondary)' }}>
              You get no notes and see no entries from them. They are not told.
            </p>
            {hidden.map((name) => (
              <div key={name} style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', gap: '12px', minHeight: '44px',
              }}>
                <span style={{ fontSize: '0.94rem' }}>{name}</span>
                <button type="button" style={quietButton} onClick={() => show(name)}>
                  Show {possessive(name)} entries again
                </button>
              </div>
            ))}
          </section>
        )}
      </div>

      <CircleConfirmDialog
        open={!!hiding}
        title={`Stop showing ${hiding ? possessive(hiding) : ''} entries?`}
        body={`You will get no notes about ${hiding ? possessive(hiding) : ''} entries and will not see them here. ${hiding} is not told what you choose. You can undo this at the bottom of this page.`}
        confirmLabel={`Don't show ${hiding ? possessive(hiding) : ''} entries`}
        busy={busy}
        onClose={() => setHiding(null)}
        onConfirm={() => hide(hiding)}
      />
    </div>
  );
}

export default SharedWithYouPage;
