import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import StreamingMicButton from '../components/StreamingMicButton';
import PrivacySelector from '../components/PrivacySelector';
import { useUser } from '../contexts/UserContext';
import api from '../api';

export default function WritePage() {
  const navigate = useNavigate();
  const { user } = useUser();
  const craftMode = !!user?.craft_mode;
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [privacyLevel, setPrivacyLevel] = useState(user?.default_privacy_level || 'private');
  const [aiUsage, setAiUsage] = useState(user?.default_ai_usage || 'chat');
  const textareaRef = useRef(null);
  const preStreamingRef = useRef('');

  useEffect(() => {
    if (textareaRef.current) textareaRef.current.focus();
  }, []);

  const handleMicStart = useCallback(() => {
    preStreamingRef.current = content;
  }, [content]);

  const handleMicTranscript = useCallback((transcript) => {
    const prefix = preStreamingRef.current;
    const sep = prefix && transcript ? '\n\n' : '';
    setContent(prefix + sep + transcript);
  }, []);

  const handleMicComplete = useCallback((data) => {
    const prefix = preStreamingRef.current;
    const sep = prefix && data?.content ? '\n\n' : '';
    setContent(prefix + sep + (data?.content || ''));
  }, []);

  const handleMicError = useCallback((err) => {
    setError(err?.message || 'Mic error');
  }, []);

  const handleSubmit = async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    const text = content.trim();
    if (!text || submitting) return;
    setSubmitting(true);
    setError('');
    try {
      const res = await api.post('/textmode/start', {
        content: text,
        ai_usage: aiUsage,
        privacy_level: privacyLevel,
      });
      const { llm_node_id } = res.data;
      // Navigate directly to the pending LLM node so NodeDetail's inline
      // input anchors below it and stays there through generation.
      navigate(`/node/${llm_node_id}?awaitLlm=${llm_node_id}`);
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.error || err.message || 'Error sending message.');
      setSubmitting(false);
    }
  };

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
        margin: '0 0 32px 0',
        textAlign: 'center',
      }}>
        What's on your mind?
      </h1>

      <form onSubmit={handleSubmit} style={{
        width: '100%',
        maxWidth: '640px',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}>
        <textarea
          ref={textareaRef}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault();
              handleSubmit();
            }
          }}
          placeholder="Type what's on your mind…"
          rows={6}
          disabled={submitting}
          style={{
            width: '100%',
            boxSizing: 'border-box',
            padding: '16px 18px',
            background: 'var(--bg-card)',
            color: 'var(--text-primary)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
            fontFamily: 'var(--sans)',
            fontSize: '1rem',
            fontWeight: 300,
            lineHeight: 1.6,
            resize: 'vertical',
            outline: 'none',
            minHeight: '160px',
          }}
        />

        {craftMode && (
          <PrivacySelector
            privacyLevel={privacyLevel}
            aiUsage={aiUsage}
            onPrivacyChange={setPrivacyLevel}
            onAIUsageChange={setAiUsage}
            disabled={submitting}
          />
        )}

        {error && (
          <div style={{
            padding: '8px 12px',
            color: 'var(--accent)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
          }}>{error}</div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          <button
            type="submit"
            disabled={!content.trim() || submitting}
            style={{
              padding: '10px 22px',
              background: 'var(--accent)',
              color: 'var(--bg-deep)',
              border: 'none',
              borderRadius: '6px',
              fontFamily: 'var(--sans)',
              fontSize: '0.9rem',
              fontWeight: 400,
              cursor: (!content.trim() || submitting) ? 'not-allowed' : 'pointer',
              opacity: (!content.trim() || submitting) ? 0.5 : 1,
            }}
          >
            {submitting ? 'Sending…' : 'Send'}
          </button>
          <StreamingMicButton
            parentId={null}
            privacyLevel={privacyLevel}
            aiUsage={aiUsage}
            disabled={submitting}
            onRecordingStart={handleMicStart}
            onTranscriptUpdate={handleMicTranscript}
            onComplete={handleMicComplete}
            onError={handleMicError}
          />
        </div>
      </form>
    </div>
  );
}
