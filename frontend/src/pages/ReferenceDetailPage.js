import React, { useState, useEffect, useRef } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { FiArrowUpRight, FiFileText, FiMessageSquare, FiPlayCircle } from 'react-icons/fi';
import api from '../api';
import MarkdownBody from '../components/MarkdownBody';
import BubbleKebabMenu from '../components/BubbleKebabMenu';
import DeleteConfirmDialog from '../components/DeleteConfirmDialog';
import ReferenceEditForm from '../components/ReferenceEditForm';
import NodeFormModal from '../components/NodeFormModal';
import RegenerateTtsDialog from '../components/RegenerateTtsDialog';
import SpeakerIcon from '../components/SpeakerIcon';
import ReferenceFeedback, { TWEET_LABELS } from '../components/ReferenceFeedback';
import { useToast } from '../contexts/ToastContext';
import { formatDate, formatDateTime } from '../utils/date';
import YouTubeEmbed from '../components/YouTubeEmbed';
import {
  bodyWithoutTitle, referenceKind, sourceLine, sourceLinkLabel, youtubeVideo,
} from '../utils/references';

// The card sits where a thread's focal node sits, with the same gutter
// on the right for the kebab, which stays outside the card. Its look is
// in index.css (.ref-card). The line under the card keeps its indent.
const CARD_INDENT = '20px';
const cardPlacement = {
  width: 'calc(100% - 50px)',
  margin: `18px 0 0 ${CARD_INDENT}`,
};

const SOURCE_GLYPH = {
  tweet: FiMessageSquare,
  video: FiPlayCircle,
  page: FiFileText,
};

// The SpeakerIcon button at the end of the byline: a larger tap target
// than the glyph (10 px above and below, 8 px each side). The wrapper's
// negative margins (.ref-card-listen) take the padding back out of the
// layout, so the byline keeps its height and the glyph its right-edge
// position.
const listenButtonStyle = { padding: '10px 8px', marginLeft: 0 };

/**
 * Who wrote it and when, under the source line: a tweet's display name
 * and @handle (opening the account), a page's or video's byline, then
 * the date. The site is already named in the source line.
 */
function Byline({ item, listen }) {
  const when = formatDateTime(item.posted_at || item.fetched_at);
  let author = null;
  if (referenceKind(item) === 'tweet' && item.author_handle) {
    author = (
      <a
        className="ref-card-author"
        href={`https://x.com/${item.author_handle}`}
        target="_blank"
        rel="noopener noreferrer"
        title="Open the account on Twitter in a new tab"
      >
        {item.author_name && <span className="ref-card-name">{item.author_name}</span>}
        <span className="ref-card-handle">@{item.author_handle}</span>
      </a>
    );
  } else if (item.author_name || item.author_handle) {
    author = <span className="ref-card-name">{item.author_name || item.author_handle}</span>;
  }
  if (!author && !when && !listen) return null;
  return (
    <div className="ref-card-byline-row" data-testid="reference-byline">
      <div className="ref-card-byline">
        {author}
        {when && <span className="ref-card-date">{author ? `· ${when}` : when}</span>}
      </div>
      {listen && <span className="ref-card-listen">{listen}</span>}
    </div>
  );
}

/**
 * The title with the link to the original as an icon after it. The icon
 * is joined to the title's last word in a no-wrap span, so a long title
 * never leaves it alone on a line. The heading is named by the title
 * (aria-label), not by the link inside it.
 */
function TitleWithLink({ title, link }) {
  if (!link) return <h1 className="ref-card-title">{title}</h1>;
  const { iconName, ...anchor } = link;
  const cut = title.search(/\S+\s*$/);
  return (
    <h1 className="ref-card-title" aria-label={title}>
      {title.slice(0, cut)}
      <span className="ref-card-title-tail">
        {title.slice(cut)}
        <a className="ref-card-title-link" aria-label={iconName} {...anchor}>
          <FiArrowUpRight aria-hidden="true" strokeWidth={1.6} />
        </a>
      </span>
    </h1>
  );
}

/**
 * One saved reference (#232), read in full. Option A of the 2026-10-13
 * mockup (Peter): the gleaning's tweet card at page size, for every kind
 * of reference. The card has a full hairline border and no accent rail,
 * so it never reads as one of the user's own entries. Top to bottom: the
 * source in a plain sentence, the byline, a page's title, the stored
 * text (a video's player, its description behind a toggle), then Listen,
 * the link out, the verdict and the read toggle. No tweet embed: the
 * stored text is the tweet. Under the card, how often Loore showed it.
 */
function ReferenceDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  // Opened from a card in a thread (a gleaning, #435): the link back to it.
  const location = useLocation();
  const backTo = location.state && location.state.backTo;
  const backLabel = (location.state && location.state.backLabel) || 'Back to the thread';
  // The reply the card was in: the open mark is logged against it (#352).
  const fromNodeId = location.state && location.state.nodeId;
  const { addToast } = useToast();
  const [item, setItem] = useState(null);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  // A video's description sits under the player behind this toggle.
  const [showStored, setShowStored] = useState(false);
  const [marking, setMarking] = useState(false);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  // An edit that changes the text of a reference with generated audio:
  // ask keep-or-regenerate first, as a node edit does (#66). The pending
  // edit waits here for the answer.
  const [pendingEdit, setPendingEdit] = useState(null);

  // Opening the page is reading it (Peter, 2026-10-14): once per open,
  // the page loads the reference and then logs the open
  // ({via: 'open', node_id}) as "Twitter ↗" does, which marks an unread
  // one read (a read one keeps its date). The load and the mark are one
  // promise kept in a ref, so React StrictMode's second effect run
  // reuses it instead of logging the open twice, and the page shows the
  // marked state at once. Only the owner can open the page (404 for
  // anyone else), so there is no one else to exclude. "Mark as unread"
  // afterwards sticks: nothing marks again until the page is reopened.
  const opening = useRef({ id: null, promise: null });
  useEffect(() => {
    setItem(null);
    setError('');
    if (opening.current.id !== id) {
      const promise = api.get(`/external/items/${id}`).then((res) => {
        const loaded = res.data;
        const body = { via: 'open', ...(fromNodeId ? { node_id: fromNodeId } : {}) };
        return Promise.resolve(api.post(`/external/items/${loaded.id}/read`, body))
          .then((marked) => ({ ...loaded, read_at: marked.data.read_at }))
          .catch(() => loaded);
      });
      opening.current = { id, promise };
    }
    opening.current.promise
      .then((loaded) => setItem(loaded))
      .catch((err) => {
        setError(err.response && err.response.status === 404
          ? 'This reference does not exist or was deleted.'
          : 'Error loading reference.');
      });
    // fromNodeId is read once per open; a new id is a new open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!item) return undefined;
    const first = item.title || (item.content || '').split('\n').find((l) => l.trim()) || '';
    document.title = first ? `${first.replace(/^#+\s*/, '')} — Loore` : 'Loore';
    return () => { document.title = 'Loore'; };
  }, [item]);

  const confirmDelete = () => {
    api.delete(`/external/items/${item.id}`)
      .then(() => {
        addToast('Reference deleted', 3000);
        navigate('/references');
      })
      .catch((err) => {
        addToast((err.response && err.response.data && err.response.data.error)
          || 'Error deleting reference.', 4000);
      })
      .finally(() => setDeleting(false));
  };

  // Only the user marks a reference read (opening this page does, above).
  // The AI surfacing it is tracked separately as surfaced_count.
  const setRead = (want, via) => {
    setMarking(true);
    const req = want
      ? api.post(`/external/items/${item.id}/read`, via ? { via } : {})
      : api.delete(`/external/items/${item.id}/read`);
    req
      .then((res) => setItem((prev) => ({ ...prev, read_at: res.data.read_at })))
      .catch(() => addToast('Could not update the read mark.', 4000))
      .finally(() => setMarking(false));
  };

  // Opening the original is reading it, as on the gleaning card: the
  // open is logged and an unread reference is marked read (a read one
  // keeps its date). The link itself opens the tab.
  const openedOriginal = () => {
    if (!marking) setRead(true, 'open');
  };

  // The link to the original: by the title (an icon after it) or, with no
  // title, as text in the title's place. Nothing when there is no URL.
  const linkLabel = item ? sourceLinkLabel(item) : '';
  const linkName = linkLabel.replace(/\s*↗$/, '');
  const originalLink = item && item.url ? {
    href: item.url,
    target: '_blank',
    rel: 'noopener noreferrer',
    onClick: openedOriginal,
    iconName: linkName === 'Open page' ? 'Open page' : `Open on ${linkName}`,
  } : null;

  const { iconName: _unused, ...anchor } = originalLink || {};
  const saveEdit = ({ title, content }, regenerateTts) => {
    const textChanged = content.trim() !== (item.content || '').trim();
    if (item.has_tts && textChanged && regenerateTts === undefined) {
      setPendingEdit({ title, content });
      return;
    }
    setSaving(true);
    api.put(`/external/items/${item.id}`, {
      title,
      content,
      ...(regenerateTts && { regenerate_tts: true }),
    })
      .then((res) => {
        setItem(res.data);
        setEditing(false);
        addToast('Reference updated', 3000);
      })
      .catch((err) => {
        addToast((err.response && err.response.data && err.response.data.error)
          || 'Error saving reference.', 4000);
      })
      .finally(() => setSaving(false));
  };

  if (error) return <div style={{ padding: '20px', color: 'var(--accent)' }}>{error}</div>;
  if (!item) return <div style={{ padding: '20px', color: 'var(--text-muted)' }}>Loading...</div>;

  const video = youtubeVideo(item);
  const Glyph = SOURCE_GLYPH[referenceKind(item)];
  const body = bodyWithoutTitle(item);

  const actions = [
    ...(item.url ? [{
      label: 'Open source',
      action: () => window.open(item.url, '_blank', 'noopener,noreferrer'),
      color: 'var(--text-primary)',
    }] : []),
    { label: 'Edit', action: () => setEditing(true), color: 'var(--text-primary)' },
    { label: 'Delete', action: () => setDeleting(true), color: 'var(--accent)' },
  ];

  return (
    <div style={{ padding: '8px 12px 12px', maxWidth: '820px', margin: '0 auto' }}>
      {backTo && (
        <Link
          to={backTo}
          style={{
            display: 'inline-block', padding: '4px 0', marginBottom: '6px',
            fontFamily: 'var(--sans)', fontSize: '0.86rem', fontWeight: 300,
            color: 'var(--text-muted)', textDecoration: 'none',
          }}
        >
          {`‹ ${backLabel}`}
        </Link>
      )}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: '16px',
        marginBottom: '12px',
      }}>
        <h2 style={{
          fontFamily: 'var(--serif)',
          fontWeight: 300,
          fontSize: '1.8rem',
          color: 'var(--text-primary)',
          margin: 0,
        }}>Reference</h2>
        <Link
          to="/references"
          style={{
            fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
            color: 'var(--text-muted)', textDecoration: 'none',
          }}
        >
          All references
        </Link>
      </div>
      <article className="ref-card" style={cardPlacement}>
        <BubbleKebabMenu visible={true} items={actions} />
        <span className="ref-card-source">
          <Glyph aria-hidden="true" strokeWidth={1.6} />
          {sourceLine(item)}
        </span>
        <Byline
          item={item}
          listen={(
            <SpeakerIcon
              itemId={item.id}
              content={item.title ? `# ${item.title}\n${item.content || ''}` : item.content}
              isPublic={false}
              buttonStyle={listenButtonStyle}
              onTtsGenerated={() => setItem((prev) => (prev ? { ...prev, has_tts: true } : prev))}
            />
          )}
        />
        {item.title ? (
          <TitleWithLink title={item.title} link={originalLink} />
        ) : originalLink && (
          // No title: the link stands where the title would be.
          <a className="ref-card-out ref-card-out-titleless" {...anchor}>
            {linkLabel}
          </a>
        )}
        {video && <YouTubeEmbed videoId={video.id} start={video.start} />}
        {video ? (
          // A video's stored text is its description (with the chapter
          // list): under the player, behind the toggle.
          <>
            <button
              type="button"
              className="ref-card-stored-toggle"
              aria-expanded={showStored}
              onClick={() => setShowStored((v) => !v)}
            >
              {showStored ? 'Hide stored text' : 'Show stored text'}
            </button>
            {showStored && (
              <div className="ref-card-text" data-testid="reference-text">
                <MarkdownBody>{body}</MarkdownBody>
              </div>
            )}
          </>
        ) : (
          <div className="ref-card-text" data-testid="reference-text">
            <MarkdownBody>{body}</MarkdownBody>
          </div>
        )}
        <div className="ref-card-foot">
          <span className="ref-card-spacer" />
          {/* No gap: ReferenceFeedback spaces itself from the button. */}
          <span style={{ display: 'inline-flex', alignItems: 'center' }}>
            <ReferenceFeedback
              itemId={item.id}
              feedback={item.feedback}
              size={18}
              // A tweet's verdict is named as on its gleaning pick (#502);
              // other references keep "Good quote" / "Bad quote".
              labels={referenceKind(item) === 'tweet' ? TWEET_LABELS : undefined}
              onChange={(fb, data) => setItem((prev) => ({
                ...prev, feedback: fb, read_at: (data && data.read_at) || prev.read_at,
              }))}
            />
            <button
              type="button"
              className="ref-card-read"
              data-read={item.read_at ? 'true' : 'false'}
              onClick={() => setRead(!item.read_at)}
              disabled={marking}
            >
              {item.read_at ? 'Mark as unread' : 'Mark as read'}
            </button>
          </span>
        </div>
      </article>
      <p className="ref-card-shown" style={{ marginLeft: CARD_INDENT }}>
        {item.surfaced_count
          ? `Shown by Loore ${item.surfaced_count}× · last ${formatDate(item.last_surfaced_at)}`
          : 'Not yet shown by Loore in a conversation'}
        {item.read_at ? ` · you read it ${formatDate(item.read_at)}` : ''}
        {item.edited_at ? ` · edited ${formatDate(item.edited_at)}` : ''}
      </p>
      {editing && (
        <NodeFormModal title="Edit Reference" onClose={() => setEditing(false)}>
          <ReferenceEditForm
            key={item.id}
            item={item}
            saving={saving}
            onSave={saveEdit}
            onCancel={() => setEditing(false)}
          />
        </NodeFormModal>
      )}
      <RegenerateTtsDialog
        open={!!pendingEdit}
        onClose={() => setPendingEdit(null)}
        onChoice={(regenerate) => {
          const edit = pendingEdit;
          setPendingEdit(null);
          if (edit) saveEdit(edit, regenerate);
        }}
      />
      <DeleteConfirmDialog
        open={deleting}
        mode="reference"
        onClose={() => setDeleting(false)}
        onConfirm={confirmDelete}
      />
    </div>
  );
}

export default ReferenceDetailPage;
