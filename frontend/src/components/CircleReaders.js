import React, { useEffect, useId, useRef, useState } from 'react';

// Two overlapping rings, stroke only: the mark of a shared entry.
export function CirclesIcon({ size = 18, color = 'var(--accent)' }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 20 20"
      fill="none" stroke={color} strokeWidth="1.4">
      <circle cx="7.5" cy="10" r="5" />
      <circle cx="12.5" cy="10" r="5" />
    </svg>
  );
}

// A ring around a dot: the banner's mark.
export function SharedMark({ size = 20, color = 'var(--accent)' }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 20 20"
      fill="none" stroke={color} strokeWidth="1.4"
      style={{ flex: 'none', marginTop: '2px' }}>
      <circle cx="10" cy="10" r="7.5" />
      <circle cx="10" cy="10" r="2.5" />
    </svg>
  );
}

/**
 * The circles icon after a shared entry's author and time. Pointing at
 * it, focusing it or tapping it lists everyone who can read the entry
 * and its replies: people by name, never the circle's name.
 */
function CircleReaders({ audience, myUsername }) {
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const wrapRef = useRef(null);
  const tipId = useId();
  const open = hover || pinned;

  useEffect(() => {
    if (!pinned) return undefined;
    const onDown = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        setPinned(false);
      }
    };
    const onKey = (e) => { if (e.key === 'Escape') setPinned(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [pinned]);

  if (!audience) return null;
  const line = (name) => (name === myUsername ? 'You' : name);
  const people = [
    audience.author === myUsername
      ? 'You, the author' : `${audience.author}, the author`,
    ...(audience.readers || []).filter((n) => n !== myUsername),
    ...((audience.readers || []).includes(myUsername) ? ['You'] : []),
  ];

  return (
    <span
      ref={wrapRef}
      style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        type="button"
        aria-label="Who can read this"
        aria-describedby={open ? tipId : undefined}
        aria-expanded={open}
        onClick={(e) => { e.stopPropagation(); setPinned((v) => !v); }}
        onFocus={() => setHover(true)}
        onBlur={() => setHover(false)}
        style={{
          width: '44px', height: '44px', margin: '-14px -8px',
          border: 0, background: 'transparent', padding: 0,
          display: 'inline-flex', alignItems: 'center',
          justifyContent: 'center', cursor: 'pointer',
        }}
      >
        <CirclesIcon />
      </button>
      {open && (
        <span
          id={tipId}
          role="tooltip"
          style={{
            position: 'absolute', left: 0, top: '26px', zIndex: 20,
            width: '230px', boxSizing: 'border-box',
            background: 'var(--bg-card-hover)',
            border: '1px solid var(--border-hover)',
            borderRadius: '8px', padding: '12px 14px',
            display: 'flex', flexDirection: 'column', gap: '4px',
            fontFamily: 'var(--sans)', fontSize: '0.875rem', fontWeight: 300,
            lineHeight: 1.45, color: 'var(--text-primary)',
            boxShadow: '0 8px 24px rgba(0,0,0,0.25)',
          }}
        >
          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
            Can read this entry and its replies
          </span>
          {people.map((p) => <span key={p}>{line(p)}</span>)}
        </span>
      )}
    </span>
  );
}

export default CircleReaders;
