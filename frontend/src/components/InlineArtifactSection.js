import React, { useState } from 'react';
import MarkdownBody from './MarkdownBody';

/**
 * InlineArtifactSection - Collapsible inline section for {user_profile} / {user_todo} placeholders.
 *
 * Rendered inline where the placeholder appears in system prompt content.
 * Collapsed by default; clicking the header toggles the full content.
 *
 * Props:
 *   type: "profile" | "todo" | "recent" | "ai_preferences"
 *   artifact: { id, version_number, content } or null
 */
const InlineArtifactSection = ({ type, artifact }) => {
  const [expanded, setExpanded] = useState(false);

  const LABELS = {
    profile: 'User Profile',
    todo: 'User TODO',
    recent: 'Recent Context',
    ai_preferences: 'AI Preferences',
  };
  const label = LABELS[type] || type;

  if (!artifact) {
    return (
      <div style={notAvailableStyle}>
        [{label} not available]
      </div>
    );
  }

  const versionLabel = artifact.version_number
    ? `${label} v${artifact.version_number}`
    : label;

  return (
    <div style={containerStyle}>
      <div
        style={headerStyle}
        onClick={() => setExpanded(!expanded)}
      >
        <span style={chevronStyle}>{expanded ? '\u25BC' : '\u25B6'}</span>
        <span>{versionLabel}</span>
      </div>
      {expanded && (
        <div style={contentStyle}>
          <MarkdownBody paragraphMargin="0.3em 0">
            {artifact.content || ''}
          </MarkdownBody>
        </div>
      )}
    </div>
  );
};


const containerStyle = {
  display: 'block',
  margin: '10px 0',
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderLeft: '3px solid var(--accent)',
  borderRadius: '6px',
  overflow: 'hidden',
};

const headerStyle = {
  padding: '8px 12px',
  cursor: 'pointer',
  fontFamily: 'var(--sans)',
  fontSize: '0.85rem',
  fontWeight: 500,
  color: 'var(--text-secondary)',
  display: 'flex',
  alignItems: 'center',
  gap: '6px',
  userSelect: 'none',
};

const chevronStyle = {
  fontSize: '0.7rem',
  color: 'var(--text-muted)',
};

const contentStyle = {
  padding: '0 12px 10px 12px',
  fontFamily: 'var(--sans)',
  fontSize: '0.9rem',
  lineHeight: 1.6,
  color: 'var(--text-primary)',
  borderTop: '1px solid var(--border)',
};

const notAvailableStyle = {
  display: 'inline-block',
  padding: '4px 8px',
  margin: '4px 0',
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderRadius: '4px',
  color: 'var(--text-muted)',
  fontStyle: 'italic',
  fontSize: '0.85rem',
  fontFamily: 'var(--sans)',
};

export default InlineArtifactSection;
