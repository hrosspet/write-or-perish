import React, { useState, useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import api from '../api';
import MarkdownBody from '../components/MarkdownBody';
import BubbleKebabMenu from '../components/BubbleKebabMenu';
import ReferenceFooter from '../components/ReferenceFooter';
import DeleteConfirmDialog from '../components/DeleteConfirmDialog';
import { useToast } from '../contexts/ToastContext';
import TweetEmbed from '../components/TweetEmbed';
import { bodyWithoutTitle, sourceLabel, tweetId } from '../utils/references';

// The focal card of a thread (NodeDetail's highlightedTextStyle), so a
// reference opens the way a node does.
const cardStyle = {
  boxSizing: 'border-box',
  padding: '1.8rem 2rem',
  margin: '18px 0 10px 0',
  backgroundColor: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderLeft: '3px solid var(--accent)',
  borderRadius: '10px',
  width: 'calc(100% - 50px)',
  maxWidth: '1500px',
  marginLeft: '20px',
  position: 'relative',
};

const tagStyle = {
  fontFamily: 'var(--sans)',
  fontSize: '0.65rem',
  fontWeight: 500,
  textTransform: 'uppercase',
  letterSpacing: '0.08em',
  padding: '3px 8px',
  borderRadius: '4px',
  color: 'var(--accent-dim)',
  backgroundColor: 'var(--accent-subtle)',
};

/**
 * One saved reference (#232), read in full. Same shell as a thread's
 * focal node: heading row, accent-edged card, kebab with the actions,
 * footer with author, date and source link.
 */
function ReferenceDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { addToast } = useToast();
  const [item, setItem] = useState(null);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  // For tweets: the embed's state decides whether the stored text is
  // the main body (embed unavailable) or a disclosure under the embed.
  const [embedStatus, setEmbedStatus] = useState('loading');
  const [showStored, setShowStored] = useState(false);

  useEffect(() => {
    setItem(null);
    setError('');
    api.get(`/external/items/${id}`)
      .then((res) => setItem(res.data))
      .catch((err) => {
        setError(err.response && err.response.status === 404
          ? 'This reference does not exist or was deleted.'
          : 'Error loading reference.');
      });
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

  if (error) return <div style={{ padding: '20px', color: 'var(--accent)' }}>{error}</div>;
  if (!item) return <div style={{ padding: '20px', color: 'var(--text-muted)' }}>Loading...</div>;

  const actions = [
    ...(item.url ? [{
      label: 'Open source',
      action: () => window.open(item.url, '_blank', 'noopener,noreferrer'),
      color: 'var(--text-primary)',
    }] : []),
    { label: 'Delete', action: () => setDeleting(true), color: 'var(--accent)' },
  ];

  return (
    <div style={{ padding: '8px 12px 12px' }}>
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
      <div style={cardStyle}>
        <BubbleKebabMenu visible={true} items={actions} />
        {item.title && (
          <h1 style={{
            fontFamily: 'var(--serif)', fontWeight: 400, fontSize: '1.5rem',
            color: 'var(--text-primary)', margin: '0 0 0.8rem 0', lineHeight: 1.3,
          }}>
            {item.title}
          </h1>
        )}
        {tweetId(item) && (
          <TweetEmbed tweetId={tweetId(item)} onStatus={setEmbedStatus} />
        )}
        {tweetId(item) && embedStatus === 'shown' ? (
          <div style={{ marginTop: '10px' }}>
            <button
              type="button"
              onClick={() => setShowStored((v) => !v)}
              style={{
                background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
                color: 'var(--text-muted)',
              }}
            >
              {showStored ? 'Hide stored text' : 'Show stored text'}
            </button>
            {showStored && (
              <div style={{
                marginTop: '8px', fontFamily: 'var(--sans)', fontSize: '0.9rem',
                fontWeight: 300, color: 'var(--text-secondary)', lineHeight: 1.7,
              }}>
                <MarkdownBody>{item.content || ''}</MarkdownBody>
              </div>
            )}
          </div>
        ) : (
          // Pages, and tweets whose embed is still loading or unavailable
          // (deleted tweet, blocked script, offline): the stored text.
          <div style={{
            fontFamily: 'var(--sans)', fontSize: '0.95rem', fontWeight: 300,
            color: 'var(--text-secondary)', lineHeight: 1.7,
          }}>
            <MarkdownBody>{bodyWithoutTitle(item)}</MarkdownBody>
          </div>
        )}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <ReferenceFooter item={item} />
          <span style={{ ...tagStyle, marginTop: '12px' }}>{sourceLabel(item)}</span>
        </div>
      </div>
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
