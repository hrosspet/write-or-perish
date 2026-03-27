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
      code: ({ node, inline, className, children, ...props }) =>
        inline ? (
          <code style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word' }} {...props}>
            {children}
          </code>
        ) : (
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word' }} {...props}>
            <code>{children}</code>
          </pre>
        ),
      li: ({ node, ...props }) => (
        <li style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word' }} {...props} />
      ),
    }}>
      {children}
    </ReactMarkdown>
  </div>
);

export default MarkdownBody;
