import React, { useEffect, useState } from 'react';
import { FiPlusCircle, FiMinusCircle } from 'react-icons/fi';
import api from '../api';
import { useToast } from '../contexts/ToastContext';

/**
 * ReferenceFeedback - the user's verdict on Loore having quoted a saved
 * reference: "Good quote" (+) or "Bad quote" (−). Two thin contour
 * glyphs, muted until chosen; the chosen one takes the accent. Choosing
 * the same one again clears it. Neutral on purpose — it judges the
 * recommendation (was this the right thing to surface, here, now), not
 * the post itself, so no heart and no thumbs-down.
 *
 * Props:
 *   itemId:   ExternalItem id
 *   feedback: 'good' | 'bad' | null (server value; local state follows it)
 *   size:     icon size in px (default 16: the glyph's circle is then
 *             about 13 px across, the em of the 0.8em footer text it
 *             sits in). The button around it is 40 px tall and as
 *             wide as the icon plus the gap between the icons, so the
 *             pair is easy to hit on a phone (#351).
 *
 * The pair carries its own outer margins (index.css, .ref-feedback): half
 * the gap between the icons on each side, so the control after it sits
 * one gap away. The parent adds no gap after it.
 *   onChange: optional (feedback, response) => void after the server
 *             confirms; `response.read_at` is set when the verdict
 *             marked the reference read (a verdict counts as reading)
 *   nodeId:   the reply the reference is shown in, when it is a
 *             recommendation: the verdict is logged against that reply,
 *             which decides which recommendations it counts for (#352)
 *   shared:   the shown verdict was given in another reply (a parallel
 *             Read); the glyphs say so on hover
 */
const ReferenceFeedback = ({ itemId, feedback, size = 16, onChange, nodeId, shared = false }) => {
  const { addToast } = useToast();
  const [value, setValue] = useState(feedback || null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setValue(feedback || null); }, [feedback]);
  // Only until the reader clicks here: then the verdict is their own
  // in this reply too.
  const [sharedShown, setSharedShown] = useState(shared);
  useEffect(() => { setSharedShown(shared); }, [shared, feedback]);

  const choose = (e, next) => {
    e.stopPropagation();
    const target = value === next ? null : next;
    setSaving(true);
    const body = { feedback: target };
    if (nodeId) body.node_id = nodeId;
    api.post(`/external/items/${itemId}/feedback`, body)
      .then((res) => {
        setValue(res.data.feedback);
        setSharedShown(false);
        if (onChange) onChange(res.data.feedback, res.data);
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
      title={sharedShown && value === kind
        ? `${label} (your rating from another reply)`
        : label}
      disabled={saving}
      onClick={(e) => choose(e, kind)}
    >
      <Icon size={size} strokeWidth={1.6} />
    </button>
  );

  return (
    <span className="ref-feedback" style={{ '--ref-feedback-icon': `${size}px` }}>
      {glyph('good', 'Good quote', FiPlusCircle)}
      {glyph('bad', 'Bad quote', FiMinusCircle)}
    </span>
  );
};

export default ReferenceFeedback;
