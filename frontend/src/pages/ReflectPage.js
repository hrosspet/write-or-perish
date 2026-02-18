import React, { useState, useCallback, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useStreamingTranscription } from '../hooks/useStreamingTranscription';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';
import { useStreamingTTS } from '../hooks/useStreamingTTS';
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
            animation: `waveBar 1.2s ease-in-out ${i * 0.05}s infinite alternate`,
          }}
        />
      ))}
      <style>{`
        @keyframes waveBar {
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
      animation: 'pulseDot 1.5s ease-in-out infinite',
    }}>
      <style>{`
        @keyframes pulseDot {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </span>
  );
}

export default function ReflectPage() {
  const navigate = useNavigate();
  const [phase, setPhase] = useState('ready'); // ready, recording, processing, response
  const [transcript, setTranscript] = useState('');
  const [llmNodeId, setLlmNodeId] = useState(null);
  const [llmResponse, setLlmResponse] = useState('');
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
          const res = await api.post('/reflect', { content: finalTranscript });
          setLlmNodeId(res.data.llm_node_id);
        } catch (err) {
          console.error('Reflect API error:', err);
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

  // When LLM completes, show response
  useEffect(() => {
    if (llmStatus === 'completed' && llmData?.content) {
      setLlmResponse(llmData.content);
      setPhase('response');
    } else if (llmStatus === 'failed') {
      setLlmResponse('AI response failed. Please try again.');
      setPhase('response');
    }
  }, [llmStatus, llmData]);

  // Streaming TTS — auto-play when response arrives
  const tts = useStreamingTTS(
    phase === 'response' && llmNodeId ? llmNodeId : null,
    { autoPlay: true }
  );

  // Start TTS when response arrives
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
    streaming.startStreaming();
  }, [streaming]);

  const handleStop = useCallback(() => {
    streaming.stopStreaming();
  }, [streaming]);

  const handleContinue = useCallback(() => {
    setPhase('ready');
    setTranscript('');
    setLlmResponse('');
    setLlmNodeId(null);
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
          Speak what's present...
        </p>

        {/* ECG Logo */}
        <div style={{ marginBottom: '32px', opacity: phase === 'recording' ? 1 : 0.5, transition: 'opacity 0.3s' }}>
          <img
            src="/loore-logo-transparent.svg"
            alt=""
            style={{
              height: '48px',
              width: 'auto',
              filter: phase === 'recording' ? 'drop-shadow(0 0 12px var(--accent-glow))' : 'none',
              animation: phase === 'recording' ? 'ecgPulse 2s ease-in-out infinite' : 'none',
            }}
          />
          <style>{`
            @keyframes ecgPulse {
              0%, 100% { opacity: 0.7; transform: scale(1); }
              50% { opacity: 1; transform: scale(1.05); }
            }
          `}</style>
        </div>

        {/* Waveform */}
        {phase === 'recording' && <WaveformBars />}

        {/* Timer */}
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

        {/* Start/Stop button */}
        {phase === 'ready' && (
          <button
            onClick={handleStart}
            style={{
              width: '72px', height: '72px', borderRadius: '50%',
              border: '2px solid var(--accent)',
              background: 'transparent',
              cursor: 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              transition: 'all 0.2s ease',
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
              transition: 'all 0.2s ease',
            }}
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="var(--accent)">
              <rect x="3" y="3" width="14" height="14" rx="2" />
            </svg>
          </button>
        )}

        {/* Transcript preview */}
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
        padding: '40px 24px',
      }}>
        <PulsingDot />
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginTop: '16px',
        }}>
          Reflecting...
        </p>
      </div>
    );
  }

  // --- RESPONSE STATE ---
  return (
    <div style={{
      maxWidth: '700px',
      margin: '0 auto',
      padding: '60px 24px',
    }}>
      {/* Date header */}
      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.7rem',
        fontWeight: 400,
        color: 'var(--text-muted)',
        textTransform: 'uppercase',
        letterSpacing: '0.08em',
        marginBottom: '24px',
      }}>
        {today}
      </p>

      {/* User's transcript */}
      <div style={{
        fontFamily: 'var(--serif)',
        fontSize: '1.05rem',
        fontWeight: 300,
        color: 'var(--text-secondary)',
        lineHeight: 1.7,
        marginBottom: '24px',
      }}>
        {transcript}
      </div>

      {/* Divider */}
      <div style={{ height: '1px', background: 'var(--border)', marginBottom: '24px' }} />

      {/* AI response label */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        marginBottom: '16px',
      }}>
        <PulsingDot />
        <span style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.75rem',
          fontWeight: 400,
          color: 'var(--text-muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.06em',
        }}>
          Loore reflects
        </span>
      </div>

      {/* AI response */}
      <div style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.9rem',
        fontWeight: 300,
        color: 'var(--text-muted)',
        lineHeight: 1.7,
        marginBottom: '40px',
      }}>
        {llmResponse.split('\n\n').map((p, i) => (
          <p key={i} style={{
            marginBottom: '12px',
            opacity: 1,
            animation: `fadeUp 0.4s ease ${i * 0.15}s both`,
          }}>
            {p}
          </p>
        ))}
        <style>{`
          @keyframes fadeUp {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
          }
        `}</style>
      </div>

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
        <button
          onClick={handleContinue}
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
          Continue reflecting
        </button>
        <button
          onClick={() => navigate('/log')}
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
          View in log
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
