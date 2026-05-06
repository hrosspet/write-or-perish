import React from 'react';
import MarkdownBody from './MarkdownBody';
import NodeFooter from './NodeFooter';

/**
 * InlineQuoteBubble - A compact version of Bubble for displaying inline quotes.
 * Designed to fit within text flow while still being clickable.
 */
const InlineQuoteBubble = ({ quote, onClick }) => {
  if (!quote) {
    // Privacy-blocked or not found — vocabulary aligned with the rest of
    // the soft-delete plan (see Bubble.js / DeleteConfirmDialog).
    return (
      <div style={notAccessibleStyle}>
        [Quoted node inaccessible]
      </div>
    );
  }

  if (quote.deleted) {
    // Soft-deleted target the viewer had pre-deletion access to. Distinct
    // string from the privacy "inaccessible" case so the viewer can tell
    // "this was deleted" vs "I can't see this".
    return (
      <div style={notAccessibleStyle}>
        [Quoted node deleted]
      </div>
    );
  }

  const text = quote.content || "";
  // Use same length as Feed's Bubble component (250 chars)
  const truncatedText = text.length > 250 ? text.substring(0, 250) + "..." : text;

  return (
    <div style={bubbleStyle} onClick={(e) => onClick(quote.id, e)}>
      <div style={quoteHeaderStyle}>
        Quoted from @{quote.username}
      </div>
      <div style={contentStyle}>
        <MarkdownBody paragraphMargin="0">
          {truncatedText}
        </MarkdownBody>
      </div>
      <NodeFooter
        username={quote.username}
        createdAt={quote.created_at}
        childrenCount={0}
      />
    </div>
  );
};


const bubbleStyle = {
  display: 'block',
  padding: '12px',
  margin: '10px 0',
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderLeft: '3px solid var(--accent)',
  borderRadius: '6px',
  cursor: 'pointer',
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

export default InlineQuoteBubble;
