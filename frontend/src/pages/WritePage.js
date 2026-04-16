import React from 'react';
import { useNavigate } from 'react-router-dom';
import NodeForm from '../components/NodeForm';
import { useUser } from '../contexts/UserContext';
import api from '../api';

export default function WritePage() {
  const navigate = useNavigate();
  const { user } = useUser();
  const craftMode = !!user?.craft_mode;

  const handleSubmit = async ({ content, privacy_level, ai_usage }) => {
    const res = await api.post('/textmode/start', {
      content,
      privacy_level,
      ai_usage,
    });
    return res.data;
  };

  const handleSuccess = (data) => {
    const llmNodeId = data?.llm_node_id;
    if (llmNodeId) {
      // Land directly on the pending LLM node so NodeDetail anchors the
      // inline input below it and flips to the response in place.
      navigate(`/node/${llmNodeId}?awaitLlm=${llmNodeId}`);
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

      <div style={{ width: '100%', maxWidth: '640px' }}>
        <NodeForm
          parentId={null}
          initialAiUsage="chat"
          hidePowerFeatures={!craftMode}
          placeholder="Type what's on your mind…"
          submitLabel="Send"
          onSubmitOverride={handleSubmit}
          onSuccess={handleSuccess}
        />
      </div>
    </div>
  );
}
