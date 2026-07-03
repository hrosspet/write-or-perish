import React from 'react';
import MarkdownBody from './MarkdownBody';

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
 */
const ExternalQuoteBubble = ({ quote }) => {
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
    ? new Date(quote.posted_at).toLocaleDateString()
    : null;

  const open = () => {
    if (quote.url) window.open(quote.url, '_blank', 'noopener,noreferrer');
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
        {postedAt && <span>{postedAt}</span>}
        {quote.url && (
          <span style={linkStyle}>View original ↗</span>
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
  gap: '12px',
  fontSize: '0.8em',
  color: 'var(--text-muted)',
};

const linkStyle = {
  color: 'var(--accent)',
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
