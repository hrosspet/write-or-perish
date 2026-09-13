import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import ModelSelector from '../components/ModelSelector';
import { useUser } from '../contexts/UserContext';
import { useToast } from '../contexts/ToastContext';
import api from '../api';

/**
 * ReadPage - the Community Archive read, started from nothing but the
 * profile and intentions (the 'read' prompt). One sentence on what will
 * happen, the model it runs on, one button. The reply arrives by batch,
 * so the page hands over to the pending reply node, which the thread
 * page shows as "Processing…" until the picks land. Admin-only while the
 * placeholder behind it is.
 */
export default function ReadPage() {
  const navigate = useNavigate();
  const { user } = useUser();
  const { addToast } = useToast();
  const [model, setModel] = useState('');
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (user && !user.is_admin) navigate('/', { replace: true });
  }, [user, navigate]);

  const start = async () => {
    setStarting(true);
    try {
      const res = await api.post('/read/start', { model: model || undefined });
      navigate(`/node/${res.data.llm_node_id}`);
    } catch (err) {
      setStarting(false);
      if (err?.response?.status === 402) return;
      addToast(err?.response?.data?.error || 'Could not start the read.', 6000);
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
        margin: '0 0 20px 0',
        textAlign: 'center',
      }}>
        Is there anything worth reading today?
      </h1>

      <p style={{
        fontFamily: 'var(--sans)',
        fontWeight: 300,
        fontSize: '0.98rem',
        lineHeight: 1.55,
        color: 'var(--text-secondary)',
        maxWidth: '34em',
        textAlign: 'center',
        margin: '0 0 36px 0',
      }}>
        The last day of the Community Archive is read against your profile
        and your intentions. Most days the answer is nothing. When something
        is worth your time, it comes back in full, with the reason.
        The reply takes a while, so the thread shows it as processing
        until it lands.
      </p>

      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        flexWrap: 'wrap',
        justifyContent: 'center',
      }}>
        <button
          type="button"
          onClick={start}
          disabled={starting}
          style={{
            background: 'var(--accent)',
            color: 'var(--bg-primary)',
            border: 'none',
            borderRadius: '6px',
            padding: '0 18px',
            height: '36px',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 400,
            cursor: starting ? 'default' : 'pointer',
            opacity: starting ? 0.7 : 1,
          }}
        >
          {starting ? 'Starting…' : 'Read today’s archive'}
        </button>
        <ModelSelector
          nodeId={null}
          selectedModel={model}
          onModelChange={setModel}
        />
      </div>
    </div>
  );
}
