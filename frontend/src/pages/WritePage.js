import React from 'react';
import { useNavigate } from 'react-router-dom';
import NodeForm from '../components/NodeForm';
import { useUser } from '../contexts/UserContext';
import { useToast } from '../contexts/ToastContext';
import { isAiAllowed } from '../utils/aiUsage';
import api from '../api';

export default function WritePage() {
  const navigate = useNavigate();
  const { user } = useUser();
  const { addToast } = useToast();
  const craftMode = !!user?.craft_mode;

  // Text mode is agentic: every entry that starts here gets the textmode
  // system prompt and honours the auto-generate preference — typed
  // (`/textmode/start`) and recorded alike. A recorded entry arrives with
  // `streaming_session_id`: its transcript and audio live in a streaming
  // draft, so it is saved through save-as-node with the same two
  // decisions (`agentic`, `auto_generate`) instead of the bare node it
  // used to become.
  const handleSubmit = async ({
    content, privacy_level, ai_usage, streaming_session_id,
  }) => {
    // Auto-generate requires AI-allowed ai_usage. Craft-mode users who
    // switch ai_usage to 'none' (or anything outside AI_ALLOWED) get a
    // graceful fallback to a plain entry instead of the agentic flow —
    // no LLM fires, the note is saved, user lands on the new node.
    if (!isAiAllowed(ai_usage)) {
      addToast(
        'Turning off auto-generate. AI usage on some nodes is turned off.',
        8000,
      );
      const res = streaming_session_id
        ? await api.post(`/drafts/streaming/${streaming_session_id}/save-as-node`, {
          content,
        })
        : await api.post('/nodes/', { content, privacy_level, ai_usage });
      return { id: res.data.id };
    }
    // Respect the user's auto-generate preference from the very first
    // turn (#134). Shares the `loore_auto_generate` key with NodeDetail's
    // inline toggle and NodeForm's craft-mode toggle, so there's one
    // "auto-generate after submit" preference across the app. Default
    // true on a fresh install (matches the backend default). When off,
    // the thread is created without firing an LLM reply.
    const stored = localStorage.getItem('loore_auto_generate');
    const autoGenerate = stored === null ? true : stored === 'true';
    if (streaming_session_id) {
      const res = await api.post(
        `/drafts/streaming/${streaming_session_id}/save-as-node`,
        { content, agentic: true, auto_generate: autoGenerate },
      );
      return res.data;
    }
    const res = await api.post('/textmode/start', {
      content, privacy_level, ai_usage, auto_generate: autoGenerate,
    });
    return res.data;
  };

  const handleSuccess = (data) => {
    const llmNodeId = data?.llm_node_id;
    if (llmNodeId && data?.user_node_id) {
      // Land on the user's entry so it gets its own URL/history step;
      // NodeDetail picks up the pending LLM response via ?awaitLlm and
      // navigates to it on completion.
      navigate(`/node/${data.user_node_id}?awaitLlm=${llmNodeId}`);
    } else if (llmNodeId) {
      navigate(`/node/${llmNodeId}?awaitLlm=${llmNodeId}`);
    } else if (data?.user_node_id) {
      // Auto-generate off (#134): no LLM reply was created — land on the
      // user's own entry. /textmode/start returns user_node_id (not id).
      navigate(`/node/${data.user_node_id}`);
    } else if (data?.id) {
      // Fallback path (ai_usage not chat/train) — navigate to the plain
      // entry without the awaitLlm query param.
      navigate(`/node/${data.id}`);
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

      <div style={{ width: '1170px', maxWidth: '90vw' }}>
        <NodeForm
          parentId={null}
          hidePowerFeatures={!craftMode}
          hideAudioUpload={!craftMode}
          aiUsageFromGlobalDefault
          placeholder="Type what's on your mind…"
          onSubmitOverride={handleSubmit}
          onSuccess={handleSuccess}
        />
      </div>
    </div>
  );
}
