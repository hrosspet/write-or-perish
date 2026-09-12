import React, { useEffect, useState } from 'react';
import { FiPlusCircle, FiMinusCircle } from 'react-icons/fi';
import api from '../api';
import { useToast } from '../contexts/ToastContext';

/**
 * ReferenceFeedback - the user's verdict on Loore having surfaced a saved
 * reference: "More like this" (+) or "Fewer like this" (−). Two thin
 * contour glyphs, muted until chosen; the chosen one takes the accent.
 * Choosing the same one again clears it. Neutral on purpose — this
 * steers future picks, it does not rate the post.
 *
 * Props:
 *   itemId:   ExternalItem id
 *   feedback: 'more' | 'less' | null (server value; local state follows it)
 *   size:     icon size in px (default 14)
 *   onChange: optional (feedback) => void after the server confirms
 */
const ReferenceFeedback = ({ itemId, feedback, size = 14, onChange }) => {
  const { addToast } = useToast();
  const [value, setValue] = useState(feedback || null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setValue(feedback || null); }, [feedback]);

  const choose = (e, next) => {
    e.stopPropagation();
    const target = value === next ? null : next;
    setSaving(true);
    api.post(`/external/items/${itemId}/feedback`, { feedback: target })
      .then((res) => {
        setValue(res.data.feedback);
        if (onChange) onChange(res.data.feedback);
      })
      .catch(() => addToast('Could not save your feedback.', 4000))
      .finally(() => setSaving(false));
  };

  const glyph = (kind, label, Icon) => (
    <button
      type="button"
      className="ref-feedback-glyph"
      data-selected={value === kind ? 'true' : 'false'}
      aria-pressed={value === kind}
      aria-label={label}
      title={label}
      disabled={saving}
      onClick={(e) => choose(e, kind)}
    >
      <Icon size={size} strokeWidth={1.6} />
    </button>
  );

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
      {glyph('more', 'More like this', FiPlusCircle)}
      {glyph('less', 'Fewer like this', FiMinusCircle)}
    </span>
  );
};

export default ReferenceFeedback;
