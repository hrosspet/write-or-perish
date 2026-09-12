import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import Bubble from '../components/Bubble';
import ReferenceFooter from '../components/ReferenceFooter';
import DeleteConfirmDialog from '../components/DeleteConfirmDialog';
import { useToast } from '../contexts/ToastContext';
import { asCardNode, sourceLabel } from '../utils/references';

/**
 * The References log (#232): everything the user saved from elsewhere
 * (clipped pages, tweets, archive tweets), newest saved first, shown
 * with the Log's cards. Mirrors Feed so the two read as one family;
 * cards open the reference page the way Log cards open a thread.
 */
function ReferencesPage({ onSearchClick }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const { addToast } = useToast();
  const navigate = useNavigate();

  const fetchPage = useCallback((pageNum) => {
    const isFirst = pageNum === 1;
    if (isFirst) setLoading(true);
    else setLoadingMore(true);
    api.get('/external/items', { params: { sort: 'saved', page: pageNum, per_page: 20 } })
      .then((response) => {
        const { items: rows, has_more } = response.data;
        setItems((prev) => (isFirst ? rows : [...prev, ...rows]));
        setHasMore(!!has_more);
        setPage(pageNum);
      })
      .catch((err) => {
        console.error(err);
        setError('Error loading references.');
      })
      .finally(() => {
        setLoading(false);
        setLoadingMore(false);
      });
  }, []);

  useEffect(() => { fetchPage(1); }, [fetchPage]);

  useEffect(() => {
    document.title = 'References — Loore';
    return () => { document.title = 'Loore'; };
  }, []);

  // Auto-load on scroll near bottom, as the Log does.
  useEffect(() => {
    if (!hasMore || loading || loadingMore) return undefined;
    const handleScroll = () => {
      const scrollBottom = window.innerHeight + window.scrollY;
      const docHeight = document.documentElement.scrollHeight;
      if (docHeight - scrollBottom < 300) fetchPage(page + 1);
    };
    window.addEventListener('scroll', handleScroll, { passive: true });
    handleScroll();
    return () => window.removeEventListener('scroll', handleScroll);
  }, [hasMore, loading, loadingMore, page, fetchPage]);

  const handleCardClick = (id, e) => {
    if (e && (e.metaKey || e.ctrlKey)) window.open(`/references/${id}`, '_blank');
    else navigate(`/references/${id}`);
  };

  const handleConfirmDelete = () => {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    api.delete(`/external/items/${id}`)
      .then(() => {
        addToast('Reference deleted', 3000);
        setItems((prev) => prev.filter((i) => i.id !== id));
      })
      .catch((err) => {
        console.error(err);
        addToast((err.response && err.response.data && err.response.data.error)
          || 'Error deleting reference.', 4000);
      })
      .finally(() => setDeleteTarget(null));
  };

  if (loading) return <div style={{ padding: '20px', color: 'var(--text-muted)' }}>Loading references...</div>;
  if (error) return <div style={{ padding: '20px', color: 'var(--accent)' }}>{error}</div>;

  return (
    <div style={{ padding: '3rem 2rem 4rem', maxWidth: '720px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2.5rem' }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          {/* The rule runs the width of the title, not the Log's 40px stub. */}
          <div style={{ display: 'inline-block' }}>
            <h2 style={{
              color: 'var(--text-primary)',
              fontFamily: 'var(--serif)',
              fontWeight: 300,
              fontSize: '2rem',
              margin: '0 0 0.8rem 0',
            }}>
              References
            </h2>
            <div style={{ height: '1px', backgroundColor: 'var(--accent)', opacity: 0.5 }} />
          </div>
          {onSearchClick && (
            <button
              type="button"
              onClick={onSearchClick}
              aria-label="Search your references"
              title="Search your references (⌘K)"
              style={{
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                color: 'var(--text-muted)',
                // Larger tap target for mobile without shifting the layout.
                padding: '8px',
                margin: '-8px',
                display: 'flex',
                alignItems: 'center',
                transition: 'color 0.15s ease',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--accent)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-muted)'; }}
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
                strokeLinejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <line x1="20" y1="20" x2="16.05" y2="16.05" />
              </svg>
            </button>
          )}
        </div>
      </div>
      {items.length === 0 ? (
        <p style={{
          color: 'var(--text-muted)',
          fontFamily: 'var(--sans)',
          fontSize: '0.95rem',
          lineHeight: 1.6,
        }}>
          Pages and tweets you save from elsewhere will appear here.
        </p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {items.map((item) => (
            <Bubble
              key={item.id}
              node={asCardNode(item)}
              onClick={handleCardClick}
              footer={<ReferenceFooter item={item} />}
              tag={item.read_at ? `Read · ${sourceLabel(item)}` : sourceLabel(item)}
              actions={[
                ...(item.url ? [{
                  label: 'Open source',
                  action: () => window.open(item.url, '_blank', 'noopener,noreferrer'),
                  color: 'var(--text-primary)',
                }] : []),
                {
                  label: 'Delete',
                  action: () => setDeleteTarget(item),
                  color: 'var(--accent)',
                },
              ]}
            />
          ))}
        </div>
      )}
      {loadingMore && <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)' }}>Loading more...</div>}
      {hasMore && !loadingMore && (
        <div
          style={{ padding: '20px', textAlign: 'center', cursor: 'pointer', color: 'var(--text-muted)' }}
          onClick={() => fetchPage(page + 1)}
        >
          Load more...
        </div>
      )}
      <DeleteConfirmDialog
        open={deleteTarget !== null}
        mode="reference"
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleConfirmDelete}
      />
    </div>
  );
}

export default ReferencesPage;
