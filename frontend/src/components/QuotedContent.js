import React from 'react';
import MarkdownBody from './MarkdownBody';
import InlineQuoteBubble from './InlineQuoteBubble';
import InlineArtifactSection from './InlineArtifactSection';

// Pattern to match {user_profile}, {user_todo}, {user_recent}, {user_recent_raw}, {user_ai_preferences} placeholders
// NOTE: recent_raw must come before recent in the alternation to avoid partial matching
const ARTIFACT_PATTERN = /\{user_(profile|todo|recent_raw|recent|ai_preferences)\}/g;
// Combined pattern for splitting (quotes + artifacts)
const COMBINED_PATTERN = /(\{quote:\d+\}|\{user_(?:profile|todo|recent_raw|recent|ai_preferences)\})/g;

/**
 * QuotedContent - Renders content with inline quote previews.
 *
 * Parses the content for {quote:ID} placeholders and renders them
 * as clickable InlineQuoteBubble components.
 *
 * Props:
 *   content: The raw text content that may contain {quote:ID} / {user_profile} / {user_todo} placeholders
 *   quotes: Object mapping quote IDs to quote data (or null if not accessible)
 *   contextArtifacts: Object with "profile" and/or "todo" keys containing artifact data
 *   onQuoteClick: Callback when a quote is clicked (receives quote ID)
 */
const QuotedContent = ({ content, quotes, contextArtifacts, onQuoteClick }) => {
  if (!content) {
    return null;
  }

  const hasQuotes = quotes && Object.keys(quotes).length > 0;
  const hasArtifacts = contextArtifacts && Object.keys(contextArtifacts).length > 0;

  // If no special data provided, just render the content as-is with markdown
  if (!hasQuotes && !hasArtifacts && !ARTIFACT_PATTERN.test(content)) {
    return (
      <MarkdownBody>
        {content}
      </MarkdownBody>
    );
  }

  // Parse content into segments using the combined pattern
  const segments = [];
  const parts = content.split(COMBINED_PATTERN);

  for (const part of parts) {
    if (!part) continue;

    // Check if this part is a quote placeholder
    const quoteMatch = part.match(/^\{quote:(\d+)\}$/);
    if (quoteMatch) {
      segments.push({ type: 'quote', quoteId: quoteMatch[1] });
      continue;
    }

    // Check if this part is an artifact placeholder
    const artifactMatch = part.match(/^\{user_(profile|todo|recent_raw|recent|ai_preferences)\}$/);
    if (artifactMatch) {
      segments.push({ type: 'artifact', artifactType: artifactMatch[1] });
      continue;
    }

    // Regular text
    segments.push({ type: 'text', content: part });
  }

  // Render segments
  return (
    <div>
      {segments.map((segment, index) => {
        if (segment.type === 'text') {
          return (
            <MarkdownBody key={index}>
              {segment.content}
            </MarkdownBody>
          );
        } else if (segment.type === 'quote') {
          const quoteData = quotes ? quotes[segment.quoteId] : null;
          return (
            <InlineQuoteBubble
              key={index}
              quote={quoteData}
              onClick={onQuoteClick}
            />
          );
        } else if (segment.type === 'artifact') {
          const artifact = contextArtifacts
            ? contextArtifacts[segment.artifactType]
            : null;
          return (
            <InlineArtifactSection
              key={index}
              type={segment.artifactType}
              artifact={artifact}
            />
          );
        }
        return null;
      })}
    </div>
  );
};


export default QuotedContent;
