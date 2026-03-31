import React, { useState } from 'react';
import MarkdownBody from './MarkdownBody';

/**
 * InlineArtifactSection - Collapsible inline section for context artifact placeholders.
 *
 * Rendered inline where the placeholder appears in system prompt content.
 * Collapsed by default; clicking the header toggles the full content.
 *
 * Props:
 *   type: "profile" | "todo" | "recent" | "recent_raw" | "ai_preferences"
 *   artifact: { id, version_number, content, source_tokens, covers_start, covers_end } or null
 */

function formatTokens(n) {
  if (!n) return null;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M tokens`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}K tokens`;
  return `${n} tokens`;
}

const InlineArtifactSection = ({ type, artifact }) => {
  const [expanded, setExpanded] = useState(false);

  const LABELS = {
    profile: 'User Profile',
    todo: 'User TODO',
    recent: 'Recent Context Summary',
    ai_preferences: 'AI Preferences',
    recent_raw: 'Recent Context Raw',
  };
  const label = LABELS[type] || type;

  if (!artifact) {
    return (
      <div style={notAvailableStyle}>
        [{label} not available]
      </div>
    );
  }

  // Build token suffix like "(10K tokens)"
  const tokenStr = formatTokens(artifact.source_tokens);
  const tokenSuffix = tokenStr ? ` (${tokenStr})` : '';

  // Recent raw data: show date range only (content is not loaded)
  if (type === 'recent_raw') {
    const dateRange = artifact.covers_start && artifact.covers_end
      ? `Covers ${artifact.covers_start} to ${artifact.covers_end}${tokenSuffix}`
      : 'Date range unavailable';
    return (
      <div style={containerStyle}>
        <div style={{ ...headerStyle, cursor: 'default' }}>
          <span style={chevronStyle}>{'\u25A0'}</span>
          <span>{label} — {dateRange}</span>
        </div>
      </div>
    );
  }

  // Profile: "User Profile v3" (token count is in the content metadata)
  // Recent context summary: "Recent Context Summary" (same)
  // Others: "Label vN"
  let headerLabel;
  if (type === 'profile') {
    const version = artifact.version_number ? ` v${artifact.version_number}` : '';
    headerLabel = `${label}${version}`;
  } else if (type === 'recent') {
    headerLabel = label;
  } else {
    headerLabel = artifact.version_number
      ? `${label} v${artifact.version_number}`
      : label;
  }

  return (
    <div style={containerStyle}>
      <div
        style={headerStyle}
        onClick={() => setExpanded(!expanded)}
      >
        <span style={chevronStyle}>{expanded ? '\u25BC' : '\u25B6'}</span>
        <span>{headerLabel}</span>
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
