import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import api from '../api';
import MarkdownBody from '../components/MarkdownBody';
import { formatDate } from '../utils/date';
import { useUser } from '../contexts/UserContext';

/**
 * ForumPage — /forum (#228)
 *
 * The Commons: public root nodes by everyone, newest first. The Log's
 * public sibling — same calm card aesthetic, no vanity metrics. Reply
 * counts render as quiet muted text, never a badge.
 */

// Peter may rename the surface — keep the words in one obvious place.
const PAGE_TITLE = 'Commons';
const PAGE_SUBTITLE = 'What people here have chosen to make public.';

function ForumPage() {
  const { user } = useUser();
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);

  const fetchPage = useCallback((pageNum) => {
    const isFirst = pageNum === 1;
    if (isFirst) setLoading(true);
    else setLoadingMore(true);

    api.get(`/forum/feed?page=${pageNum}`)
      .then((response) => {
        const { items: newItems, has_more } = response.data;
        setItems((prev) => (isFirst ? newItems : [...prev, ...newItems]));
        setHasMore(has_more);
        setPage(pageNum);
      })
      .catch((err) => {
        console.error(err);
        setError('Error loading the commons.');
      })
      .finally(() => {
        setLoading(false);
        setLoadingMore(false);
      });
  }, []);

  useEffect(() => {
    fetchPage(1);
  }, [fetchPage]);

  // cmd/ctrl-click opens the thread in a new tab; a plain click navigates
  // in place (mirrors SemanticNeighbors / Cmd+K results).
  const openNode = (e, id) => {
    if (e.metaKey || e.ctrlKey) {
      window.open(`/node/${id}`, '_blank', 'noopener');
    } else {
      navigate(`/node/${id}`);
    }
  };

  // Defense in depth: the nav link is hidden when the flag is off, but the
  // route may still be reachable directly.
  if (user && !user.share_v1_enabled) {
    return (
      <div style={{ padding: '120px 24px', textAlign: 'center' }}>
        <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>
          Not available.
        </p>
      </div>
    );
  }

  if (loading) {
    return <div style={{ padding: '20px', color: 'var(--text-muted)' }}>Loading...</div>;
  }
  if (error) {
    return <div style={{ padding: '20px', color: 'var(--accent)' }}>{error}</div>;
  }

  return (
    <div style={{ padding: '3rem 2rem 4rem', maxWidth: '720px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2.5rem' }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          margin: '0 0 0.5rem 0',
        }}>
          <h2 style={{
            color: 'var(--text-primary)',
            fontFamily: 'var(--serif)',
            fontWeight: 300,
            fontSize: '2rem',
            margin: 0,
          }}>
            {PAGE_TITLE}
          </h2>
          <Link
            to="/share"
            style={{
              fontFamily: 'var(--sans)',
              fontSize: '0.78rem',
              fontWeight: 300,
              color: 'var(--text-muted)',
              textDecoration: 'none',
              transition: 'color 0.15s ease',
            }}
            onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--accent)'; }}
            onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-muted)'; }}
          >
            your shares →
          </Link>
        </div>
        <p style={{
          color: 'var(--text-muted)',
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          margin: '0 0 0.8rem 0',
        }}>
          {PAGE_SUBTITLE}
        </p>
        <div style={{
          width: '40px',
          height: '1px',
          backgroundColor: 'var(--accent)',
          opacity: 0.5,
        }} />
      </div>

      {items.length === 0 ? (
        <p style={{
          textAlign: 'center',
          color: 'var(--text-muted)',
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          marginTop: '3rem',
        }}>
          Nothing public yet.
        </p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {items.map((item) => (
            <div
              key={item.id}
              onClick={(e) => openNode(e, item.id)}
              style={{
                background: 'var(--bg-card)',
                border: '1px solid var(--border)',
                borderRadius: '12px',
                padding: '24px 28px',
                cursor: 'pointer',
                transition: 'border-color 0.15s ease',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--border-hover)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
            >
              <div style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'baseline',
                gap: '12px',
                fontFamily: 'var(--sans)',
                fontSize: '0.75rem',
                fontWeight: 300,
                color: 'var(--text-muted)',
                marginBottom: '10px',
              }}>
                <span>{item.username}</span>
                <span style={{ whiteSpace: 'nowrap' }}>{formatDate(item.created_at)}</span>
              </div>
              <div style={{
                fontFamily: 'var(--sans)',
                fontSize: '0.95rem',
                fontWeight: 300,
                color: 'var(--text-secondary)',
                lineHeight: 1.7,
              }}>
                <MarkdownBody>{item.content}</MarkdownBody>
              </div>
              {item.reply_count > 0 && (
                <div style={{
                  fontFamily: 'var(--sans)',
                  fontSize: '0.75rem',
                  fontWeight: 300,
                  color: 'var(--text-muted)',
                  opacity: 0.8,
                  marginTop: '12px',
                }}>
                  {item.reply_count} {item.reply_count === 1 ? 'response' : 'responses'}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {loadingMore && (
        <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)' }}>
          Loading more...
        </div>
      )}
      {hasMore && !loadingMore && (
        <div
          style={{ padding: '20px', textAlign: 'center', cursor: 'pointer', color: 'var(--text-muted)' }}
          onClick={() => fetchPage(page + 1)}
        >
          Load more...
        </div>
      )}
    </div>
  );
}

export default ForumPage;
