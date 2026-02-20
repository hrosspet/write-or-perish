import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import api from '../api';

export default function PromptsPage() {
  const [prompts, setPrompts] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get('/prompts/')
      .then(res => {
        setPrompts(res.data.prompts);
        setLoading(false);
      })
      .catch(err => {
        console.error('Failed to load prompts:', err);
        setLoading(false);
      });
  }, []);

  const formatDate = (iso) => {
    if (!iso) return 'default';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const generatedByLabel = (g) => {
    if (g === 'default') return 'default';
    if (g === 'user') return 'edited manually';
    if (g === 'revert') return 'reverted';
    return g;
  };

  if (loading) {
    return (
      <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
        <p style={{ color: 'var(--text-muted)' }}>Loading...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
      <h1 style={{
        fontFamily: 'var(--serif)',
        fontSize: '2rem',
        fontWeight: 300,
        color: 'var(--text-primary)',
        margin: '0 0 8px 0',
      }}>
        Prompts
      </h1>

      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.75rem',
        fontWeight: 300,
        color: 'var(--text-muted)',
        margin: '0 0 16px 0',
        opacity: 0.7,
      }}>
        System prompts that power Loore's AI features
      </p>

      {/* Accent divider */}
      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* Prompt cards */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '1px' }}>
        {prompts.map((p) => (
          <Link
            key={p.prompt_key}
            to={`/prompts/${p.prompt_key}`}
            style={{
              display: 'block',
              padding: '20px 0',
              borderBottom: '1px solid var(--border)',
              textDecoration: 'none',
              transition: 'opacity 0.2s ease',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px', marginBottom: '6px' }}>
              <span style={{
                fontFamily: 'var(--sans)',
                fontSize: '0.95rem',
                fontWeight: 400,
                color: 'var(--text-primary)',
              }}>
                {p.title}
              </span>
              <span style={{
                fontFamily: 'var(--sans)',
                fontSize: '0.7rem',
                fontWeight: 300,
                color: 'var(--text-muted)',
                opacity: 0.7,
              }}>
                v{p.version_number} &middot; {generatedByLabel(p.generated_by)} &middot; {formatDate(p.created_at)}
              </span>
            </div>
            <p style={{
              fontFamily: 'var(--sans)',
              fontSize: '0.8rem',
              fontWeight: 300,
              color: 'var(--text-muted)',
              margin: 0,
              lineHeight: 1.5,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>
              {p.preview}
            </p>
          </Link>
        ))}
      </div>
    </div>
  );
}
