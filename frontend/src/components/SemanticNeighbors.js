import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import { formatDate } from '../utils/date';

// Admin-only inspector (#155/#197): the 5 nodes most semantically similar to
// the focal node — using the node's OWN embedding as the query, so it's the
// guess-free "source zero" neighborhood. A fixed panel to the right of the
// thread; lets us feel out what semantic retrieval surfaces from any point in
// the archive without going through the agentic flow. Renders nothing for
// non-admins, or when the node isn't embedded yet (e.g. ai_usage='none', or
// the sweep hasn't reached it).
export default function SemanticNeighbors({ nodeId }) {
  const { user } = useUser();
  const isAdmin = !!(user && user.is_admin);
  const [neighbors, setNeighbors] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    if (!isAdmin || !nodeId) return undefined;
    let cancelled = false;
    setLoaded(false);
    api.get('/search/neighbors', { params: { node_id: nodeId, limit: 5 } })
      .then((res) => {
        if (!cancelled) { setNeighbors(res.data.results || []); setLoaded(true); }
      })
      .catch(() => {
        if (!cancelled) { setNeighbors([]); setLoaded(true); }
      });
    return () => { cancelled = true; };
  }, [nodeId, isAdmin]);

  if (!isAdmin || !loaded || neighbors.length === 0) return null;

  return (
    <div style={{
      position: 'fixed',
      top: '96px',
      right: '24px',
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
        marginBottom: '2px', display: 'flex', alignItems: 'center', gap: '6px',
      }}>
        Semantically near
        <span title="Admin-only: nearest nodes by embedding similarity" style={{
          fontSize: '0.5rem', opacity: 0.6, border: '1px solid var(--border)',
          borderRadius: '3px', padding: '0 3px', letterSpacing: '0.08em',
        }}>admin</span>
      </div>
      {neighbors.map((n) => (
        <button
          key={n.id}
          onClick={() => navigate(`/node/${n.id}`)}
          style={{
            textAlign: 'left',
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            padding: '8px 10px',
            cursor: 'pointer',
            fontFamily: 'var(--sans)',
            transition: 'border-color 0.15s ease',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--border-hover)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            marginBottom: '4px', gap: '6px',
          }}>
            <span style={{ fontSize: '0.6rem', color: 'var(--text-muted)' }}>
              {n.username} · {formatDate(n.created_at)}
            </span>
            <span style={{
              fontSize: '0.6rem', color: 'var(--accent)',
              background: 'var(--accent-subtle)', borderRadius: '3px',
              padding: '0 4px', flexShrink: 0,
            }} title="cosine relevance">
              {Math.round(n.score * 100)}%
            </span>
          </div>
          <div style={{
            fontSize: '0.75rem', color: 'var(--text-secondary)', lineHeight: 1.4,
            display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}>
            {n.preview}
          </div>
        </button>
      ))}
    </div>
  );
}
