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
 * read, and they differ in what the model gets. "Read further" asks for
 * more from the same day with everything in the thread so far in view:
 * the earlier picks, the reader's marks on them (they travel in the
 * closing note of the prompt, see _reference_marks_note) and whatever
 * was written since; what the reader has read never comes back, which
 * is the feed's obvious behaviour and goes unsaid here. A reply typed
 * below is a Text-mode conversation about the picks instead: the day
 * stays out, the assistant's tools are on. The tail makes that
 * difference plain and carries the list's read state so the reader can
 * clear it in one go. In craft mode the model picker sits beside "Read
 * further" (`modelPicker`): under a read reply the tail is the response
 * action, so the craft bar's generic LLM Response and its picker stay
 * out, and the picker lives next to the action it serves (it also sets
 * the model for a reply typed below).
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

const READ_FURTHER_TITLE = 'Loore reads the same day of tweets again, against everything in this thread so far '
  + '— your marks on these picks included — and shows you what else is relevant.';
const HINT = 'Read further: Loore reads the same day again with everything in this thread so far in view '
  + '— your marks included — and shows you what else is relevant. '
  + 'A reply below is a Text-mode conversation instead.';

export const ReadReplyTail = ({
  nodeId, unread, total, loaded = true, onMarkedAll, onReadAgain, busy, modelPicker = null,
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
  return (
    <div className="read-tail">
      <div className="read-tail-row">
        <span className="read-tail-state">{state}</span>
        <span className="read-tail-actions">
          {modelPicker}
          <button
            type="button"
            className="read-again"
            onClick={onReadAgain}
            disabled={busy}
            title={READ_FURTHER_TITLE}
          >
            {busy ? 'Reading…' : 'Read further'}
          </button>
        </span>
      </div>
      <p className="read-tail-hint">{HINT}</p>
    </div>
  );
};
