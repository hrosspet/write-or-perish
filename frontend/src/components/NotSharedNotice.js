import React from 'react';
import { Link } from 'react-router-dom';

// An entry the signed-in person may not open (#704): made private since,
// never shared with them, or deleted. The server answers all three, and a
// missing id, with the same 404, so nobody can find out which ids exist;
// these words are the same for all of them too. They say plainly that the
// entry is not open to this account (not a broken link) and offer the way
// on to the person's own Home page.
export const NOT_SHARED_TITLE = "This entry isn't shared with you";
export const NOT_SHARED_TEXT = 'It may be private, or it may have been deleted.';

export default function NotSharedNotice() {
  return (
    <div role="status" style={{
      maxWidth: '520px', margin: '18vh auto 0', padding: '0 24px',
      textAlign: 'center',
    }}>
      <h1 style={{
        fontFamily: 'var(--serif)', fontWeight: 300, fontSize: '1.6rem',
        color: 'var(--text-primary)', margin: '0 0 0.75rem', lineHeight: 1.3,
      }}>
        {NOT_SHARED_TITLE}
      </h1>
      <p style={{
        fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.95rem',
        color: 'var(--text-secondary)', lineHeight: 1.7, margin: '0 0 1.75rem',
      }}>
        {NOT_SHARED_TEXT}
      </p>
      <Link to="/" style={{
        fontFamily: 'var(--sans)', fontWeight: 400, fontSize: '0.9rem',
        color: 'var(--accent)', textDecoration: 'none', letterSpacing: '0.02em',
      }}>
        Go to your Home page &rarr;
      </Link>
    </div>
  );
}
