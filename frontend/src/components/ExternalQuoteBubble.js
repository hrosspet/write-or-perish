import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import MarkdownBody from './MarkdownBody';
import { formatDate } from '../utils/date';
import { useUser } from '../contexts/UserContext';
import { useToast } from '../contexts/ToastContext';
import ReferenceFeedback from './ReferenceFeedback';
import PickVerdict from './PickVerdict';

// Sources whose reference is a tweet: the card names its author.
const TWEET_SOURCES = ['community_archive', 'read_pick', 'twitter_bookmark', 'twitter_like'];

// The preview is cut here, with "…"; the reference page has the rest.
export const QUOTE_PREVIEW_CHARS = 500;

export const previewText = (text) => (
  (text || '').length > QUOTE_PREVIEW_CHARS
    ? `${text.substring(0, QUOTE_PREVIEW_CHARS)}…`
    : (text || '')
);

const hostOf = (url) => {
  try { return url ? new URL(url).hostname.replace(/^www\./, '') : null; } catch (e) { return null; }
};

/**
 * ExternalQuoteBubble - An inline quote of an external reference (a
 * tweet, a bookmark, a clipped page) — the quote-as-response rendering
 * and the tweet card of a gleaning (#435). Content comes verbatim from
 * the server-resolved external item, never from LLM text.
 *
 * The card (#435, Peter 2026-10-09): the author's display name when the
 * archive has one, the @handle and the date (no avatar, no "Saved from"
 * line), the text cut at 500 characters with "…", then the actions:
 * "Twitter ↗", the good / bad verdict (ReferenceFeedback) and "Mark as
 * read". Tapping the card itself opens the reference's page in Loore
 * (/references/<id>: the full text, the listen button, the verdict);
 * the action buttons never also open it. Only the reference's owner can
 * open that page, so for anyone else the card is not a link.
 *
 * The reference's owner also gets the same read toggle as the reference
 * page (POST/DELETE /external/items/<id>/read), so a recommendation can
 * be marked read right where Loore surfaced it. The label alone carries
 * the state ("Mark as unread" = read). Only the user marks a
 * reference read — the AI quoting it is tracked separately as surfacing.
 * `onReadChange(id, readAt)` and `onFeedbackChange(id, feedback)` tell
 * the page, which keeps the list's marks (a read reply's "n unread") in
 * step with the bubbles.
 *
 * Opening the post is reading it, and so is judging it: the owner's
 * "Twitter ↗" marks the reference read as the tab opens, a good / bad
 * verdict marks it read server-side, and "Mark as unread" stays for the
 * ones to come back to. Opening the reference page in Loore is a read
 * too (Peter, 2026-10-14), but the page itself marks it, so the mark
 * does not race the page's own load: the card only passes the reply's
 * id in the navigation state.
 *
 * `nodeId` is the reply the bubble is in. Opens, marks and verdicts are
 * logged against it (#352), and for a recommendation the server sends
 * the verdict that counts for it (`feedback`, `feedback_shared` when it
 * was given in a parallel Read) and, when the model already knew the
 * reader's verdict, `rated_before` for the line under an empty control.
 *
 * `showRecommendationFeedback` is set only where Loore quoted the
 * reference (an LLM reply): the verdict judges a recommendation, and a
 * reference the user quoted themselves was recommended by nobody (#363).
 *
 * `backLabel` names the page the reference page links back to (e.g.
 * "Today's gleanings"); the link goes to the node the card is in.
 *
 * In a gleaning (the server marks the card a pick of this reply,
 * `recommendation_kind` 'read', #502) the card's footer has no verdict:
 * the pick's own pair sits under the card (PickVerdict), judging Loore's
 * intro above the card and the tweet apart. `onIntroVerdictChange(id,
 * verdict)` tells the page about the intro's.
 */
