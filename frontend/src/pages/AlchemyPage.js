import React, { useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import useSubmitShortcut, { submitShortcutHint } from '../hooks/useSubmitShortcut';

/**
 * Alchemical Mode landing (/alchemy) — gated depth-work entry point.
 *
 * Only renders for users with alchemy_status === 'active' (readiness
 * pre-filter + explicit opt-in, both server-side). Mirrors the textmode
 * landing: one prompt, one textarea, then straight into the conversation.
 */
export default function AlchemyPage() {
  const { user } = useUser();
  const navigate = useNavigate();
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const textareaRef = useRef(null);

  const canSubmit = content.trim().length > 0 && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError('');
    try {
      const res = await api.post('/alchemy/start', { content });
      const data = res.data;
      const llmNodeId = data?.llm_node_id;
      if (llmNodeId) {
        // Mirrors WritePage's navigation after /textmode/start: land
        // directly on the pending LLM node so NodeDetail anchors the
        // inline input below it and flips to the response in place.
        navigate(`/node/${llmNodeId}?awaitLlm=${llmNodeId}`);
      } else if (data?.user_node_id) {
        navigate(`/node/${data.user_node_id}`);
      } else {
        setSubmitting(false);
        setError('Unexpected response. Please try again.');
      }
    } catch (err) {
      setSubmitting(false);
      setError(
        err.response?.data?.error ||
        err.response?.data?.message ||
        'Something went wrong. Please try again.'
      );
    }
  };

  useSubmitShortcut(textareaRef, handleSubmit, canSubmit);

  if (user?.alchemy_status !== 'active') {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        minHeight: 'calc(100vh - 120px)',
        padding: '40px 24px',
      }}>
        <p style={{
          fontFamily: 'var(--sans)',
          fontWeight: 300,
          color: 'var(--text-muted)',
          margin: 0,
        }}>
          Not available.
        </p>
      </div>
    );
  }

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'flex-start',
      minHeight: 'calc(100vh - 120px)',
      padding: '60px 24px 40px',
      background: 'radial-gradient(ellipse at 50% 30%, rgba(196,149,106,0.05) 0%, transparent 70%)',
    }}>
      <h1 style={{
        fontFamily: 'var(--serif)',
        fontSize: 'clamp(1.6rem, 3.5vw, 2.2rem)',
        fontWeight: 300,
        color: 'var(--text-primary)',
        margin: '0 0 10px 0',
        textAlign: 'center',
      }}>
        Alchemy
      </h1>
      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.95rem',
        fontWeight: 300,
        color: 'var(--text-muted)',
        margin: '0 0 32px 0',
        textAlign: 'center',
      }}>
        What's alive for you right now?
      </p>

      <div style={{ width: '720px', maxWidth: '90vw', display: 'flex', flexDirection: 'column' }}>
        <textarea
          ref={textareaRef}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="Begin where you are…"
          rows={8}
          style={{
            width: '100%',
            boxSizing: 'border-box',
            padding: '16px',
            fontFamily: 'var(--sans)',
            fontWeight: 300,
            fontSize: '1rem',
            lineHeight: 1.65,
            borderRadius: '10px',
            resize: 'vertical',
          }}
        />

        {error && (
          <p style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.88rem',
            fontWeight: 300,
            color: 'var(--error)',
            margin: '10px 0 0 0',
          }}>
            {error}
          </p>
        )}

        <div style={{
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          gap: '12px',
          marginTop: '14px',
        }}>
          <span style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.75rem',
            fontWeight: 300,
            color: 'var(--text-muted)',
          }}>
            {submitShortcutHint()}
          </span>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            style={{
              padding: '10px 26px',
              fontSize: '0.95rem',
              color: 'var(--accent)',
              borderColor: 'var(--accent)',
              opacity: canSubmit ? 1 : 0.45,
              cursor: canSubmit ? 'pointer' : 'default',
            }}
          >
            {submitting ? 'Sending…' : 'Send'}
          </button>
        </div>
      </div>

      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.78rem',
        fontWeight: 300,
        color: 'var(--text-muted)',
        marginTop: 'auto',
        paddingTop: '40px',
      }}>
        Experimental — you can stop any time.
      </p>
    </div>
  );
}
