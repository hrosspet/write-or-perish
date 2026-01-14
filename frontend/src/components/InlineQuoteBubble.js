import React from 'react';
import NodeFooter from './NodeFooter';

/**
 * InlineQuoteBubble - A compact version of Bubble for displaying inline quotes.
 * Designed to fit within text flow while still being clickable.
 */
const InlineQuoteBubble = ({ quote, onClick }) => {
  if (!quote) {
    // Quote not accessible or not found
    return (
      <div style={notAccessibleStyle}>
        [Quote not accessible]
      </div>
    );
  }

  const text = quote.content || "";
  const truncatedText = text.length > 150 ? text.substring(0, 150) + "..." : text;

  return (
    <div style={bubbleStyle} onClick={() => onClick(quote.id)}>
      <div style={quoteHeaderStyle}>
        Quoted from @{quote.username}
      </div>
      <div style={contentStyle}>
        {truncatedText}
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
  background: '#252525',
  border: '1px solid #444',
  borderLeft: '3px solid #61dafb',
  borderRadius: '6px',
  cursor: 'pointer',
  whiteSpace: 'pre-wrap',
  maxWidth: '100%',
  transition: 'background 0.2s ease',
};

const quoteHeaderStyle = {
  fontSize: '0.85em',
  color: '#888',
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
  background: '#333',
  border: '1px solid #555',
  borderRadius: '4px',
  color: '#888',
  fontStyle: 'italic',
  fontSize: '0.9em',
};

export default InlineQuoteBubble;
