import React, { useState } from 'react';
import api from '../api';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime } from '../utils/date';

/**
 * A Community Archive read reply on the thread page.
 *
 * ReadWindowLine sits above the verdict and answers the reader's first
 * question when the picks look stale: which day was this, exactly. The
 * bounds come from the render the reply was made from, in the reader's
 * local time, with the count of tweets they had already seen and so
 * never reached the model.
 *
 * ReadReplyTail sits under the picks. Two things can happen after a
 * read, and they differ in what the model gets. "Read again with my
 * marks" feeds the day back in with the reader's marks on these picks
 * in view: what they marked read leaves the list, and read / good / bad
 * tell the model how the first picks landed (the marks travel in the
 * closing note of the prompt, see _reference_marks_note). A reply typed
 * below is a Text-mode conversation about the picks instead: the day
 * stays out, the assistant's tools are on. The tail makes that
 * difference plain, says when there is nothing to feed back yet, and
 * carries the list's read state so the reader can clear it in one go.
 */

const count = (n) => Number(n || 0).toLocaleString('en-US');

export const ReadWindowLine = ({ window: w }) => {
  if (!w || !w.window_end) return null;
  const from = formatDateTime(w.window_start);
  const to = formatDateTime(w.window_end);
  return (
    <p className="read-window">
      {`Tweets from ${from} to ${to} (your time): ${count(w.tweets)} by ${count(w.accounts)} accounts.`}
      {w.excluded > 0 && ` ${count(w.excluded)} you had already seen were left out.`}
    </p>
  );
};

const READ_AGAIN_TITLE = 'Reads the day\'s tweets again with your marks on these picks in view: '
  + 'what you marked read leaves the list; read, good and bad tell it how the picks landed.';
const HINT_WITH_MARKS = 'The second read gets your marks: what you marked read leaves the list, '
  + 'and read, good and bad tell it how these picks landed. '
  + 'A reply below is a Text-mode conversation instead.';
const HINT_NO_MARKS = 'Nothing marked yet, so a second read would see the same day and the same picks. '
  + 'Mark what you read and rate the picks first. '
  + 'A reply below is a Text-mode conversation instead.';

export const ReadReplyTail = ({
  nodeId, unread, total, marked = 0, loaded = true, onMarkedAll, onReadAgain, busy,
}) => {
  const { addToast } = useToast();
  const [marking, setMarking] = useState(false);

  const markAll = () => {
    setMarking(true);
    api.post(`/nodes/${nodeId}/feed-picks/read`)
      .then((res) => { if (onMarkedAll) onMarkedAll(res.data.read_at || {}); })
      .catch(() => addToast('Could not mark the list as read.', 4000))
      .finally(() => setMarking(false));
  };

  let state = null;
  if (loaded && total > 0) {
    state = unread > 0 ? (
      <>
        <span>{unread === total ? `${total} unread` : `${unread} of ${total} unread`}</span>
        <button
          type="button"
          className="ext-quote-read-toggle"
          data-read="false"
          onClick={markAll}
          disabled={marking}
        >
          Mark all as read
        </button>
      </>
    ) : <span>All read.</span>;
  }
  // Before the quotes load the marks are unknown; say nothing about them.
  const noMarksYet = loaded && total > 0 && marked === 0;

  return (
    <div className="read-tail">
      <div className="read-tail-row">
        <span className="read-tail-state">{state}</span>
        <button
          type="button"
          className="read-again"
          onClick={onReadAgain}
          disabled={busy}
          title={READ_AGAIN_TITLE}
        >
          {busy ? 'Reading…' : 'Read again with my marks'}
        </button>
      </div>
      <p className="read-tail-hint">
        {noMarksYet ? HINT_NO_MARKS : HINT_WITH_MARKS}
      </p>
    </div>
  );
};
