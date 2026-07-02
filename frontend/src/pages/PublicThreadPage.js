import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../api';
import MarkdownBody from '../components/MarkdownBody';
import { formatDate } from '../utils/date';

/**
 * PublicThreadPage — the logged-out view of /node/:id (#228)
 *
 * A public node and its public discussion, readable without an account —
 * the funnel. Self-contained shell like PublicSharePage: fonts and CSS
 * vars load globally for logged-out visitors too.
 */

function authorLine(node) {
  if (node.node_type === 'llm') {
    return node.username
      ? `${node.llm_model} · via ${node.username}`
      : `Loore AI · ${node.llm_model}`;
  }
  return node.username;
}

/**
 * One node's card. The focused node (deep-link target) gets a faint accent
 * border so arriving readers land visibly.
 */
function ThreadNode({ node, focusId, isRoot }) {
  const focused = node.id === focusId;
  return (
    <>
      <div style={{
        background: 'var(--bg-card)',
        // Faint accent border marks the deep-link target so arriving
        // readers land visibly.
        border: focused
          ? '1px solid var(--accent)'
          : '1px solid var(--border)',
        borderRadius: '12px',
        padding: isRoot ? '32px 36px' : '20px 24px',
        marginBottom: '16px',
      }}>
        <div style={{
          fontFamily: 'var(--sans)',
          fontSize: isRoot ? '0.8rem' : '0.75rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginBottom: isRoot ? '14px' : '10px',
        }}>
          {authorLine(node)}
        </div>
        <div style={{
          fontFamily: 'var(--sans)',
          fontSize: isRoot ? '1rem' : '0.95rem',
          fontWeight: 300,
          color: 'var(--text-secondary)',
          lineHeight: 1.75,
        }}>
          <MarkdownBody>{node.content}</MarkdownBody>
        </div>
        <div style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.7rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          opacity: 0.7,
          marginTop: '14px',
        }}>
          {formatDate(node.created_at, { relative: false })}
        </div>
      </div>
      {node.children && node.children.length > 0 && (
        <div style={{
          marginLeft: isRoot ? '0' : '20px',
          paddingLeft: '16px',
          borderLeft: '2px solid var(--border)',
        }}>
          {node.children.map((child) => (
            <ThreadNode key={child.id} node={child} focusId={focusId} isRoot={false} />
          ))}
        </div>
      )}
    </>
  );
}

export default function PublicThreadPage({ nodeIdOverride }) {
  // Permalink route (/u/:username/:slug) resolves the id itself and
  // passes it in; the plain /node/:id route reads params.
  const params = useParams();
  const id = nodeIdOverride || params.id;
  const navigate = useNavigate();
  // 'loading' | 'ok' | 'notfound'
  const [status, setStatus] = useState('loading');
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    api.get(`/forum/node/${id}`)
      .then((res) => {
        if (cancelled) return;
        setData(res.data);
        setStatus('ok');
      })
      .catch(() => {
        // 404 covers missing, private, deleted, and feature-off —
        // deliberately indistinguishable.
        if (!cancelled) setStatus('notfound');
      });
    return () => { cancelled = true; };
  }, [id]);

  const shell = (children) => (
    <div style={{
      minHeight: '100vh', background: 'var(--bg-deep)',
      color: 'var(--text-primary)', fontFamily: 'var(--sans)',
    }}>
      <div style={{ maxWidth: '720px', margin: '0 auto', padding: '72px 24px 120px' }}>
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
      <ThreadNode node={data.thread} focusId={data.focus_id} isRoot />

      {data.truncated && (
        <p style={{
          textAlign: 'center', color: 'var(--text-muted)',
          fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
          opacity: 0.8, marginTop: '8px',
        }}>
          thread truncated
        </p>
      )}

      {/* The funnel: warm, not pushy. */}
      <div style={{ textAlign: 'center', marginTop: '72px' }}>
        <p style={{
          fontFamily: 'var(--serif)', fontSize: '1.25rem', fontWeight: 300,
          color: 'var(--text-primary)', lineHeight: 1.6,
          margin: '0 0 24px 0',
        }}>
          Respond, or ask the AI about this thread — from your own Loore.
        </p>
        <button
          type="button"
          onClick={() => navigate(`/login?returnUrl=${encodeURIComponent('/node/' + id)}`)}
          style={{
            background: 'var(--accent)',
            color: 'var(--bg-deep)',
            border: 'none',
            borderRadius: '8px',
            padding: '12px 28px',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 400,
            letterSpacing: '0.02em',
            cursor: 'pointer',
            transition: 'opacity 0.15s ease',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.85'; }}
          onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
        >
          Sign in to respond
        </button>
      </div>
    </>
  );
}
