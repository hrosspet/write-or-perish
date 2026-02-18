import React, { useState, useCallback, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useStreamingTranscription } from '../hooks/useStreamingTranscription';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';
import { useStreamingTTS } from '../hooks/useStreamingTTS';
import ReactMarkdown from 'react-markdown';
import api from '../api';

function formatDuration(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

function WaveformBars() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '3px', height: '32px', justifyContent: 'center' }}>
      {Array.from({ length: 24 }).map((_, i) => (
        <div
          key={i}
          style={{
            width: '2px',
            background: 'var(--accent)',
            borderRadius: '1px',
            opacity: 0.6,
            animation: `waveBarOrient 1.2s ease-in-out ${i * 0.05}s infinite alternate`,
          }}
        />
      ))}
      <style>{`
        @keyframes waveBarOrient {
          0% { height: 4px; }
          100% { height: ${12 + Math.random() * 20}px; }
        }
      `}</style>
    </div>
  );
}

function PulsingDot() {
  return (
    <span style={{
      display: 'inline-block',
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      background: 'var(--accent)',
      animation: 'pulseDotOrient 1.5s ease-in-out infinite',
    }}>
      <style>{`
        @keyframes pulseDotOrient {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </span>
  );
}

/**
 * Parse the AI response into structured sections.
 * Looks for ### Completed, ### New Tasks, ### Priority Order, ### Note
 */
function parseOrientResponse(text) {
  const sections = {};
  const parts = text.split(/^###\s+/m);

  for (const part of parts) {
    if (!part.trim()) continue;
    const firstNewline = part.indexOf('\n');
    if (firstNewline < 0) continue;
    const heading = part.slice(0, firstNewline).trim().toLowerCase();
    const body = part.slice(firstNewline + 1).trim();

    if (heading.includes('completed')) sections.completed = body;
    else if (heading.includes('new task')) sections.newTasks = body;
    else if (heading.includes('priority')) sections.priority = body;
    else if (heading.includes('note')) sections.note = body;
  }

  return sections;
}

export default function OrientPage() {
  const navigate = useNavigate();
  const [phase, setPhase] = useState('ready'); // ready, recording, processing, response
  const [transcript, setTranscript] = useState('');
  const [llmNodeId, setLlmNodeId] = useState(null);
  const [llmResponse, setLlmResponse] = useState('');
  const [applied, setApplied] = useState(false);
  const transcriptRef = useRef('');

  // Streaming transcription
  const streaming = useStreamingTranscription({
    privacyLevel: 'private',
    aiUsage: 'chat',
    onTranscriptUpdate: (text) => {
      setTranscript(text);
      transcriptRef.current = text;
    },
    onComplete: async (data) => {
      setPhase('processing');
      const finalTranscript = data.content || transcriptRef.current;
      setTranscript(finalTranscript);
      if (finalTranscript.trim()) {
        try {
          const res = await api.post('/orient', { content: finalTranscript });
          setLlmNodeId(res.data.llm_node_id);
        } catch (err) {
          console.error('Orient API error:', err);
          setPhase('response');
          setLlmResponse('Something went wrong. Please try again.');
        }
      }
    },
  });

  // Poll LLM completion
  const { data: llmData, status: llmStatus } = useAsyncTaskPolling(
    llmNodeId ? `/nodes/${llmNodeId}/llm-status` : null,
    { enabled: !!llmNodeId && phase === 'processing', interval: 1500 }
  );

  useEffect(() => {
    if (llmStatus === 'completed' && llmData?.content) {
      setLlmResponse(llmData.content);
      setPhase('response');
    } else if (llmStatus === 'failed') {
      setLlmResponse('AI response failed. Please try again.');
      setPhase('response');
    }
  }, [llmStatus, llmData]);

  // Streaming TTS
  const tts = useStreamingTTS(
    phase === 'response' && llmNodeId ? llmNodeId : null,
    { autoPlay: true }
  );

  const ttsStartedRef = useRef(false);
  useEffect(() => {
    if (phase === 'response' && llmNodeId && llmResponse && !ttsStartedRef.current) {
      ttsStartedRef.current = true;
      tts.startTTS();
    }
  }, [phase, llmNodeId, llmResponse]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleStart = useCallback(() => {
    setPhase('recording');
    ttsStartedRef.current = false;
    setApplied(false);
    streaming.startStreaming();
  }, [streaming]);

  const handleStop = useCallback(() => {
    streaming.stopStreaming();
  }, [streaming]);

  const handleApplyTodo = async () => {
    if (!llmNodeId || applied) return;
    // Build an updated todo from the AI response
    // For now, send the structured suggestions as the new todo content
    const parsed = parseOrientResponse(llmResponse);
    let updatedContent = '';

    // Build updated markdown — this is a simplified version
    // In production, this would merge with the existing todo more intelligently
    if (parsed.priority) {
      updatedContent += `## Today\n\n`;
      const lines = parsed.priority.split('\n').filter(l => l.trim());
      for (const line of lines) {
        const cleaned = line.replace(/^\d+[.)]\s*/, '').trim();
        if (cleaned) updatedContent += `- [ ] ${cleaned}\n`;
      }
    }
    if (parsed.newTasks) {
      updatedContent += `\n## Upcoming\n\n`;
      const lines = parsed.newTasks.split('\n').filter(l => l.trim());
      for (const line of lines) {
        const cleaned = line.replace(/^[-*]\s*/, '').trim();
        if (cleaned) updatedContent += `- [ ] ${cleaned}\n`;
      }
    }
    if (parsed.completed) {
      updatedContent += `\n## Completed recently\n\n`;
      const lines = parsed.completed.split('\n').filter(l => l.trim());
      for (const line of lines) {
        const cleaned = line.replace(/^[-*]\s*/, '').trim();
        if (cleaned) updatedContent += `- [x] ${cleaned}\n`;
      }
    }

    if (!updatedContent.trim()) {
      // Fallback — just save the AI response as-is
      updatedContent = llmResponse;
    }

    try {
      await api.post(`/orient/${llmNodeId}/apply-todo`, {
        updated_content: updatedContent,
      });
      setApplied(true);
    } catch (err) {
      console.error('Failed to apply todo:', err);
    }
  };

  const handleNewSession = useCallback(() => {
    setPhase('ready');
    setTranscript('');
    setLlmResponse('');
    setLlmNodeId(null);
    setApplied(false);
    ttsStartedRef.current = false;
    streaming.cancelStreaming();
  }, [streaming]);

  const today = new Date().toLocaleDateString('en-US', {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  });

  // --- RECORDING STATE ---
  if (phase === 'ready' || phase === 'recording') {
    return (
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        minHeight: 'calc(100vh - 120px)',
        padding: '40px 24px',
        background: 'radial-gradient(ellipse at 50% 40%, rgba(196,149,106,0.06) 0%, transparent 70%)',
      }}>
        <p style={{
          fontFamily: 'var(--serif)',
          fontStyle: 'italic',
          fontSize: '1.1rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginBottom: '40px',
        }}>
          Ground your day. See what matters.
        </p>

        {/* Compass icon */}
        <div style={{ marginBottom: '32px', opacity: phase === 'recording' ? 1 : 0.5, transition: 'opacity 0.3s' }}>
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5"
            style={{
              filter: phase === 'recording' ? 'drop-shadow(0 0 12px var(--accent-glow))' : 'none',
              animation: phase === 'recording' ? 'compassPulse 2s ease-in-out infinite' : 'none',
            }}>
            <circle cx="12" cy="12" r="10" />
            <path d="M12 2v4M12 18v4M2 12h4M18 12h4" />
            <path d="M14.5 9.5L12 12l-2.5-2.5" fill="var(--accent)" stroke="none" />
          </svg>
          <style>{`
            @keyframes compassPulse {
              0%, 100% { opacity: 0.7; transform: scale(1); }
              50% { opacity: 1; transform: scale(1.05); }
            }
          `}</style>
        </div>

        {phase === 'recording' && <WaveformBars />}

        {phase === 'recording' && (
          <p style={{
            fontFamily: 'var(--sans)',
            fontSize: '1.2rem',
            fontWeight: 300,
            color: 'var(--text-secondary)',
            margin: '16px 0 32px 0',
            letterSpacing: '0.1em',
          }}>
            {formatDuration(streaming.duration || 0)}
          </p>
        )}

        {phase === 'ready' && (
          <button
            onClick={handleStart}
            style={{
              width: '72px', height: '72px', borderRadius: '50%',
              border: '2px solid var(--accent)',
              background: 'transparent',
              cursor: 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <svg width="24" height="24" viewBox="0 0 24 24" fill="var(--accent)">
              <circle cx="12" cy="12" r="8" />
            </svg>
          </button>
        )}

        {phase === 'recording' && (
          <button
            onClick={handleStop}
            style={{
              width: '72px', height: '72px', borderRadius: '50%',
              border: '2px solid var(--accent)',
              background: 'transparent',
              cursor: 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="var(--accent)">
              <rect x="3" y="3" width="14" height="14" rx="2" />
            </svg>
          </button>
        )}

        {phase === 'recording' && transcript && (
          <p style={{
            fontFamily: 'var(--serif)',
            fontSize: '0.85rem',
            fontWeight: 300,
            color: 'var(--text-muted)',
            maxWidth: '500px',
            textAlign: 'center',
            marginTop: '24px',
            opacity: 0.7,
          }}>
            {transcript.length > 200 ? '...' + transcript.slice(-200) : transcript}
          </p>
        )}
      </div>
    );
  }

  // --- PROCESSING STATE ---
  if (phase === 'processing') {
    return (
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        minHeight: 'calc(100vh - 120px)',
      }}>
        <PulsingDot />
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginTop: '16px',
        }}>
          Orienting your day...
        </p>
      </div>
    );
  }

  // --- RESPONSE STATE ---
  const parsed = parseOrientResponse(llmResponse);

  return (
    <div style={{ maxWidth: '700px', margin: '0 auto', padding: '60px 24px' }}>
      {/* Date header */}
      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.7rem',
        fontWeight: 400,
        color: 'var(--text-muted)',
        textTransform: 'uppercase',
        letterSpacing: '0.08em',
        marginBottom: '8px',
        textAlign: 'center',
      }}>
        {today}
      </p>

      <h2 style={{
        fontFamily: 'var(--serif)',
        fontSize: '1.5rem',
        fontWeight: 300,
        color: 'var(--text-primary)',
        textAlign: 'center',
        marginBottom: '24px',
      }}>
        Your day, oriented
      </h2>

      {/* User's sharing */}
      <div style={{
        fontFamily: 'var(--serif)',
        fontStyle: 'italic',
        fontSize: '0.95rem',
        fontWeight: 300,
        color: 'var(--text-secondary)',
        lineHeight: 1.7,
        marginBottom: '32px',
        padding: '16px',
        borderLeft: '2px solid var(--border)',
      }}>
        {transcript}
      </div>

      {/* AI Response sections */}
      {parsed.completed && (
        <div style={{ marginBottom: '24px' }}>
          <h3 style={{
            fontFamily: 'var(--sans)', fontSize: '0.7rem', fontWeight: 500,
            color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.08em',
            borderBottom: '1px solid var(--border)', paddingBottom: '6px', marginBottom: '12px',
          }}>
            Completed
          </h3>
          <div style={{ fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300, color: 'var(--text-muted)', lineHeight: 1.6, opacity: 0.7 }}>
            <ReactMarkdown>{parsed.completed}</ReactMarkdown>
          </div>
        </div>
      )}

      {parsed.newTasks && (
        <div style={{ marginBottom: '24px' }}>
          <h3 style={{
            fontFamily: 'var(--sans)', fontSize: '0.7rem', fontWeight: 500,
            color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.08em',
            borderBottom: '1px solid var(--border)', paddingBottom: '6px', marginBottom: '12px',
          }}>
            New Tasks
          </h3>
          <div style={{ fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            <ReactMarkdown>{parsed.newTasks}</ReactMarkdown>
          </div>
        </div>
      )}

      {parsed.priority && (
        <div style={{ marginBottom: '24px' }}>
          <h3 style={{
            fontFamily: 'var(--sans)', fontSize: '0.7rem', fontWeight: 500,
            color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.08em',
            borderBottom: '1px solid var(--border)', paddingBottom: '6px', marginBottom: '12px',
          }}>
            Suggested Priority
          </h3>
          <div style={{ fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            <ReactMarkdown>{parsed.priority}</ReactMarkdown>
          </div>
        </div>
      )}

      {parsed.note && (
        <div style={{
          marginBottom: '32px',
          padding: '16px 20px',
          background: 'var(--accent-subtle)',
          borderLeft: '3px solid var(--accent)',
          borderRadius: '0 8px 8px 0',
        }}>
          <div style={{ fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            <ReactMarkdown>{parsed.note}</ReactMarkdown>
          </div>
        </div>
      )}

      {/* If no structured sections found, show raw response */}
      {!parsed.completed && !parsed.newTasks && !parsed.priority && !parsed.note && llmResponse && (
        <div style={{
          fontFamily: 'var(--sans)', fontSize: '0.9rem', fontWeight: 300,
          color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: '32px',
        }}>
          <ReactMarkdown>{llmResponse}</ReactMarkdown>
        </div>
      )}

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
        {!applied ? (
          <button
            onClick={handleApplyTodo}
            style={{
              padding: '10px 24px',
              background: 'var(--accent)',
              border: 'none',
              borderRadius: '6px',
              color: 'var(--bg-deep)',
              fontFamily: 'var(--sans)',
              fontSize: '0.85rem',
              fontWeight: 400,
              cursor: 'pointer',
            }}
          >
            Apply changes to todo
          </button>
        ) : (
          <span style={{
            padding: '10px 24px',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            fontWeight: 300,
            color: '#4ade80',
          }}>
            Changes applied
          </span>
        )}
        <button
          onClick={() => navigate('/todo')}
          style={{
            padding: '10px 24px',
            background: 'none',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            cursor: 'pointer',
          }}
        >
          View todo list
        </button>
        <button
          onClick={handleNewSession}
          style={{
            padding: '10px 24px',
            background: 'none',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            cursor: 'pointer',
          }}
        >
          Orient again
        </button>
        <button
          onClick={() => navigate('/')}
          style={{
            padding: '10px 24px',
            background: 'none',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            cursor: 'pointer',
          }}
        >
          Back to home
        </button>
      </div>
    </div>
  );
}
