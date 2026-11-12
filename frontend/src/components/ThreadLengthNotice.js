import React from 'react';
import { Link } from 'react-router-dom';
import {
  NEW_THREAD_PATH, THREAD_LIMIT_TEXT, THREAD_NOTICE_TEXT,
} from '../utils/threadLength';

/**
 * Above the reply form of a long thread (#648). *state* is the thread's
 * `thread_length`: "notice" from 90 % of the limit (the thread is getting
 * long, AI replies will stop soon), "limit" at it (no AI reply here; a new
 * thread). Writing in the thread still works either way. The pattern of
 * the spend cap's notice (SpendCapBanner: an uppercase accent label before
 * the text) and the outlined accent button of DataDeletionBanner. No
 * price, no token numbers. *message*: the server's text, when it sent one
 * (`thread_length_message`: the thread's length, or an entry or what it
 * quotes too large for a reply).
 */
export default function ThreadLengthNotice({ state, message, newThreadPath = NEW_THREAD_PATH }) {
  if (state !== 'notice' && state !== 'limit') return null;
  const atLimit = state === 'limit';
  return (
    <div
      role="status"
      aria-label={atLimit ? 'Limit reached' : 'Long thread'}
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        columnGap: 16,
        rowGap: 8,
        padding: '10px 14px',
        marginBottom: 10,
        background: atLimit ? 'var(--accent-subtle)' : 'transparent',
        border: '1px solid var(--border)',
        borderRadius: 8,
        boxSizing: 'border-box',
      }}
    >
      <span style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.7rem',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        color: atLimit ? 'var(--accent)' : 'var(--text-muted)',
        whiteSpace: 'nowrap',
      }}>
        {atLimit ? 'Limit reached' : 'Long thread'}
      </span>
      <span style={{
        flex: '1 1 260px',
        fontFamily: 'var(--sans)',
        fontSize: '0.88rem',
        fontWeight: 300,
        lineHeight: 1.5,
        color: 'var(--text-secondary)',
      }}>
        {atLimit ? (message || THREAD_LIMIT_TEXT) : THREAD_NOTICE_TEXT}
      </span>
      <Link
        to={newThreadPath}
        style={{
          padding: '8px 18px',
          background: 'transparent',
          border: `1px solid ${atLimit ? 'var(--accent)' : 'var(--border)'}`,
          borderRadius: 6,
          color: atLimit ? 'var(--accent)' : 'var(--text-secondary)',
          fontFamily: 'var(--sans)',
          fontSize: '0.85rem',
          textDecoration: 'none',
          whiteSpace: 'nowrap',
        }}
      >
        Start a new thread
      </Link>
    </div>
  );
}
