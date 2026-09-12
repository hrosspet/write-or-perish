import React, { useEffect, useState } from 'react';
import api from '../api';
import MarkdownBody from './MarkdownBody';
import { formatDate } from '../utils/date';
import { useUser } from '../contexts/UserContext';
import { useToast } from '../contexts/ToastContext';

const SOURCE_LABELS = {
  community_archive: 'Community Archive',
  twitter_bookmark: 'X bookmark',
  twitter_like: 'X like',
};

/**
 * ExternalQuoteBubble - An inline quote of a saved external reference
 * (tweet/bookmark) — the quote-as-response rendering. Content comes
 * verbatim from the server-resolved external item, never from LLM text.
 * Clicking opens the original post.
 *
 * The reference's owner also gets the same read toggle as the reference
 * page (POST/DELETE /external/items/<id>/read), so a recommendation can
 * be marked read right where Loore surfaced it. Only the user marks a
 * reference read — the AI quoting it is tracked separately as surfacing.
 */
const ExternalQuoteBubble = ({ quote }) => {
  const userCtx = useUser();
  const currentUser = userCtx ? userCtx.user : null;
  const { addToast } = useToast();
  const [readAt, setReadAt] = useState(quote ? quote.read_at : null);
  const [marking, setMarking] = useState(false);
  const serverReadAt = quote ? quote.read_at : null;
  useEffect(() => { setReadAt(serverReadAt); }, [serverReadAt]);

  if (!quote) {
    return (
      <div style={notAccessibleStyle}>
        [Quoted reference inaccessible]
      </div>
    );
  }

  const text = quote.content || '';
  const truncatedText = text.length > 500 ? text.substring(0, 500) + '...' : text;
  const sourceLabel = SOURCE_LABELS[quote.source] || quote.source;
  const postedAt = quote.posted_at
    ? formatDate(quote.posted_at, { relative: false })
    : null;
  const mine = !!currentUser && quote.user_id === currentUser.id;

  const open = () => {
    if (quote.url) window.open(quote.url, '_blank', 'noopener,noreferrer');
  };

  const toggleRead = (e) => {
    e.stopPropagation();
    setMarking(true);
    const req = readAt
      ? api.delete(`/external/items/${quote.id}/read`)
      : api.post(`/external/items/${quote.id}/read`);
    req
      .then((res) => setReadAt(res.data.read_at))
      .catch(() => addToast('Could not update the read mark.', 4000))
      .finally(() => setMarking(false));
  };

  return (
    <div
      style={{ ...bubbleStyle, cursor: quote.url ? 'pointer' : 'default' }}
      onClick={open}
    >
      <div style={quoteHeaderStyle}>
        Saved from @{quote.author_handle || 'unknown'} · {sourceLabel}
      </div>
      <div style={contentStyle}>
        <MarkdownBody paragraphMargin="0">
          {truncatedText}
        </MarkdownBody>
      </div>
      <div style={footerStyle}>
        <span>{postedAt}</span>
        {mine && (
          <span style={readSlotStyle}>
            {readAt && (
              <span style={readTagStyle} title={`You read it ${formatDate(readAt)}`}>
                Read
              </span>
            )}
            <button
              type="button"
              className="ext-quote-read-toggle"
              onClick={toggleRead}
              disabled={marking}
            >
              {readAt ? 'Mark as unread' : 'Mark as read'}
            </button>
          </span>
        )}
      </div>
    </div>
  );
};

const bubbleStyle = {
  display: 'block',
  padding: '12px',
  margin: '10px 0',
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderLeft: '3px solid var(--info)',
  borderRadius: '6px',
  whiteSpace: 'pre-wrap',
  maxWidth: '100%',
  transition: 'background 0.2s ease',
};

const quoteHeaderStyle = {
  fontSize: '0.85em',
  color: 'var(--text-muted)',
  marginBottom: '6px',
  fontStyle: 'italic',
};

const contentStyle = {
  fontSize: '0.95em',
  lineHeight: '1.4',
  marginBottom: '8px',
};

const footerStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  flexWrap: 'wrap',
  gap: '8px 12px',
  fontSize: '0.8em',
  color: 'var(--text-muted)',
};

const readSlotStyle = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '10px',
  marginLeft: 'auto',
};

// Same "Read" mark as the reference page, scaled to the footer.
const readTagStyle = {
  fontFamily: 'var(--sans)',
  fontSize: '0.8em',
  fontWeight: 500,
  textTransform: 'uppercase',
  letterSpacing: '0.08em',
  color: 'var(--accent-dim)',
};

const notAccessibleStyle = {
  display: 'inline-block',
  padding: '4px 8px',
  margin: '4px 0',
  background: 'var(--bg-surface)',
  border: '1px solid var(--border-hover)',
  borderRadius: '4px',
  color: 'var(--text-muted)',
  fontStyle: 'italic',
  fontSize: '0.9em',
};

export default ExternalQuoteBubble;
