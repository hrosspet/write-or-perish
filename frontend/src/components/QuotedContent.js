import React from 'react';
import ReactMarkdown from 'react-markdown';
import InlineQuoteBubble from './InlineQuoteBubble';

// Pattern to match {quote:123} placeholders
const QUOTE_PATTERN = /\{quote:(\d+)\}/g;

/**
 * QuotedContent - Renders content with inline quote previews.
 *
 * Parses the content for {quote:ID} placeholders and renders them
 * as clickable InlineQuoteBubble components.
 *
 * Props:
 *   content: The raw text content that may contain {quote:ID} placeholders
 *   quotes: Object mapping quote IDs to quote data (or null if not accessible)
 *   onQuoteClick: Callback when a quote is clicked (receives quote ID)
 */
const QuotedContent = ({ content, quotes, onQuoteClick }) => {
  if (!content) {
    return null;
  }

  // If no quotes data provided, just render the content as-is with markdown
  if (!quotes || Object.keys(quotes).length === 0) {
    return (
      <ReactMarkdown components={markdownComponents}>
        {content}
      </ReactMarkdown>
    );
  }

  // Parse content into segments: text and quote placeholders
  const segments = [];
  let lastIndex = 0;
  let match;

  // Reset the regex lastIndex for fresh matching
  QUOTE_PATTERN.lastIndex = 0;

  while ((match = QUOTE_PATTERN.exec(content)) !== null) {
    // Add text before this match
    if (match.index > lastIndex) {
      segments.push({
        type: 'text',
        content: content.substring(lastIndex, match.index),
      });
    }

    // Add the quote placeholder
    const quoteId = match[1];
    segments.push({
      type: 'quote',
      quoteId: quoteId,
    });

    lastIndex = match.index + match[0].length;
  }

  // Add remaining text after last match
  if (lastIndex < content.length) {
    segments.push({
      type: 'text',
      content: content.substring(lastIndex),
    });
  }

  // Render segments
  return (
    <div>
      {segments.map((segment, index) => {
        if (segment.type === 'text') {
          return (
            <ReactMarkdown key={index} components={markdownComponents}>
              {segment.content}
            </ReactMarkdown>
          );
        } else if (segment.type === 'quote') {
          const quoteData = quotes[segment.quoteId];
          return (
            <InlineQuoteBubble
              key={index}
              quote={quoteData}
              onClick={onQuoteClick}
            />
          );
        }
        return null;
      })}
    </div>
  );
};

// Markdown component overrides for consistent styling
const markdownComponents = {
  p: ({ node, ...props }) => (
    <p style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props} />
  ),
  code: ({ node, inline, className, children, ...props }) =>
    inline ? (
      <code style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props}>
        {children}
      </code>
    ) : (
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props}>
        <code>{children}</code>
      </pre>
    ),
  li: ({ node, ...props }) => (
    <li style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props} />
  ),
};

export default QuotedContent;
