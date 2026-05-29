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
 * Recursively replace checkbox inputs in React children tree.
 * Handles both tight lists (checkbox is direct child of li)
 * and loose lists (checkbox is inside a <p> wrapper).
 * Returns { children, found }.
 */
function replaceCheckboxes(children, renderToggle) {
  let found = false;
  const mapped = React.Children.map(children, child => {
    if (child && child.props && child.props.type === 'checkbox') {
      found = true;
      return renderToggle(!!child.props.checked);
    }
    // Recurse into wrapper elements like <p>
    if (child && child.props && child.props.children) {
      const inner = replaceCheckboxes(child.props.children, renderToggle);
      if (inner.found) {
        found = true;
        return React.cloneElement(child, {}, inner.children);
      }
    }
    return child;
  });
  return { children: mapped, found };
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
    h1: ({ node, ...props }) => (
      <h1 style={{ fontFamily: 'var(--serif)', fontSize: '2.2em', fontWeight: 700, lineHeight: 1.2, margin: '1.2em 0 0.4em', color: 'var(--text-primary)' }} {...props} />
    ),
    h2: ({ node, ...props }) => (
      <h2 style={{ fontFamily: 'var(--serif)', fontSize: '1.8em', fontWeight: 700, lineHeight: 1.25, margin: '1.1em 0 0.4em', color: 'var(--text-primary)' }} {...props} />
    ),
    h3: ({ node, ...props }) => (
      <h3 style={{ fontFamily: 'var(--serif)', fontSize: '1.5em', fontWeight: 600, lineHeight: 1.3, margin: '1em 0 0.35em', color: 'var(--text-primary)' }} {...props} />
    ),
    h4: ({ node, ...props }) => (
      <h4 style={{ fontFamily: 'var(--serif)', fontSize: '1.25em', fontWeight: 600, lineHeight: 1.3, margin: '1em 0 0.35em', color: 'var(--text-primary)' }} {...props} />
    ),
    h5: ({ node, ...props }) => (
      <h5 style={{ fontFamily: 'var(--serif)', fontSize: '1.1em', fontWeight: 600, lineHeight: 1.35, margin: '0.9em 0 0.3em', color: 'var(--text-primary)' }} {...props} />
    ),
    h6: ({ node, ...props }) => (
      <h6 style={{ fontFamily: 'var(--serif)', fontSize: '0.95em', fontWeight: 600, lineHeight: 1.35, margin: '0.9em 0 0.3em', color: 'var(--text-primary)' }} {...props} />
    ),
    p: ({ node, ...props }) => (
      <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word', margin: paragraphMargin }} {...props} />
    ),
    blockquote: ({ node, ...props }) => (
      <blockquote
        style={{
          borderLeft: '3px solid var(--accent-dim)',
          background: 'var(--bg-card)',
          padding: '0.5em 1em',
          margin: '0.75em 0',
          color: 'var(--text-secondary)',
          fontStyle: 'italic',
        }}
        {...props}
      />
    ),
    strong: ({ node, ...props }) => (
      <strong style={{ fontWeight: 700 }} {...props} />
    ),
    em: ({ node, ...props }) => (
      <em style={{ fontStyle: 'italic' }} {...props} />
    ),
    del: ({ node, ...props }) => (
      <del style={{ textDecoration: 'line-through', color: 'var(--text-muted)' }} {...props} />
    ),
    img: ({ node, ...props }) => (
      <img style={{ maxWidth: '100%', height: 'auto' }} {...props} />
    ),
    ul: ({ node, ...props }) => (
      <ul style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
    ),
    ol: ({ node, ...props }) => (
      <ol style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
    ),
    li: ({ node, children: liChildren, ...props }) => {
      const isTask = props.className === 'task-list-item';

      if (isTask) {
        const itemText = onCheckboxToggle ? extractText(liChildren).trim() : null;
        const { children: filteredChildren } = replaceCheckboxes(
          liChildren,
          (isChecked) => {
            const interactive = !!onCheckboxToggle;
            const handlers = interactive ? {
              onClick: (e) => {
                e.preventDefault();
                onCheckboxToggle(itemText, isChecked);
              },
              role: 'checkbox',
              'aria-checked': isChecked,
              tabIndex: 0,
              onKeyDown: (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onCheckboxToggle(itemText, isChecked);
                }
              },
            } : {};
            return (
              <span
                {...handlers}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  width: '18px',
                  height: '18px',
                  borderRadius: '50%',
                  border: `1.5px solid ${isChecked ? 'var(--accent-dim)' : 'var(--border-hover)'}`,
                  background: isChecked ? 'var(--accent-dim)' : 'none',
                  flexShrink: 0,
                  marginRight: '8px',
                  fontSize: '0.6rem',
                  color: 'var(--bg-deep)',
                  fontWeight: 600,
                  transition: 'all 0.3s',
                  cursor: interactive ? 'pointer' : 'inherit',
                  verticalAlign: 'middle',
                }}
              >
                {isChecked && '✓'}
              </span>
            );
          },
        );

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
    pre: ({ node, ...props }) => (
      <pre
        style={{
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: '6px',
          padding: '0.75em 1em',
          margin: '0.75em 0',
          overflowX: 'auto',
          fontSize: '0.9em',
        }}
        {...props}
      />
    ),
    code: ({ node, inline, className, children, ...props }) =>
      inline ? (
        <code style={{ whiteSpace: 'pre-wrap', overflowWrap: 'break-word', fontSize: '0.9em' }} {...props}>
          {children}
        </code>
      ) : (
        <code className={className} {...props}>{children}</code>
      ),
    a: ({ node, children, ...props }) => (
      <a
        style={{ color: 'var(--accent)', textDecoration: 'underline' }}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        {...props}
      >{children}</a>
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
