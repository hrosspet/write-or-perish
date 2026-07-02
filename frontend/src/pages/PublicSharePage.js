import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../api';
import MarkdownBody from '../components/MarkdownBody';
import { FaThumbtack } from 'react-icons/fa';
import { formatDate } from '../utils/date';

/**
 * PublicSharePage — /@:username
 *
 * The one outward-facing surface of Upload v1: a person's published shares,
 * readable without an account. Deliberately quiet — no app chrome required,
 * no metrics, just the writing. Fonts and CSS vars come from index.html /
 * index.css, which load for logged-out visitors too (same as the landing
 * page).
 */
export default function PublicSharePage({ usernameOverride }) {
  const params = useParams();
  const username = usernameOverride || params.username;
  const navigate = useNavigate();
  // 'loading' | 'ok' | 'notfound'
  const [status, setStatus] = useState('loading');
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    api.get(`/share/public/${encodeURIComponent(username)}`)
      .then((res) => {
        if (cancelled) return;
        setData(res.data);
        setStatus('ok');
      })
      .catch(() => {
        // 404 covers both "unknown user" and "feature off" — deliberately
        // indistinguishable.
        if (!cancelled) setStatus('notfound');
      });
    return () => { cancelled = true; };
  }, [username]);

  const shell = (children) => (
    <div style={{
      minHeight: '100vh', background: 'var(--bg-deep)',
      color: 'var(--text-primary)', fontFamily: 'var(--sans)',
    }}>
      <div style={{ maxWidth: '640px', margin: '0 auto', padding: '96px 24px 120px' }}>
        {children}
      </div>
    </div>
  );

  if (status === 'loading') {
    return shell(null);
  }

  if (status === 'notfound') {
    return shell(
      <p style={{
        textAlign: 'center', color: 'var(--text-muted)',
        fontFamily: 'var(--sans)', fontSize: '0.9rem', fontWeight: 300,
        marginTop: '20vh',
      }}>
        Nothing here.
      </p>
    );
  }

  return shell(
    <>
      <h1 style={{
        fontFamily: 'var(--serif)', fontSize: '2.4rem', fontWeight: 300,
        color: 'var(--text-primary)', margin: '0 0 40px 0',
        textAlign: 'center',
      }}>
        {data.username}
      </h1>

      {data.shares.length === 0 && (
        <p style={{
          textAlign: 'center', color: 'var(--text-muted)',
          fontSize: '0.9rem', fontWeight: 300,
        }}>
          {data.username}'s public entries will appear here as they share thoughts with Loore.
        </p>
      )}

      {data.shares.map((share) => (
        <div
          key={share.public_node_id || share.id}
          onClick={(e) => {
            if (!share.public_node_id) return;
            const target = share.permalink || `/node/${share.public_node_id}`;
            if (e.metaKey || e.ctrlKey) {
              window.open(target, '_blank', 'noopener');
            } else {
              navigate(target);
            }
          }}
          onMouseEnter={(e) => { if (share.public_node_id) e.currentTarget.style.borderColor = 'var(--border-hover)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
          style={{
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '12px', padding: '32px 36px', marginBottom: '24px',
            cursor: share.public_node_id ? 'pointer' : 'default',
            transition: 'border-color 0.15s ease',
          }}
        >
          {share.share_type && (
            <div style={{
              fontFamily: 'var(--sans)', fontSize: '0.65rem', fontWeight: 300,
              letterSpacing: '0.14em', textTransform: 'uppercase',
              color: 'var(--text-muted)', marginBottom: '12px',
            }}>
              {share.share_type}
            </div>
          )}
          <div style={{
            fontFamily: 'var(--sans)', fontSize: '0.95rem', fontWeight: 300,
            color: 'var(--text-secondary)', lineHeight: 1.75,
          }}>
            <MarkdownBody>{share.content}</MarkdownBody>
          </div>
          <div style={{
            fontFamily: 'var(--sans)', fontSize: '0.7rem', fontWeight: 300,
            color: 'var(--text-muted)', opacity: 0.7, marginTop: '16px',
            display: 'flex', alignItems: 'center', gap: '8px',
          }}>
            <span>{formatDate(share.published_at, { relative: false })}</span>
            {share.pinned && (
              <FaThumbtack
                title="Pinned"
                style={{ color: 'var(--accent)', opacity: 0.8, fontSize: '0.7rem' }}
              />
            )}
          </div>
        </div>
      ))}
    </>
  );
}
