import React from 'react';

/**
 * PrivacySelector component for selecting privacy level and AI usage permissions.
 *
 * @param {Object} props
 * @param {string} props.privacyLevel - Current privacy level ("private", "circles", "public")
 * @param {string} props.aiUsage - Current AI usage ("none", "chat", "train")
 * @param {Function} props.onPrivacyChange - Callback when privacy level changes
 * @param {Function} props.onAIUsageChange - Callback when AI usage changes
 * @param {boolean} props.disabled - Whether the selector is disabled
 */
const PrivacySelector = ({
  privacyLevel = "private",
  aiUsage = "none",
  onPrivacyChange,
  onAIUsageChange,
  disabled = false
}) => {
  return (
    <div style={{
      marginTop: '12px',
      marginBottom: '12px',
      padding: '12px',
      backgroundColor: '#f8f9fa',
      borderRadius: '6px',
      border: '1px solid #dee2e6'
    }}>
      <div style={{ marginBottom: '12px' }}>
        <label style={{
          display: 'block',
          fontWeight: '600',
          marginBottom: '6px',
          fontSize: '14px',
          color: '#495057'
        }}>
          Privacy Level
        </label>
        <select
          value={privacyLevel}
          onChange={(e) => onPrivacyChange(e.target.value)}
          disabled={disabled}
          style={{
            width: '100%',
            padding: '8px',
            borderRadius: '4px',
            border: '1px solid #ced4da',
            fontSize: '14px',
            backgroundColor: disabled ? '#e9ecef' : 'white'
          }}
        >
          <option value="private">🔒 Private - Only I can see this</option>
          <option value="circles">👥 Circles - Shared with specific groups (coming soon)</option>
          <option value="public">🌐 Public - Anyone can see this</option>
        </select>
        <div style={{ fontSize: '12px', color: '#6c757d', marginTop: '4px' }}>
          {privacyLevel === 'private' && 'This note is private and only visible to you.'}
          {privacyLevel === 'circles' && 'This note will be shared with your selected circles (feature coming soon).'}
          {privacyLevel === 'public' && 'This note will be visible to all users.'}
        </div>
      </div>

      <div>
        <label style={{
          display: 'block',
          fontWeight: '600',
          marginBottom: '6px',
          fontSize: '14px',
          color: '#495057'
        }}>
          AI Usage
        </label>
        <select
          value={aiUsage}
          onChange={(e) => onAIUsageChange(e.target.value)}
          disabled={disabled}
          style={{
            width: '100%',
            padding: '8px',
            borderRadius: '4px',
            border: '1px solid #ced4da',
            fontSize: '14px',
            backgroundColor: disabled ? '#e9ecef' : 'white'
          }}
        >
          <option value="none">🚫 None - No AI access</option>
          <option value="chat">💬 Chat - AI can use for responses (not training)</option>
          <option value="train">🎓 Train - AI can use for training data</option>
        </select>
        <div style={{ fontSize: '12px', color: '#6c757d', marginTop: '4px' }}>
          {aiUsage === 'none' && 'AI will not access this note.'}
          {aiUsage === 'chat' && 'AI can read this note to generate responses, but won\'t use it for training.'}
          {aiUsage === 'train' && 'AI can use this note for training data to improve the model.'}
        </div>
      </div>
    </div>
  );
};

export default PrivacySelector;
