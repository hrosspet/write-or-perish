import React from 'react';

/**
 * PrivacySelector component for selecting privacy level and AI usage permissions.
 */
const PrivacySelector = ({
  privacyLevel = "private",
  aiUsage = "none",
  onPrivacyChange,
  onAIUsageChange,
  disabled = false
}) => {
  const labelStyle = {
    display: 'block',
    marginBottom: '6px',
    fontFamily: 'var(--sans)',
    fontWeight: 400,
    fontSize: '0.75rem',
    textTransform: 'uppercase',
    letterSpacing: '0.12em',
    color: 'var(--text-muted)',
  };

  const selectStyle = {
    width: '100%',
    padding: '8px',
    borderRadius: '6px',
    border: '1px solid var(--border)',
    backgroundColor: disabled ? 'var(--bg-surface)' : 'var(--bg-deep)',
    color: 'var(--text-secondary)',
    fontSize: '14px',
    fontFamily: 'var(--sans)',
    fontWeight: 300,
  };

  const descStyle = {
    fontSize: '0.8rem',
    color: 'var(--text-muted)',
    marginTop: '4px',
    fontFamily: 'var(--sans)',
    fontWeight: 300,
  };

  return (
    <div style={{
      marginTop: '12px',
      marginBottom: '12px',
      padding: '12px',
      backgroundColor: 'var(--bg-deep)',
      borderRadius: '8px',
      border: '1px solid var(--border)'
    }}>
      <div style={{ marginBottom: '12px' }}>
        <label style={labelStyle}>
          Privacy Level
        </label>
        <select
          value={privacyLevel}
          onChange={(e) => onPrivacyChange(e.target.value)}
          disabled={disabled}
          style={selectStyle}
        >
          <option value="private">Private - Only I can see this</option>
          <option value="circles">Circles - Shared with specific groups (coming soon)</option>
          <option value="public">Public - Anyone can see this</option>
        </select>
        <div style={descStyle}>
          {privacyLevel === 'private' && 'This note is private and only visible to you.'}
          {privacyLevel === 'circles' && 'This note will be shared with your selected circles (feature coming soon).'}
          {privacyLevel === 'public' && 'This note will be visible to all users.'}
        </div>
      </div>

      <div>
        <label style={labelStyle}>
          AI Usage
        </label>
        <select
          value={aiUsage}
          onChange={(e) => onAIUsageChange(e.target.value)}
          disabled={disabled}
          style={selectStyle}
        >
          <option value="none">None - No AI access</option>
          <option value="chat">Chat - AI can use for responses (not training)</option>
          <option value="train">Train - AI can use for training data</option>
        </select>
        <div style={descStyle}>
          {aiUsage === 'none' && 'AI will not access this note.'}
          {aiUsage === 'chat' && 'AI can read this note to generate responses, but won\'t use it for training.'}
          {aiUsage === 'train' && 'AI can use this note for training data to improve the model.'}
        </div>
      </div>
    </div>
  );
};

export default PrivacySelector;
