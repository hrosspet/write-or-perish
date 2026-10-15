import React, { useEffect, useRef, useState } from 'react';
import { FiPlusCircle, FiMinusCircle } from 'react-icons/fi';
import api from '../api';
import LooreTooltip from './LooreTooltip';
import { formatDate } from '../utils/date';
import { useToast } from '../contexts/ToastContext';

// The hover labels (Peter, 2026-10-14: "the intro", not "the point").
export const PICK_LABELS = {
  pick: { good: 'Good pick', bad: 'Bad pick' },
  intro: { good: 'Good intro', bad: 'Bad intro' },
  tweet: { good: 'Good tweet', bad: 'Bad tweet' },
};
const GROUP_LABELS = { pick: 'This pick', intro: "Loore's intro", tweet: 'The tweet' };

/**
 * PickVerdict - the verdicts on one pick of a gleaning (#502, option B of
 * the 2026-10-14 mockup, Peter). A pick is Loore's intro (the paragraph
 * above the card, model-written) and the tweet in the card; the user
 * judges each apart, as good, unrated or bad.
 *
 * Under the card, closing the whole pick: at rest ONE ⊕ ⊖ pair ("Good
 * pick" / "Bad pick"). A tap on it gives both parts that verdict, and the
 * pair opens into a labelled pair per part, "The intro ⊕ ⊖  The tweet
 * ⊕ ⊖". Tapping a lit glyph clears that part, the other one changes it;
 * when both are cleared the pair closes again. A pick with any verdict
 * (on load too) shows the opened form. Focus follows the change: to the
 * intro's glyph of the verdict just given when the pair opens, to the
 * pick's when it closes.
 *
 * One request per tap (POST /nodes/<id>/feed-picks/<item>/verdict), the
 * pick-level one carrying both parts, so the two never race. The tweet's
 * part is the reference's own verdict (the one ReferenceFeedback sets
 * elsewhere; it marks the tweet read), the intro's is the pick's. The
 * answer is the marks as stored (`feedback` = the tweet verdict that
 * counts for this pick), and the glyphs show only that: nothing is lit
 * before the server has it. A tap while one is saving is ignored.
 *
 * Props: nodeId (the gleaning), itemId (the tweet's ExternalItem), intro
 * and tweet ('good' | 'bad' | null), tweetShared (the tweet's verdict was
 * given in a parallel Read), ratedBefore ({feedback, at}: the verdict the
 * model knew when it picked, named beside an unrated tweet), and
 * onChange(answer, sent) after the server confirms.
 */
const PickVerdict = ({ nodeId, itemId, intro, tweet, tweetShared = false, ratedBefore, onChange }) => {
  const { addToast } = useToast();
  const [marks, setMarks] = useState({ intro: intro || null, tweet: tweet || null });
  useEffect(() => { setMarks((m) => ({ ...m, intro: intro || null })); }, [intro]);
  useEffect(() => { setMarks((m) => ({ ...m, tweet: tweet || null })); }, [tweet]);
  const [sharedShown, setSharedShown] = useState(tweetShared);
  useEffect(() => { setSharedShown(tweetShared); }, [tweetShared, tweet]);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const footRef = useRef(null);
  // The glyph to focus once the pair has opened or closed.
  const [focusNext, setFocusNext] = useState(null);

  useEffect(() => {
    if (!focusNext || !footRef.current) return;
    const el = footRef.current.querySelector(
      `[data-subject="${focusNext.subject}"][data-value="${focusNext.value}"]`);
    if (el) el.focus();
    setFocusNext(null);
  }, [focusNext, marks]);

  const opened = !!(marks.intro || marks.tweet);

  const send = (body, value) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    const wasOpen = opened;
    api.post(`/nodes/${nodeId}/feed-picks/${itemId}/verdict`, body)
      .then((res) => {
        const d = res.data || {};
        const next = { intro: d.intro_verdict || null, tweet: d.feedback || null };
        setMarks(next);
        setSharedShown(!!d.feedback_shared);
        const nowOpen = !!(next.intro || next.tweet);
        // The glyph tapped is gone when the pair opens or closes; within
        // the opened pair it stays, and so does the focus.
        if (!wasOpen && nowOpen) setFocusNext({ subject: 'intro', value });
        else if (wasOpen && !nowOpen) setFocusNext({ subject: 'pick', value });
        if (onChange) onChange(d, body);
      })
      .catch(() => addToast('Could not save your verdict.', 4000))
      .finally(() => { savingRef.current = false; setSaving(false); });
  };

  const choose = (e, subject, value) => {
    e.stopPropagation();
    if (subject === 'pick') {
      send({ intro: value, tweet: value }, value);
      return;
    }
    send({ [subject]: marks[subject] === value ? null : value }, value);
  };

  const glyph = (subject, value, Icon) => {
    const current = subject === 'pick' ? null : marks[subject];
    const selected = current === value;
    let label = PICK_LABELS[subject][value];
    if (subject === 'tweet' && selected && sharedShown) {
      label = `${label} (your rating from another reply)`;
    }
    return (
      <LooreTooltip text={label} describe={false}>
        <button
          type="button"
          className="ref-feedback-glyph"
          data-subject={subject}
          data-value={value}
          data-selected={selected ? 'true' : 'false'}
          aria-pressed={selected}
          aria-label={label}
          onClick={(e) => choose(e, subject, value)}
        >
          <Icon size={16} strokeWidth={1.6} />
        </button>
      </LooreTooltip>
    );
  };

  const pair = (subject) => (
    <span
      className="ref-feedback"
      role="group"
      aria-label={GROUP_LABELS[subject]}
      style={{ '--ref-feedback-icon': '16px' }}
    >
      {glyph(subject, 'good', FiPlusCircle)}
      {glyph(subject, 'bad', FiMinusCircle)}
    </span>
  );

  return (
    <div className="pick-foot" ref={footRef} aria-busy={saving || undefined}>
      {opened ? (
        <>
          <span className="pick-part">
            <span className="pick-part-label">The intro</span>
            {pair('intro')}
          </span>
          <span className="pick-part">
            <span className="pick-part-label">The tweet</span>
            {pair('tweet')}
          </span>
        </>
      ) : pair('pick')}
      {ratedBefore && !marks.tweet && (
        <span className="pick-rated-before">
          {`You rated this tweet ${ratedBefore.feedback} on ${formatDate(ratedBefore.at, { relative: false })}`}
        </span>
      )}
    </div>
  );
};

export default PickVerdict;