const ExternalQuoteBubble = ({ quote, nodeId, onReadChange, onFeedbackChange, onIntroVerdictChange, showRecommendationFeedback = false, backLabel }) => {
  const userCtx = useUser();
  const currentUser = userCtx ? userCtx.user : null;
  const { addToast } = useToast();
  const navigate = useNavigate();
  const [readAt, setReadAt] = useState(quote ? quote.read_at : null);
  const [marking, setMarking] = useState(false);
  const serverReadAt = quote ? quote.read_at : null;
  useEffect(() => { setReadAt(serverReadAt); }, [serverReadAt]);

  if (!quote) {
    return (
      <div style={notAccessibleStyle}>
        [Quoted reference inaccessible]
      </div>
    );
  }

  const isTweet = TWEET_SOURCES.includes(quote.source);
  const postedAt = quote.posted_at
    ? formatDate(quote.posted_at, { relative: false })
    : null;
  const mine = !!currentUser && quote.user_id === currentUser.id;
  // A gleaning's pick: the verdicts go under the card (#502).
  const gleaningPick = mine && showRecommendationFeedback && !!nodeId
    && quote.recommendation_kind === 'read';

  const setRead = (want, via) => {
    setMarking(true);
    const body = {};
    if (nodeId) body.node_id = nodeId;
    if (via) body.via = via;
    const req = want
      ? api.post(`/external/items/${quote.id}/read`, body)
      : api.delete(`/external/items/${quote.id}/read`, { data: body });
    req
      .then((res) => {
        setReadAt(res.data.read_at);
        if (onReadChange) onReadChange(quote.id, res.data.read_at);
      })
      .catch(() => addToast('Could not update the read mark.', 4000))
      .finally(() => setMarking(false));
  };

  const openOriginal = (e) => {
    e.stopPropagation();
    if (!quote.url) return;
    // window.open first: the popup rules want the user gesture.
    window.open(quote.url, '_blank', 'noopener,noreferrer');
    // Every open is logged (the record's "opened"); it marks the
    // reference read when it was not.
    if (mine && !marking) setRead(true, 'open');
  };

  const openReference = () => {
    if (!mine) return;
    const state = nodeId
      ? { backTo: `/node/${nodeId}`, backLabel: backLabel || 'Back to the thread', nodeId }
      : undefined;
    navigate(`/references/${quote.id}`, { state });
  };

  // A click on the card body opens the reference page; one on any
  // button or link inside it does only what that control does.
  const onCardClick = (e) => {
    if (e.target.closest && e.target.closest('button, a')) return;
    openReference();
  };
  const onCardKey = (e) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openReference();
    }
  };

  const toggleRead = (e) => {
    e.stopPropagation();
    setRead(!readAt);
  };

  const host = hostOf(quote.url);
  const byline = isTweet ? (
    <>
      {quote.author_name && <span className="ext-quote-name">{quote.author_name}</span>}
      <span className="ext-quote-handle">@{quote.author_handle || 'unknown'}</span>
    </>
  ) : (
    <>
      {quote.title && <span className="ext-quote-name">{quote.title}</span>}
      {(quote.author_handle || host) && (
        <span className="ext-quote-handle">{quote.author_handle || host}</span>
      )}
    </>
  );

  const card = (
    <div
      className={`ext-quote-card${mine ? ' ext-quote-card-link' : ''}`}
      style={gleaningPick ? pickBubbleStyle : bubbleStyle}
      onClick={mine ? onCardClick : undefined}
      onKeyDown={mine ? onCardKey : undefined}
      tabIndex={mine ? 0 : undefined}
      role={mine ? 'link' : undefined}
      aria-label={mine ? 'Open this reference in Loore' : undefined}
    >
      <div className="ext-quote-byline">
        {byline}
        {postedAt && <span className="ext-quote-date">· {postedAt}</span>}
      </div>
      <div style={contentStyle}>
        <MarkdownBody paragraphMargin="0">
          {previewText(quote.content)}
        </MarkdownBody>
      </div>
      <div className="ext-quote-actions">
        {quote.url && (
          <button
            type="button"
            className="ext-quote-open"
            onClick={openOriginal}
            title={isTweet ? 'Open the tweet on Twitter in a new tab' : 'Open the original in a new tab'}
          >
            {isTweet ? 'Twitter ↗' : 'Open original ↗'}
          </button>
        )}
        {mine && showRecommendationFeedback && !gleaningPick && (
          <ReferenceFeedback
            itemId={quote.id}
            feedback={quote.feedback}
            nodeId={nodeId}
            shared={!!quote.feedback_shared}
            onChange={(fb, data) => {
              if (onFeedbackChange) onFeedbackChange(quote.id, fb);
              if (data && data.read_at && !readAt) {
                setReadAt(data.read_at);
                if (onReadChange) onReadChange(quote.id, data.read_at);
              }
            }}
          />
        )}
        {mine && (
          <button
            type="button"
            className="ext-quote-read-toggle"
            data-read={readAt ? 'true' : 'false'}
            onClick={toggleRead}
            disabled={marking}
            title={readAt ? `You read it ${formatDate(readAt)}` : undefined}
          >
            {readAt ? 'Mark as unread' : 'Mark as read'}
          </button>
        )}
        {mine && showRecommendationFeedback && !gleaningPick
          && quote.rated_before && !quote.feedback && (
          <span style={ratedBeforeStyle}>
            {`You rated this ${quote.rated_before.feedback} on ${formatDate(quote.rated_before.at, { relative: false })}`}
          </span>
        )}
      </div>
    </div>
  );
  if (!gleaningPick) return card;
  return (
    <>
      {card}
      <PickVerdict
        nodeId={nodeId}
        itemId={quote.id}
        intro={quote.intro_verdict}
        tweet={quote.feedback}
        tweetShared={!!quote.feedback_shared}
        ratedBefore={quote.rated_before}
        onChange={(data, sent) => {
          if ('tweet' in sent && onFeedbackChange) onFeedbackChange(quote.id, data.feedback || null);
          if ('intro' in sent && onIntroVerdictChange) {
            onIntroVerdictChange(quote.id, data.intro_verdict || null);
          }
          if (data.read_at && !readAt) {
            setReadAt(data.read_at);
            if (onReadChange) onReadChange(quote.id, data.read_at);
          }
        }}
      />
    </>
  );
};

const bubbleStyle = {
  display: 'block',
  padding: '12px 14px',
  margin: '10px 0',
  background: 'var(--bg-surface)',
  border: '1px solid var(--border)',
  borderRadius: '10px',
  whiteSpace: 'pre-wrap',
  maxWidth: '100%',
  boxSizing: 'border-box',
};

// In a gleaning the pick's verdicts close it under the card: nothing
// below the card but them.
const pickBubbleStyle = { ...bubbleStyle, margin: '10px 0 0' };

const contentStyle = {
  fontSize: '0.95em',
  lineHeight: '1.55',
  margin: '6px 0 4px',
  overflowWrap: 'anywhere',
};

// Under an empty control: the verdict the model already knew when it
// quoted this again. Quiet, and it wraps onto its own line on a phone.
const ratedBeforeStyle = {
  fontStyle: 'italic',
};

const notAccessibleStyle = {
  display: 'inline-block',
  padding: '4px 8px',
  margin: '4px 0',
  background: 'var(--bg-surface)',
  border: '1px solid var(--border-hover)',
  borderRadius: '4px',
  color: 'var(--text-muted)',
  fontStyle: 'italic',
  fontSize: '0.9em',
};

export default ExternalQuoteBubble;
