import React from 'react';

/**
 * PrivacySelector component for selecting privacy level and AI usage permissions.
 */
const PrivacySelector = ({
  privacyLevel = "private",
  aiUsage = "none",
  onPrivacyChange,
  onAIUsageChange,
  disabled = false,
  // Replies in public threads inherit the thread's privacy (#228) —
  // render a quiet static note instead of the dropdown.
  lockPrivacy = false,
  // Levels left out of the dropdown (imports never offer 'public', #677).
  omitLevels = [],
  // One calm line shown under the privacy choice, in place of its description.
  privacyNote = null,
}) => {
  const labelStyle = {
    display: 'block',
    marginBottom: '6px',
    fontFamily: 'var(--sans)',
    fontWeight: 400,
    fontSize: '0.7rem',
    textTransform: 'uppercase',
    letterSpacing: '0.14em',
    color: 'var(--text-muted)',
  };

  const selectStyle = {
    width: '100%',
    padding: '10px 14px',
    borderRadius: '6px',
    border: '1px solid var(--border)',
    backgroundColor: disabled ? 'var(--bg-surface)' : 'var(--bg-input)',
    color: 'var(--text-secondary)',
    fontSize: '0.9rem',
    fontFamily: 'var(--sans)',
    fontWeight: 300,
    appearance: 'none',
    WebkitAppearance: 'none',
    outline: 'none',
    cursor: 'pointer',
    transition: 'border-color 0.3s',
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
    }}>
      {lockPrivacy ? (
        <div style={{ marginBottom: '12px' }}>
          <label style={labelStyle}>
            Privacy Level
          </label>
          <div style={descStyle}>
            Public — inherited from the thread.
          </div>
        </div>
      ) : (
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
          {!omitLevels.includes('private') && (
            <option value="private">🔒 Private · Only I can see this</option>
          )}
          {!omitLevels.includes('circles') && (
            <option value="circles">👥 Circles · Shared with specific groups (coming soon)</option>
          )}
          {!omitLevels.includes('public') && (
            <option value="public">🌐 Public · Anyone can see this</option>
          )}
        </select>
        <div style={descStyle}>
          {privacyNote}
          {!privacyNote && privacyLevel === 'private' && 'This note is private and only visible to you.'}
          {!privacyNote && privacyLevel === 'circles' && 'This note will be shared with your selected circles (feature coming soon).'}
          {!privacyNote && privacyLevel === 'public' && 'This note will be visible to all users.'}
        </div>
      </div>
      )}

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
          <option value="none">🚫 None · No AI access</option>
          <option value="chat">💬 Chat · AI can use for responses</option>
          <option value="train">🧠 Train · AI can use for training</option>
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
