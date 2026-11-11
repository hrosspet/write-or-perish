import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../api';
import { readersSentence, splitNames } from '../utils/circles';

/**
 * The panel under "Who can read this" when Circles is chosen: the
 * author's circles as toggles, a field for Loore names "for this entry
 * only", and the sentence that names the readers before saving.
 *
 * Names are looked up when the field is left or a separator is typed,
 * not on every keystroke: the lookup finds exact names only, and half a
 * name is a name nobody has.
 *
 * onResolved({ readers, error, pending, circles }) tells the form who
 * would read the entry, for its button and its check before saving.
 */
function CircleAudiencePicker({
  circleIds, namesText, onChange, onResolved, disabled = false,
  // { checked, onChange }: the entry has something above it in its
  // thread, which is shared only when this box is ticked.
  pathShare = null,
}) {
  const [circles, setCircles] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [committed, setCommitted] = useState(() => splitNames(namesText));
  const [readers, setReaders] = useState([]);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const requestRef = useRef(0);
  const onResolvedRef = useRef(onResolved);
  useEffect(() => { onResolvedRef.current = onResolved; });

  useEffect(() => {
    let alive = true;
    api.get('/circles')
      .then((res) => { if (alive) setCircles(res.data.circles || []); })
      .catch(() => {
        if (alive) {
          setCircles([]);
          setLoadError('Your circles could not be loaded.');
        }
      });
    return () => { alive = false; };
  }, []);

  const idsKey = (circleIds || []).join(',');
  const namesKey = committed.join(',');
  useEffect(() => {
    const ids = idsKey ? idsKey.split(',').map(Number) : [];
    const names = namesKey ? namesKey.split(',') : [];
    if (ids.length === 0 && names.length === 0) {
      setReaders([]);
      setError('');
      setPending(false);
      return undefined;
    }
    const request = ++requestRef.current;
    setPending(true);
    const timer = setTimeout(() => {
      api.post('/circles/audience-preview', { circle_ids: ids, usernames: names })
        .then((res) => {
          if (request !== requestRef.current) return;
          setReaders(res.data.readers || []);
          setError('');
        })
        .catch((err) => {
          if (request !== requestRef.current) return;
          setReaders([]);
          setError(err.response?.data?.error
            || 'The readers could not be worked out. Try again.');
        })
        .finally(() => {
          if (request === requestRef.current) setPending(false);
        });
    }, 200);
    return () => clearTimeout(timer);
  }, [idsKey, namesKey]);

  useEffect(() => {
    onResolvedRef.current?.({ readers, error, pending, circles: circles || [] });
  }, [readers, error, pending, circles]);

  const toggle = (id) => {
    const has = (circleIds || []).includes(id);
    onChange({
      circleIds: has ? circleIds.filter((c) => c !== id) : [...(circleIds || []), id],
      namesText,
    });
  };

  const onNames = (text) => {
    onChange({ circleIds, namesText: text });
    // A finished name: everything before the last separator.
    if (/[,\s]$/.test(text) || text === '') setCommitted(splitNames(text));
  };

  const chip = (selected) => ({
    minHeight: '44px', padding: '0 16px', borderRadius: '22px',
    border: `1px solid ${selected ? 'var(--accent)' : 'var(--border-hover)'}`,
    background: selected ? 'var(--accent)' : 'transparent',
    color: selected ? 'var(--bg-deep)' : 'var(--text-primary)',
    fontFamily: 'var(--sans)', fontWeight: selected ? 400 : 300,
    fontSize: '0.94rem', display: 'flex', alignItems: 'center', gap: '8px',
    cursor: disabled ? 'default' : 'pointer',
  });

  const nothingChosen = (circleIds || []).length === 0 && committed.length === 0;

  return (
    <div style={{
      background: 'var(--bg-surface)', border: '1px solid var(--border)',
      borderRadius: '8px', padding: '18px', marginTop: '10px',
      display: 'flex', flexDirection: 'column', gap: '16px',
    }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
        {circles === null && (
          <span style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            Loading your circles…
          </span>
        )}
        {(circles || []).map((c) => {
          const selected = (circleIds || []).includes(c.id);
          return (
            <button
              key={c.id} type="button" aria-pressed={selected}
              disabled={disabled} onClick={() => toggle(c.id)}
              style={chip(selected)}
            >
              {selected && (
                <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14"
                  fill="none" stroke="var(--bg-deep)" strokeWidth="1.8"
                  strokeLinecap="round" strokeLinejoin="round">
                  <path d="M2.5 7.5l3 3 6-7" />
                </svg>
              )}
              {c.name}
            </button>
          );
        })}
        {circles !== null && circles.length === 0 && !loadError && (
          <span style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            You have no circles yet.
          </span>
        )}
        {/* A new tab: the entry being written stays where it is. */}
        <Link
          to="/circles" target="_blank" rel="noopener"
          style={{
            minHeight: '44px', display: 'flex', alignItems: 'center',
            padding: '0 8px', fontSize: '0.875rem', textDecoration: 'none',
            color: 'var(--accent)', fontFamily: 'var(--sans)',
          }}
        >
          {circles && circles.length === 0 ? 'Make a circle' : 'Edit circles'}
        </Link>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <label htmlFor="one-off-people" style={{
          fontSize: '0.8rem', color: 'var(--text-secondary)',
          fontFamily: 'var(--sans)', fontWeight: 300,
        }}>
          Or name people for this entry only
        </label>
        <input
          id="one-off-people" type="text" value={namesText}
          placeholder="@name, @name"
          autoCapitalize="none" autoCorrect="off" spellCheck={false}
          disabled={disabled}
          onChange={(e) => onNames(e.target.value)}
          onBlur={() => setCommitted(splitNames(namesText))}
          style={{
            minHeight: '46px', boxSizing: 'border-box', padding: '0 14px',
            borderRadius: '8px', border: '1px solid var(--border-hover)',
            background: 'var(--bg-input)', color: 'var(--text-primary)',
            fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '1rem',
            width: '100%',
          }}
        />
      </div>
      <div aria-live="polite" style={{
        fontFamily: 'var(--serif)', fontSize: '1.3rem', lineHeight: 1.4,
        color: error || loadError ? 'var(--error)' : 'var(--text-primary)',
        minHeight: '1.4em',
      }}>
        {loadError || error
          || (nothingChosen
            ? 'Choose a circle or name a person. Until then nobody but you can read this entry.'
            : readers.length > 0
              ? readersSentence(readers)
              : pending
                ? 'Looking up who can read this…'
                : 'Nobody is in this circle yet. Add a person to it, or name someone.')}
      </div>
      {pathShare && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
          <label style={{
            display: 'flex', alignItems: 'center', gap: '12px', minHeight: '44px',
            fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.94rem',
            color: 'var(--text-primary)', cursor: disabled ? 'default' : 'pointer',
          }}>
            <input
              type="checkbox"
              checked={!!pathShare.checked}
              disabled={disabled}
              onChange={(e) => pathShare.onChange(e.target.checked)}
              style={{ width: '20px', height: '20px', accentColor: 'var(--accent)', margin: 0 }}
            />
            Share also the discussion leading up to this node?
          </label>
          <div style={{
            fontSize: '0.8rem', lineHeight: 1.5, color: 'var(--text-secondary)',
            fontFamily: 'var(--sans)', fontWeight: 300,
          }}>
            Everything from the start of this thread down to this entry
            is then shared with the same people, including the thread's
            prompt and the AI replies on the way. People's AI replies use
            the prompt with their own artifacts, never yours. Without it,
            only this entry is shared.
          </div>
        </div>
      )}
      <div style={{
        fontSize: '0.8rem', lineHeight: 1.5, color: 'var(--text-secondary)',
        fontFamily: 'var(--sans)', fontWeight: 300,
      }}>
        You can change this later, or take the entry back to only you.
      </div>
    </div>
  );
}

export default CircleAudiencePicker;
