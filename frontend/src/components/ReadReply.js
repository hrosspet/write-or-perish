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
 * read, and they differ in what the model gets: a reply typed below is
 * a conversation about the picks (the day's tweets stay out of it);
 * "Read the day again" feeds the day back in, minus what the reader has
 * marked read, with these picks and their marks in view. The tail makes
 * that difference plain, and carries the list's read state so the
 * reader can clear it in one go.
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

export const ReadReplyTail = ({
  nodeId, unread, total, loaded = true, onMarkedAll, onReadAgain, busy,
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
        <button
          type="button"
          className="read-again"
          onClick={onReadAgain}
          disabled={busy}
          title="Reads the same day's tweets again with these picks and your marks in view. Tweets you marked as read are left out."
        >
          {busy ? 'Reading…' : 'Read the day again'}
        </button>
      </div>
      <p className="read-tail-hint">
        A reply below talks about these picks. Reading again sends the day's tweets back in, minus the ones you marked as read.
      </p>
    </div>
  );
};
