import React, { useState, useEffect } from 'react';
import api from '../api';
import { useUser } from '../contexts/UserContext';

// Download PoC (Feature 2, dark behind DOWNLOAD_V1): the user's own saved
// external content (bookmarks, Community Archive tweets) most relevant to
// the thread they're writing in — query composed server-side from thread
// tail + intentions + profile. A fixed left rail mirroring the admin
// SemanticNeighbors right rail; renders nothing when the flag is off, the
// user has no imported items, or nothing clears the relevance floor. No
// scores shown — this is an offering, not a dashboard.
export default function RecommendationsPanel({ nodeId }) {
  const { user } = useUser();
  const enabled = !!(user && user.download_v1_enabled);
  const [items, setItems] = useState([]);
  const [loaded, setLoaded] = useState(false);
  // Fixed left rail; below this width it would overlap the thread column.
  const [wide, setWide] = useState(
    typeof window !== 'undefined' ? window.innerWidth >= 1200 : true);

  useEffect(() => {
    if (!enabled || !nodeId) return undefined;
    let cancelled = false;
    setLoaded(false);
    api.get('/external/recommendations', { params: { node_id: nodeId } })
      .then((res) => {
        if (!cancelled) { setItems(res.data.items || []); setLoaded(true); }
      })
      .catch(() => {
        if (!cancelled) { setItems([]); setLoaded(true); }
      });
    return () => { cancelled = true; };
  }, [nodeId, enabled]);

  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= 1200);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  if (!enabled || !loaded || items.length === 0 || !wide) return null;

  return (
    <div style={{
      position: 'fixed',
      top: '120px',
      left: '24px',
      width: '260px',
      maxHeight: 'calc(100vh - 140px)',
      overflowY: 'auto',
      display: 'flex',
      flexDirection: 'column',
      gap: '8px',
      zIndex: 50,
    }}>
      <div style={{
        fontSize: '0.62rem', letterSpacing: '0.16em', textTransform: 'uppercase',
        color: 'var(--accent)', opacity: 0.6, fontFamily: 'var(--sans)',
        marginBottom: '2px',
      }}>
        Related, from your saves
      </div>
      {items.map((item) => (
        <a
          key={item.id}
          href={item.url || undefined}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            textDecoration: 'none',
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            padding: '8px 10px',
            cursor: item.url ? 'pointer' : 'default',
            fontFamily: 'var(--sans)',
            transition: 'border-color 0.15s ease',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--border-hover)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          {item.author_handle && (
            <div style={{
              fontSize: '0.6rem', color: 'var(--text-muted)', marginBottom: '4px',
            }}>
              @{item.author_handle}
            </div>
          )}
          <div style={{
            fontSize: '0.75rem', color: 'var(--text-secondary)', lineHeight: 1.4,
            display: '-webkit-box', WebkitLineClamp: 4, WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}>
            {item.content}
          </div>
        </a>
      ))}
    </div>
  );
}
