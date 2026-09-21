import React, { useEffect, useState } from 'react';
import api from '../api';
import MarkdownBody from './MarkdownBody';
import { formatDate } from '../utils/date';
import { useUser } from '../contexts/UserContext';
import { useToast } from '../contexts/ToastContext';
import ReferenceFeedback from './ReferenceFeedback';

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
 * be marked read right where Loore surfaced it. The label alone carries
 * the state ("Mark as unread" = read). Only the user marks a
 * reference read — the AI quoting it is tracked separately as surfacing.
 * Beside it, the good/bad-quote verdict (ReferenceFeedback) — the
 * hit-or-miss half of the recommendation record. `onReadChange(id,
 * readAt)` and `onFeedbackChange(id, feedback)` tell the page, which
 * keeps the list's marks (a read reply's "n unread", "nothing marked
 * yet") in step with the bubbles.
 *
 * Opening the post is reading it: the owner's click marks the reference
 * read as the tab opens, and "Mark as unread" stays for the ones to come
 * back to (a long post opened for later). Only the default flipped; the
 * toggle is the override.
 */
const ExternalQuoteBubble = ({ quote, onReadChange, onFeedbackChange }) => {
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

  const setRead = (want) => {
    setMarking(true);
    const req = want
      ? api.post(`/external/items/${quote.id}/read`)
      : api.delete(`/external/items/${quote.id}/read`);
    req
      .then((res) => {
        setReadAt(res.data.read_at);
        if (onReadChange) onReadChange(quote.id, res.data.read_at);
      })
      .catch(() => addToast('Could not update the read mark.', 4000))
      .finally(() => setMarking(false));
  };

  const open = () => {
    if (!quote.url) return;
    // window.open first: the popup rules want the user gesture.
    window.open(quote.url, '_blank', 'noopener,noreferrer');
    if (mine && !readAt && !marking) setRead(true);
  };

  const toggleRead = (e) => {
    e.stopPropagation();
    setRead(!readAt);
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
          <span style={ownerSlotStyle}>
            <ReferenceFeedback
              itemId={quote.id}
              feedback={quote.feedback}
              onChange={(fb) => { if (onFeedbackChange) onFeedbackChange(quote.id, fb); }}
            />
            <button
              type="button"
              className="ext-quote-read-toggle"
              data-read={readAt ? 'true' : 'false'}
              onClick={toggleRead}
              disabled={marking}
              title={readAt ? `You read it ${formatDate(readAt)}` : undefined}
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

const ownerSlotStyle = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '14px',
  marginLeft: 'auto',
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
