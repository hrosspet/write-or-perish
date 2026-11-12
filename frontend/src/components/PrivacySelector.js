import React from 'react';
import { useUser } from '../contexts/UserContext';
import CircleAudiencePicker from './CircleAudiencePicker';
import { joinNames } from '../utils/circles';

/**
 * PrivacySelector component for selecting privacy level and AI usage permissions.
 *
 * With Circles on for the user (user.circles_enabled) the privacy part is
 * "Who can read this": Only me / Circles / Everyone (stored as private /
 * circles / public), and Circles opens the audience picker. With Circles
 * off it is the dropdown as before, its Circles option greyed out.
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
  // Circles. `audience` = { circleIds, namesText } chosen so far;
  // `threadAudience` = the people of the shared thread a reply is written
  // into (the picker is then a note naming them); `allowCircles` false
  // leaves the choice out (imports: Only me / Everyone).
  audience = null,
  onAudienceChange,
  onAudienceResolved,
  threadAudience = null,
  allowCircles = true,
  // An existing reply in someone's shared thread: Everyone is not its
  // writer's to choose.
  lockPublic = false,
  // An AI reply in a shared thread is read by the thread's people or
  // only by the person who asked for it: Everyone is not shown at all.
  hidePublic = false,
  // An entry of the author's own with something above it in its thread:
  // { checked, onChange } for "Share also the discussion leading up to
  // this node?". Null: no such box.
  pathShare = null,
  // A reply under the user's own node that only they read, in a thread
  // someone else shared: Only me is the one choice (the server refuses
  // Everyone and any other audience there).
  onlyMe = false,
  // The entry being edited (its readers sentence follows the time each
  // circle was shared with it), and a counter the form raises to have
  // the typed names looked up now (see CircleAudiencePicker).
  audienceNodeId = null,
  audienceCommitSignal = 0,
  pickerKey,
}) => {
  // No provider in some tests and embeds: then Circles is off.
  const user = useUser()?.user;
  const circlesOn = !!user?.circles_enabled;

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

  const choiceStyle = (selected, off) => ({
    minHeight: '48px',
    borderRadius: '6px',
    border: `1px solid ${selected ? 'var(--accent)' : 'var(--border)'}`,
    background: selected ? 'var(--bg-card)' : 'var(--bg-surface)',
    color: selected ? 'var(--text-primary)' : 'var(--text-secondary)',
    fontFamily: 'var(--sans)',
    fontWeight: selected ? 400 : 300,
    fontSize: '0.94rem',
    cursor: off ? 'not-allowed' : 'pointer',
    opacity: off ? 0.4 : 1,
    padding: '0 8px',
  });

  const choices = [
    { value: 'private', label: 'Only me' },
    ...(allowCircles ? [{ value: 'circles', label: 'Circles' }] : []),
    ...(hidePublic ? [] : [{ value: 'public', label: 'Everyone', off: lockPublic }]),
  ];

  let privacyPart;
  if (onlyMe) {
    privacyPart = (
      <div style={{ marginBottom: '12px' }}>
        <label style={labelStyle}>Who can read this</label>
        <div style={descStyle}>
          Only you. You are replying under a node only you can read, in a
          thread someone else shared.
        </div>
      </div>
    );
  } else if (lockPrivacy && threadAudience) {
    // A reply in a shared thread is read by the thread's people.
    privacyPart = (
      <div style={{ marginBottom: '12px' }}>
        <label style={labelStyle}>Who can read this</label>
        <div style={descStyle}>
          {joinNames(threadAudience)
            ? `${joinNames(threadAudience)} will see it, with your name. Nobody else can.`
            : 'The people this thread is shared with.'}
        </div>
      </div>
    );
  } else if (lockPrivacy) {
    privacyPart = (
      <div style={{ marginBottom: '12px' }}>
        <label style={labelStyle}>
          {circlesOn ? 'Who can read this' : 'Privacy Level'}
        </label>
        <div style={descStyle}>
          {circlesOn
            ? 'Everyone — inherited from the thread.'
            : 'Public — inherited from the thread.'}
        </div>
      </div>
    );
  } else if (circlesOn) {
    privacyPart = (
      <div style={{ marginBottom: '12px' }}>
        <div style={labelStyle} id="who-can-read-label">Who can read this</div>
        <div
          role="group" aria-labelledby="who-can-read-label"
          style={{
            display: 'grid', gap: '8px',
            gridTemplateColumns: `repeat(${choices.length}, minmax(0, 1fr))`,
          }}
        >
          {choices.map((c) => (
            <button
              key={c.value} type="button"
              aria-pressed={privacyLevel === c.value}
              disabled={disabled || c.off}
              onClick={() => onPrivacyChange(c.value)}
              style={choiceStyle(privacyLevel === c.value, c.off)}
            >
              {c.label}
            </button>
          ))}
        </div>
        {privacyLevel === 'circles' && allowCircles && (
          threadAudience ? (
            <div style={descStyle}>
              {`${joinNames(threadAudience)} can read this reply. Nobody else can.`}
            </div>
          ) : (
            <CircleAudiencePicker
              key={pickerKey}
              circleIds={audience?.circleIds || []}
              namesText={audience?.namesText || ''}
              onChange={onAudienceChange || (() => {})}
              onResolved={onAudienceResolved}
              pathShare={pathShare}
              disabled={disabled}
              nodeId={audienceNodeId}
              commitSignal={audienceCommitSignal}
            />
          )
        )}
        {privacyLevel === 'private' && (
          <div style={descStyle}>Only you can read this.</div>
        )}
        {privacyLevel === 'public' && (
          <div style={descStyle}>Anyone can read this, also people who are not signed in.</div>
        )}
      </div>
    );
  } else {
    privacyPart = (
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
          <option value="private">🔒 Private · Only I can see this</option>
          {/* Not selectable until Circles is on for the account; an entry
              already saved with it keeps showing it. */}
          <option value="circles" disabled>👥 Circles · Shared with specific groups (coming soon)</option>
          <option value="public">🌐 Public · Anyone can see this</option>
        </select>
        <div style={descStyle}>
          {privacyLevel === 'private' && 'This note is private and only visible to you.'}
          {privacyLevel === 'circles' && 'Circles is coming soon. Until then this note is only visible to you.'}
          {privacyLevel === 'public' && 'This note will be visible to all users.'}
        </div>
      </div>
    );
  }

  return (
    <div style={{
      marginTop: '12px',
      marginBottom: '12px',
    }}>
      {privacyPart}

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
