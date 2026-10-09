import React, { useState, useCallback, useEffect, useRef } from 'react';

/**
 * Small copy-to-clipboard icon button: a clipboard glyph that turns into a
 * check mark for 1.5 s after a successful copy. Shared by the share cards
 * (ProposalInline) and Markdown code blocks (MarkdownBody). Position it with
 * `style` (e.g. position: absolute). If the clipboard is unavailable (non-secure
 * context) or the write is refused, nothing happens and no toast is shown.
 */
const CopyButton = ({ text, title = 'Copy', style, ...rest }) => {
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);

  const handleCopy = useCallback((e) => {
    e.stopPropagation();
    if (!text) return;
    try {
      navigator.clipboard.writeText(text).then(() => {
        setCopied(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1500);
      }).catch(() => { /* clipboard unavailable — no toast needed */ });
    } catch (err) { /* navigator.clipboard undefined — no toast needed */ }
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      title={title}
      aria-label={title}
      style={{
        background: 'none', border: 'none', cursor: 'pointer',
        padding: '4px', lineHeight: 0,
        color: copied ? 'var(--success)' : 'var(--text-muted)',
        opacity: copied ? 1 : 0.6,
        transition: 'opacity 0.15s ease, color 0.15s ease',
        ...style,
      }}
      onMouseEnter={(e) => { e.currentTarget.style.opacity = 1; }}
      onMouseLeave={(e) => { if (!copied) e.currentTarget.style.opacity = 0.6; }}
      {...rest}
    >
      {copied ? (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <path d="M3 8.5 L6.5 12 L13 4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.2"/>
          <path d="M10.5 5.5 V4 A1.5 1.5 0 0 0 9 2.5 H4 A1.5 1.5 0 0 0 2.5 4 V9 A1.5 1.5 0 0 0 4 10.5 H5.5" stroke="currentColor" strokeWidth="1.2"/>
        </svg>
      )}
    </button>
  );
};

export default CopyButton;
