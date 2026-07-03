import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * True when a rendered child is a nested list element (<ul>/<ol>). Detected via
 * the hast `node` react-markdown passes to every component, because when these
 * helpers run the element's `type` is our custom `ul`/`ol` component (a
 * function), not the string 'ul'.
 */
function isListElement(child) {
  const tag = child && child.props && child.props.node && child.props.node.tagName;
  return tag === 'ul' || tag === 'ol';
}

/**
 * Extract plain text from React children recursively, WITHOUT descending into
 * nested lists — so a task item's label is just its own text, not its
 * sub-items' text concatenated (which would never match its source line when
 * toggling).
 */
function extractText(children) {
  let text = '';
  React.Children.forEach(children, child => {
    if (typeof child === 'string') {
      text += child;
    } else if (child && child.props && child.props.children && !isListElement(child)) {
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
    // Recurse into wrapper elements like <p>, but NOT into nested lists. A
    // nested <li>'s checkbox is still a raw <input> when the parent <li>
    // renders, so descending here would let the parent steal it (binding the
    // toggle to the parent's label). Leaving nested lists untouched lets each
    // nested <li> wire its own checkbox when it renders.
    if (child && child.props && child.props.children && !isListElement(child)) {
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
 * Inline "add a task here" input row, shown below a checklist item when its
 * hover "+" is clicked. Type + Enter inserts; Esc / blur-when-empty cancels.
 */
function AddTaskInput({ onSubmit, onCancel }) {
  const [val, setVal] = React.useState('');
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: '4px 0' }}>
      <span style={{ width: '18px', height: '18px', borderRadius: '50%', border: '1.5px dashed var(--border-hover)', flexShrink: 0, opacity: 0.5 }} />
      <input
        autoFocus
        value={val}
        onChange={(e) => setVal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); const t = val.trim(); if (t) onSubmit(t); }
          else if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
        }}
        onBlur={() => { if (!val.trim()) onCancel(); }}
        placeholder="New item…"
        style={{ flex: 1, minWidth: 0, background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontFamily: 'var(--sans)', fontSize: '0.92em', padding: '4px 8px' }}
      />
    </div>
  );
}

/**
 * MarkdownBody — shared ReactMarkdown wrapper with consistent styling.
 *
 * Props:
 *   children: markdown string
 *   style: optional style object applied to the outer <div>
 *   paragraphMargin: optional margin for <p> elements (default: "0.5em 0")
 *   onCheckboxToggle: optional callback(lineText, currentChecked) for clickable checkboxes
 *   onAddTask: optional callback(afterItemText, newText) — enables the per-row hover "+"
 */
// flowText: render paragraphs with standard markdown soft-wrap semantics
// (single source newlines flow into the line) instead of the default
// pre-wrap. The default preserves newlines because node content (user
// writing, LLM replies) depends on it; AUTHORED markdown wrapped at a
// fixed column (e.g. the user changelog) must opt in to flow, or every
// source line break renders literally — unreadable on narrow screens.
const MarkdownBody = ({ children, style, paragraphMargin = '0.5em 0', flowText = false, onCheckboxToggle, onAddTask }) => {
  const [addingAfter, setAddingAfter] = React.useState(null);
  const components = {
    h1: ({ node, children, ...props }) => (
      <h1 style={{ fontFamily: 'var(--serif)', fontSize: '2.2em', fontWeight: 700, lineHeight: 1.2, margin: '1.2em 0 0.4em', color: 'var(--text-primary)' }} {...props}>{children}</h1>
    ),
    h2: ({ node, children, ...props }) => (
      <h2 style={{ fontFamily: 'var(--serif)', fontSize: '1.8em', fontWeight: 700, lineHeight: 1.25, margin: '1.1em 0 0.4em', color: 'var(--text-primary)' }} {...props}>{children}</h2>
    ),
    h3: ({ node, children, ...props }) => (
      <h3 style={{ fontFamily: 'var(--serif)', fontSize: '1.5em', fontWeight: 600, lineHeight: 1.3, margin: '1em 0 0.35em', color: 'var(--text-primary)' }} {...props}>{children}</h3>
    ),
    h4: ({ node, children, ...props }) => (
      <h4 style={{ fontFamily: 'var(--serif)', fontSize: '1.25em', fontWeight: 600, lineHeight: 1.3, margin: '1em 0 0.35em', color: 'var(--text-primary)' }} {...props}>{children}</h4>
    ),
    h5: ({ node, children, ...props }) => (
      <h5 style={{ fontFamily: 'var(--serif)', fontSize: '1.1em', fontWeight: 600, lineHeight: 1.35, margin: '0.9em 0 0.3em', color: 'var(--text-primary)' }} {...props}>{children}</h5>
    ),
    h6: ({ node, children, ...props }) => (
      <h6 style={{ fontFamily: 'var(--serif)', fontSize: '0.95em', fontWeight: 600, lineHeight: 1.35, margin: '0.9em 0 0.3em', color: 'var(--text-primary)' }} {...props}>{children}</h6>
    ),
    p: ({ node, ...props }) => (
      <p style={{ whiteSpace: flowText ? 'normal' : 'pre-wrap', overflowWrap: 'break-word', margin: paragraphMargin }} {...props} />
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
    img: ({ node, alt, ...props }) => (
      <img alt={alt || ''} style={{ maxWidth: '100%', height: 'auto' }} {...props} />
    ),
    ul: ({ node, ...props }) => {
      const isTaskList = (props.className || '').split(/\s+/).includes('contains-task-list');
      // Task lists are styled via the `.loore-md ul.contains-task-list` rules in
      // index.css so nested levels indent correctly. Inline padding here would
      // override that CSS and flatten the tree (#138). Plain lists stay inline.
      return isTaskList
        ? <ul {...props} />
        : <ul style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />;
    },
    ol: ({ node, ...props }) => (
      <ol style={{ margin: '4px 0', paddingLeft: '24px' }} {...props} />
    ),
    li: ({ node, children: liChildren, ...props }) => {
      const isTask = props.className === 'task-list-item';

      if (isTask) {
        const itemText = (onCheckboxToggle || onAddTask) ? extractText(liChildren).trim() : null;
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

        const childArr = React.Children.toArray(filteredChildren);
        const ownContent = childArr.filter((c) => !isListElement(c));
        const nestedLists = childArr.filter((c) => isListElement(c));
        const addable = !!onAddTask;

        return (
          <li
            style={{
              whiteSpace: 'normal',
              overflowWrap: 'break-word',
              marginBottom: '2px',
              listStyleType: 'none',
            }}
            {...props}
          >
            <span className="loore-task-row" style={{ display: 'block' }}>
              {ownContent}
              {addable && (
                <button
                  type="button"
                  className="loore-add-task"
                  title="Add an item below"
                  aria-label="Add an item below"
                  onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); setAddingAfter(itemText); }}
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); setAddingAfter(itemText); }}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: 'var(--accent)',
                    cursor: 'pointer',
                    fontSize: '1.1em',
                    lineHeight: 1,
                    padding: '0 2px',
                    marginLeft: '4px',
                    verticalAlign: 'middle',
                  }}
                >+</button>
              )}
            </span>
            {nestedLists}
            {addable && addingAfter === itemText && (
              <AddTaskInput
                onSubmit={(t) => { onAddTask(itemText, t); setAddingAfter(null); }}
                onCancel={() => setAddingAfter(null)}
              />
            )}
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
    <div className="loore-md" style={style}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
};

export default MarkdownBody;
