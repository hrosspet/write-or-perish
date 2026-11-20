import React, { useState } from 'react';
import api from '../api';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime, parseTimestamp } from '../utils/date';

/**
 * A Community Archive read reply on the thread page.
 *
 * ReadWindowLine sits above the verdict and answers the reader's first
 * question when the picks look stale: which day was this, exactly. The
 * bounds come from the render the reply was made from, in the reader's
 * local time, with the count of tweets they had already read and so
 * never reached the model.
 *
 * ReadReplyTail sits under the picks and carries the list's read
 * state, so the reader can clear it in one go.
 *
 * ReadCancelledLine sits under a cancelled read's text ("This read was
 * cancelled.") and says why, for the reply's owner: the server sends
 * the reason (llm_task_error) to them only. The actions that follow
 * a read — Read further, or a reply about the picks — live in the
 * action row under the node (NodeDetail), not in the card: that row is
 * under every node of the thread, so the actions stay at hand however
 * long the conversation under the picks gets.
 */

const count = (n) => Number(n || 0).toLocaleString('en-US');

// "3 November", the reader's local date of the newest tweet.
const dayMonth = (iso) => parseTimestamp(iso)
  .toLocaleDateString('en-GB', { day: 'numeric', month: 'long' });

/**
 * The sentence a stale read adds (#688): the archive had nothing newer
 * than the window's end when the read was made (read_window.export_stale,
 * set by the server when the newest tweet was more than 36 hours old).
 */
export const staleExportNote = (w) => (w && w.export_stale && w.window_end
  ? `The newest tweets available are from ${dayMonth(w.window_end)}; the archive's daily export has not updated since.`
  : null);

export const ReadWindowLine = ({ window: w }) => {
  if (!w || !w.window_end) return null;
  const from = formatDateTime(w.window_start);
  const to = formatDateTime(w.window_end);
  const stale = staleExportNote(w);
  return (
    <p className="read-window">
      {`Tweets from ${from} to ${to} (your time): ${count(w.tweets)} by ${count(w.accounts)} accounts.`}
      {w.excluded > 0 && ` ${count(w.excluded)} you had already read were left out.`}
      {stale && ` ${stale}`}
    </p>
  );
};

export const ReadCancelledLine = ({ reason }) => {
  if (!reason || !reason.trim()) return null;
  return <p className="read-note">{reason}</p>;
};

export const ReadReplyTail = ({ nodeId, unread, total, loaded = true, onMarkedAll }) => {
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
      </div>
    </div>
  );
};
