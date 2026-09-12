import React from 'react';
import { footerStyle } from './NodeFooter';
import { formatDateTime } from '../utils/date';
import { authorLabel } from '../utils/references';

// The card footer for a saved reference: who, when, and where it came
// from. Same style as NodeFooter, none of its node-only affordances.
function ReferenceFooter({ item }) {
  const author = authorLabel(item);
  let host = null;
  try { host = item.url ? new URL(item.url).hostname.replace(/^www\./, '') : null; } catch (e) { /* keep null */ }
  const when = formatDateTime(item.posted_at || item.fetched_at);
  return (
    <div style={footerStyle}>
      {author && <span>{author}</span>}
      {author && <span style={{ color: 'var(--border)' }}>&middot;</span>}
      <span>{when}</span>
      {item.url && (
        <>
          <span style={{ color: 'var(--border)' }}>&middot;</span>
          <a
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            title="Open the original in a new tab"
            style={{ color: 'inherit', textDecoration: 'none' }}
          >
            {host || 'source'}
          </a>
        </>
      )}
    </div>
  );
}

export default ReferenceFooter;
