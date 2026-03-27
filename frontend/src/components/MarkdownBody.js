import React from 'react';
import ReactMarkdown from 'react-markdown';

/**
 * MarkdownBody — shared ReactMarkdown wrapper with consistent styling.
 *
 * Props:
 *   children: markdown string
 *   style: optional style object applied to the outer <div>
 *   paragraphMargin: optional margin for <p> elements (default: "0.5em 0")
 */
const MarkdownBody = ({ children, style, paragraphMargin = '0.5em 0' }) => (
  <div style={style}>
    <ReactMarkdown components={{
      p: ({ node, ...props }) => (
        <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word', margin: paragraphMargin }} {...props} />
      ),
      ul: ({ node, ...props }) => (
        <ul style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
      ),
      ol: ({ node, ...props }) => (
        <ol style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
      ),
      li: ({ node, ...props }) => (
        <li style={{ whiteSpace: 'normal', overflowWrap: 'break-word', marginBottom: '2px' }} {...props} />
      ),
      hr: ({ node, ...props }) => (
        <hr style={{ border: 'none', borderTop: '1px solid var(--border)', margin: '20px 0' }} {...props} />
      ),
      code: ({ node, inline, className, children, ...props }) =>
        inline ? (
          <code style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word', fontSize: '0.9em' }} {...props}>
            {children}
          </code>
        ) : (
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word', fontSize: '0.9em' }} {...props}>
            <code>{children}</code>
          </pre>
        ),
    }}>
      {children}
    </ReactMarkdown>
  </div>
);

export default MarkdownBody;
