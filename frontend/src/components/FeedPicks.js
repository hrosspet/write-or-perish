import React, { useEffect, useState } from 'react';
import api from '../api';
import MarkdownBody from './MarkdownBody';
import ReferenceFeedback from './ReferenceFeedback';
import ReferenceReadToggle from './ReferenceReadToggle';
import { formatDate } from '../utils/date';
import { useToast } from '../contexts/ToastContext';

/**
 * FeedPicks - the tweets a Community Archive feed reply named, read in
 * full under the verdict. One run of entries separated by hairlines,
 * not cards: the tweet's text is the body, the model's one-line reason
 * sits under it in its own voice, and the left margin carries the
 * number that matters for calibration — the model's relevance estimate
 * — with a filled mark on the ones it recommended.
 *
 * Every pick is a saved reference, so the footer is the reference's own
 * record: the good / bad verdict (was this the right thing to surface)
 * and the user's read mark. A read pick fades so the unread ones stand
 * out while working down the list. Opening a tweet marks it read, and so
 * does rating it; the toggle stays to undo it (a long post opened for
 * later).
 */
const FeedPicks = ({ nodeId, enabled = true }) => {
  const { addToast } = useToast();
  const [picks, setPicks] = useState(null);
  const [error, setError] = useState(null);
  const [markingAll, setMarkingAll] = useState(false);

  useEffect(() => {
    if (!nodeId || !enabled) return undefined;
    let cancelled = false;
    api.get(`/nodes/${nodeId}/feed-picks`)
      .then((res) => { if (!cancelled) setPicks(res.data.picks || []); })
      .catch(() => { if (!cancelled) setError('Could not load the picks.'); });
    return () => { cancelled = true; };
  }, [nodeId, enabled]);

  if (error) return <div className="feed-picks-error">{error}</div>;
  if (!picks || picks.length === 0) return null;

  const patch = (idx, updates) => setPicks((prev) => prev.map((p, i) => (
    i === idx ? { ...p, item: { ...p.item, ...updates } } : p
  )));
  const unread = picks.filter((p) => !p.item.read_at).length;

  // Every open is logged against this reply (#352); it also marks the
  // tweet read when it was not.
  const markReadOnOpen = (idx, item) => {
    api.post(`/external/items/${item.id}/read`, { node_id: nodeId, via: 'open' })
      .then((res) => patch(idx, { read_at: res.data.read_at }))
      .catch(() => addToast('Could not update the read mark.', 4000));
  };

  const markAllRead = () => {
    setMarkingAll(true);
    api.post(`/nodes/${nodeId}/feed-picks/read`)
      .then((res) => {
        const readAt = res.data.read_at || {};
        setPicks((prev) => prev.map((p) => ({
          ...p, item: { ...p.item, read_at: readAt[p.item.id] || p.item.read_at },
        })));
      })
      .catch(() => addToast('Could not mark the list as read.', 4000))
      .finally(() => setMarkingAll(false));
  };

  return (
    <>
    <ol className="feed-picks" aria-label="Tweets picked from the Community Archive">
      {picks.map((pick, idx) => {
        const { item } = pick;
        const read = !!item.read_at;
        return (
          <li
            key={item.id}
            className="feed-pick"
            data-recommended={pick.recommended ? 'true' : 'false'}
            data-read={read ? 'true' : 'false'}
          >
            <div className="feed-pick-margin" aria-hidden="true">
              {typeof pick.relevance === 'number' && (
                <span className="feed-pick-relevance">{pick.relevance}%</span>
              )}
              {pick.recommended && <span className="feed-pick-mark" />}
            </div>
            <div className="feed-pick-body">
              <div className="feed-pick-head">
                <a
                  href={`https://x.com/${item.author_handle}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  title="Open the account on X in a new tab"
                >
                  @{item.author_handle}
                </a>
                {pick.recommended && (
                  <span className="feed-pick-recommended">recommended</span>
                )}
                {pick.picked_by && (
                  <span className="feed-pick-by" title="Who chose this tweet">
                    {pick.picked_by === 'random' ? 'random sample' : pick.picked_by}
                  </span>
                )}
                {typeof pick.relevance === 'number' && (
                  <span className="sr-only">{`relevance ${pick.relevance} percent`}</span>
                )}
              </div>
              <div className="feed-pick-text">
                <MarkdownBody paragraphMargin="0">{item.content}</MarkdownBody>
              </div>
              {pick.why && <p className="feed-pick-why">{pick.why}</p>}
              <div className="feed-pick-foot">
                {item.posted_at && <span>{formatDate(item.posted_at, { relative: false })}</span>}
                {item.url && (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open the tweet in a new tab (marks it read)"
                    onClick={() => markReadOnOpen(idx, item)}
                  >
                    Open on X
                  </a>
                )}
                <span className="feed-pick-owner">
                  <ReferenceFeedback
                    itemId={item.id}
                    feedback={item.feedback}
                    nodeId={nodeId}
                    shared={!!item.feedback_shared}
                    onChange={(feedback, data) => patch(idx, {
                      feedback,
                      ...(data && data.read_at ? { read_at: data.read_at } : {}),
                    })}
                  />
                  <ReferenceReadToggle
                    itemId={item.id}
                    nodeId={nodeId}
                    readAt={item.read_at}
                    onChange={(read_at) => patch(idx, { read_at })}
                  />
                </span>
              </div>
            </div>
          </li>
        );
      })}
    </ol>
    <div className="feed-picks-end">
      {unread > 0 ? (
        <button
          type="button"
          className="ext-quote-read-toggle"
          data-read="false"
          onClick={markAllRead}
          disabled={markingAll}
        >
          Mark all as read
        </button>
      ) : (
        <span>All read.</span>
      )}
    </div>
    </>
  );
};

export default FeedPicks;
