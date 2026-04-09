import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Extract plain text from React children recursively.
 */
function extractText(children) {
  let text = '';
  React.Children.forEach(children, child => {
    if (typeof child === 'string') {
      text += child;
    } else if (child && child.props && child.props.children) {
      text += extractText(child.props.children);
    }
  });
  return text;
}

/**
 * MarkdownBody — shared ReactMarkdown wrapper with consistent styling.
 *
 * Props:
 *   children: markdown string
 *   style: optional style object applied to the outer <div>
 *   paragraphMargin: optional margin for <p> elements (default: "0.5em 0")
 *   onCheckboxToggle: optional callback(lineText, currentChecked) for clickable checkboxes
 */
const MarkdownBody = ({ children, style, paragraphMargin = '0.5em 0', onCheckboxToggle }) => {
  const components = {
    p: ({ node, ...props }) => (
      <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word', margin: paragraphMargin }} {...props} />
    ),
    ul: ({ node, ...props }) => (
      <ul style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
    ),
    ol: ({ node, ...props }) => (
      <ol style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
    ),
    li: ({ node, children: liChildren, ...props }) => {
      const isTask = props.className === 'task-list-item';

      if (isTask && onCheckboxToggle) {
        // Find the checkbox input among children to get checked state
        let checked = false;
        const filteredChildren = React.Children.map(liChildren, child => {
          if (child && child.props && child.props.type === 'checkbox') {
            checked = !!child.props.checked;
            // Replace default checkbox with round toggle
            return (
              <span
                onClick={(e) => {
                  e.preventDefault();
                  const text = extractText(liChildren).trim();
                  onCheckboxToggle(text, checked);
                }}
                role="checkbox"
                aria-checked={checked}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    const text = extractText(liChildren).trim();
                    onCheckboxToggle(text, checked);
                  }
                }}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  width: '18px',
                  height: '18px',
                  borderRadius: '50%',
                  border: `1.5px solid ${checked ? 'var(--accent-dim)' : 'var(--border-hover)'}`,
                  background: checked ? 'var(--accent-dim)' : 'none',
                  flexShrink: 0,
                  marginRight: '8px',
                  fontSize: '0.6rem',
                  color: 'var(--bg-deep)',
                  fontWeight: 600,
                  transition: 'all 0.3s',
                  cursor: 'pointer',
                  verticalAlign: 'middle',
                }}
              >
                {checked && '✓'}
              </span>
            );
          }
          return child;
        });

        return (
          <li
            style={{
              whiteSpace: 'normal',
              overflowWrap: 'break-word',
              marginBottom: '2px',
              listStyleType: 'none',
              marginLeft: '-24px',
            }}
            {...props}
          >
            {filteredChildren}
          </li>
        );
      }

      return (
        <li
          style={{
            whiteSpace: 'normal',
            overflowWrap: 'break-word',
            marginBottom: '2px',
            listStyleType: isTask ? 'none' : undefined,
            marginLeft: isTask ? '-24px' : undefined,
          }}
          {...props}
        >
          {liChildren}
        </li>
      );
    },
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
    a: ({ node, children, ...props }) => (
      <a style={{ color: 'var(--accent)', textDecoration: 'underline' }} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>
    ),
    table: ({ node, ...props }) => (
      <div style={{ overflowX: 'auto', margin: '8px 0' }}>
        <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: '0.9em' }} {...props} />
      </div>
    ),
    thead: ({ node, ...props }) => (
      <thead style={{ borderBottom: '2px solid var(--border)' }} {...props} />
    ),
    th: ({ node, ...props }) => (
      <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: 600, whiteSpace: 'nowrap' }} {...props} />
    ),
    td: ({ node, ...props }) => (
      <td style={{ padding: '6px 10px', borderTop: '1px solid var(--border)' }} {...props} />
    ),
  };

  return (
    <div style={style}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
};

export default MarkdownBody;
