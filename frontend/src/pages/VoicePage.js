import React, { useState, useCallback, useRef, useEffect } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { FaPlay, FaPause, FaUndo, FaRedo, FaKeyboard } from 'react-icons/fa';
import { useVoiceSession } from '../hooks/useVoiceSession';
import { useUser } from '../contexts/UserContext';
import { useInterruptedRecovery } from '../hooks/useInterruptedRecovery';
import RecoveryBanner from '../components/RecoveryBanner';
import OfflineBanner from '../components/OfflineBanner';
import ProposalInline from '../components/ProposalInline';
import { useToast } from '../contexts/ToastContext';
import api from '../api';

function formatDuration(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

function WaveformBars({ animated = true }) {
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
            animation: animated ? `waveBarVoice 1.2s ease-in-out ${i * 0.05}s infinite alternate` : 'none',
            height: animated ? undefined : '4px',
          }}
        />
      ))}
      <style>{`
        @keyframes waveBarVoice {
          0% { height: 4px; }
          100% { height: ${12 + Math.random() * 20}px; }
        }
      `}</style>
    </div>
  );
}

function PulsingDot({ color = 'var(--accent)' }) {
  return (
    <span style={{
      display: 'inline-block',
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      background: color,
      animation: 'pulseDotVoice 1.5s ease-in-out infinite',
    }}>
      <style>{`
        @keyframes pulseDotVoice {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </span>
  );
}

const ECG_PATH = "M 24,81.6 L 64,81.6 L 84,74.4 L 100,86.4 L 132,16.8 L 168,127.2 L 192,50.4 L 208,81.6 L 224,81.6 L 256,81.6";
const ECG_PULSE_PATH = "M 100,86.4 L 132,16.8 L 168,127.2 L 192,50.4";

function EcgAnimation({ active = true, showScanline = true, dim = false }) {
  return (
    <div style={{
      position: 'relative',
      width: '280px',
      height: '168px',
      marginBottom: '2rem',
      opacity: dim ? 0.4 : 1,
      transition: 'opacity 0.4s ease',
    }}>
      {showScanline && active && (
        <div style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: '3px',
          height: '100%',
          background: 'linear-gradient(to bottom, transparent, var(--accent), transparent)',
          borderRadius: '2px',
          filter: 'blur(1px)',
          animation: 'ecgScanVoice 3s ease-in-out 2.1s infinite',
          opacity: 0,
        }} />
      )}
      <svg width="100%" height="100%" viewBox="0 0 280 168" fill="none">
        <path
          d={ECG_PATH}
          stroke="#c4956a"
          strokeWidth="8"
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity="0.15"
          filter="url(#ecgBlurVoice)"
          style={active ? {
            strokeDasharray: 500,
            strokeDashoffset: 500,
            animation: 'ecgDrawLineVoice 1.5s cubic-bezier(0.22, 1, 0.36, 1) 0.6s forwards',
          } : {
            strokeDasharray: 'none',
            opacity: 0.1,
          }}
        />
        <path
          d={ECG_PATH}
          stroke="#c4956a"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={active ? {
            strokeDasharray: 500,
            strokeDashoffset: 500,
            animation: 'ecgDrawLineVoice 1.5s cubic-bezier(0.22, 1, 0.36, 1) 0.6s forwards',
          } : {
            strokeDasharray: 'none',
          }}
        />
        <path
          d={ECG_PULSE_PATH}
          stroke="#c4956a"
          strokeWidth="5"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={active ? {
            opacity: 0,
            strokeDasharray: 500,
            strokeDashoffset: 500,
            animation: 'ecgDrawLineVoice 1.5s cubic-bezier(0.22, 1, 0.36, 1) 0.6s forwards, ecgBreatheVoice 3s ease-in-out 2.1s infinite',
          } : {
            opacity: 0.2,
            strokeDasharray: 'none',
          }}
        />
        <defs>
          <filter id="ecgBlurVoice">
            <feGaussianBlur stdDeviation="4" />
          </filter>
        </defs>
      </svg>
      <style>{`
        @keyframes ecgDrawLineVoice {
          to { stroke-dashoffset: 0; }
        }
        @keyframes ecgBreatheVoice {
          0%, 100% { opacity: 0.25; filter: drop-shadow(0 0 8px var(--accent-glow)); }
          50% { opacity: 0.6; filter: drop-shadow(0 0 20px var(--accent)); }
        }
        @keyframes ecgScanVoice {
          0% { left: 0%; opacity: 0; }
          5% { opacity: 0.5; }
          50% { opacity: 0.3; }
          95% { opacity: 0.5; }
          100% { left: 100%; opacity: 0; }
        }
      `}</style>
    </div>
  );
}

function Spinner() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" style={{ animation: 'spinVoice 1s linear infinite' }}>
      <circle cx="10" cy="10" r="8" fill="none" stroke="var(--accent)" strokeWidth="2" strokeDasharray="40 20" strokeLinecap="round" />
      <style>{`
        @keyframes spinVoice {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </svg>
  );
}

export default function VoicePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const resumeId = searchParams.get('resume');
  const parentId = searchParams.get('parent');

  const {
    interruptedDraft, checked: recoveryChecked,
    handleDiscard, clearInterrupted,
  } = useInterruptedRecovery();

  const [toolCallsMeta, setToolCallsMeta] = useState(null);
  const [llmContent, setLlmContent] = useState(null);
  const { addToast } = useToast();
  const setThreadParentIdRef = useRef(null);
  const lastLlmNodeIdRef = useRef(null);

  const { user } = useUser();
  const selectedModel = user?.preferred_model || null;

  const {
    phase, isStopping, hasError, isOnline, streaming, audio, handleStart, handleStop,
    handleContinue, handleResumeSession, handleCancelProcessing, setThreadParentId,
  } = useVoiceSession({
    apiEndpoint: '/voice',
    ttsTitle: 'Voice',
    initialLlmNodeId: resumeId ? Number(resumeId) : null,
    initialParentId: parentId ? Number(parentId) : null,
    model: selectedModel,
    aiUsage: user?.default_ai_usage || 'none',
    onLLMComplete: (nodeId, content, isResume) => {
      lastLlmNodeIdRef.current = nodeId;
      setLlmContent(content);
      // ProposalInline handles its own parsing + apply-status derivation
      // from tool_calls_meta. We just feed it the raw content + meta.
      api.get(`/nodes/${nodeId}/llm-status`).then(res => {
        if (res.data.tool_calls_meta) {
          setToolCallsMeta(res.data.tool_calls_meta);
        }
      }).catch(() => { /* non-fatal */ });
    },
  });

  setThreadParentIdRef.current = setThreadParentId;

  const voiceReset = useCallback(() => {
    setLlmContent(null);
    setToolCallsMeta(null);
  }, []);

  const displayTime = audio.cumulativeTime || 0;
  const displayDuration = audio.totalDuration || 0;

  const formatTime = (seconds) => {
    if (isNaN(seconds) || !isFinite(seconds)) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const handleSeek = (e) => {
    if (!displayDuration || displayDuration <= 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const percentage = Math.max(0, Math.min(1, x / rect.width));
    const newTime = percentage * displayDuration;
    if (isFinite(newTime)) {
      audio.seekToCumulativeTime(newTime);
    }
  };

  const containerStyle = {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 'calc(100vh - 120px)',
    padding: '40px 24px',
    background: 'radial-gradient(ellipse at 50% 40%, rgba(196,149,106,0.06) 0%, transparent 70%)',
  };

  // Rendered in every phase so the user can escape to Text Mode at any
  // time. Position: fixed so it anchors to the viewport regardless of
  // which return branch is active.
  const textModeButton = (
    <button
      key="text-mode-btn"
      onClick={() => {
        if (lastLlmNodeIdRef.current) {
          navigate(`/node/${lastLlmNodeIdRef.current}`);
        } else {
          navigate('/textmode');
        }
      }}
      title="Continue in Text Mode"
      style={{
        position: 'fixed',
        top: '72px',
        right: '20px',
        background: 'none',
        border: '1px solid var(--border)',
        borderRadius: '6px',
        padding: '6px 12px',
        color: 'var(--text-muted)',
        fontFamily: 'var(--sans)',
        fontSize: '0.78rem',
        fontWeight: 300,
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        zIndex: 50,
      }}
    >
      <FaKeyboard size={11} />
      <span>Text Mode</span>
    </button>
  );

  const controlButtonStyle = (active = true) => ({
    background: 'none',
    border: 'none',
    color: active ? 'var(--accent)' : 'var(--text-muted)',
    cursor: active ? 'pointer' : 'default',
    fontSize: '18px',
    padding: '8px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'opacity 0.2s',
  });

  // Pause audio while recovery banner is visible
  const showRecovery = interruptedDraft && phase !== 'recording';
  const playAfterDismissRef = useRef(false);
  useEffect(() => {
    if (showRecovery && audio.isPlaying) {
      audio.pause();
    }
    if (!showRecovery && playAfterDismissRef.current) {
      playAfterDismissRef.current = false;
      audio.play();
    }
  }, [showRecovery, audio]);

  if (!recoveryChecked) {
    return <div style={containerStyle}>{textModeButton}</div>;
  }

  if (showRecovery) {
    return (
      <div style={containerStyle}>
        {textModeButton}
        <RecoveryBanner
          draft={interruptedDraft}
          onContinue={() => {
            const { session_id, id, chunk_count, parent_id } = interruptedDraft;
            clearInterrupted();
            handleResumeSession({ sessionId: session_id, draftId: id, chunkCount: chunk_count, parentId: parent_id });
          }}
          onDiscard={() => {
            if (phase === 'playback') {
              playAfterDismissRef.current = true;
            }
            handleDiscard();
          }}
        >
          <EcgAnimation active={false} dim={true} showScanline={false} />
        </RecoveryBanner>
      </div>
    );
  }

  // --- READY / RECORDING STATE ---
  if (phase === 'ready' || phase === 'recording') {
    return (
      <div style={containerStyle}>
        {textModeButton}
        <p style={{
          fontFamily: 'var(--serif)',
          fontStyle: 'italic',
          fontSize: 'clamp(1.2rem, 2.5vw, 1.6rem)',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginBottom: '40px',
        }}>
          What's on your mind?
        </p>

        <EcgAnimation
          key={phase}
          active={phase === 'recording'}
          dim={phase === 'ready'}
          showScanline={phase === 'recording'}
        />

        {phase === 'recording' && <WaveformBars animated={!isStopping} />}

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

        {phase === 'ready' && <OfflineBanner />}

        {hasError && phase === 'ready' && (
          <div style={{ marginBottom: '16px' }}>
            <PulsingDot color="var(--error, #e74c3c)" />
          </div>
        )}

        {phase === 'ready' && (
          <button
            onClick={handleStart}
            disabled={!isOnline}
            style={{
              width: '72px', height: '72px', borderRadius: '50%',
              border: `2px solid ${isOnline ? 'var(--accent)' : 'var(--text-muted)'}`,
              background: 'transparent',
              cursor: isOnline ? 'pointer' : 'not-allowed',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              transition: 'all 0.2s ease',
              opacity: isOnline ? 1 : 0.4,
            }}
          >
            <svg width="24" height="24" viewBox="0 0 24 24" fill={isOnline ? 'var(--accent)' : 'var(--text-muted)'}>
              <circle cx="12" cy="12" r="8" />
            </svg>
          </button>
        )}

        {phase === 'recording' && (
          <button
            onClick={() => { if (!isStopping) handleStop(); }}
            style={{
              width: '72px', height: '72px', borderRadius: '50%',
              border: '2px solid var(--accent)',
              background: 'transparent',
              cursor: 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              transition: 'all 0.2s ease',
              opacity: isStopping ? 0.5 : 1,
            }}
          >
            {isStopping ? (
              <Spinner />
            ) : (
              <svg width="20" height="20" viewBox="0 0 20 20" fill="var(--accent)">
                <rect x="3" y="3" width="14" height="14" rx="2" />
              </svg>
            )}
          </button>
        )}
      </div>
    );
  }

  // --- PROCESSING STATE ---
  if (phase === 'processing') {
    return (
      <div style={containerStyle}>
        {textModeButton}
        <EcgAnimation active={true} showScanline={false} />
        <PulsingDot />
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginTop: '16px',
        }}>
          Thinking...
        </p>
        <button
          onClick={() => handleCancelProcessing(voiceReset)}
          style={{
            background: 'none',
            border: 'none',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            fontSize: '0.8rem',
            fontFamily: 'var(--sans)',
            marginTop: '32px',
            opacity: 0.5,
            transition: 'opacity 0.2s',
            padding: '8px 16px',
          }}
          onMouseEnter={(e) => e.target.style.opacity = '0.8'}
          onMouseLeave={(e) => e.target.style.opacity = '0.5'}
        >
          ✕
        </button>
      </div>
    );
  }

  // --- PLAYBACK STATE ---

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      minHeight: 'calc(100vh - 120px)',
      padding: '40px 24px',
      background: 'radial-gradient(ellipse at 50% 40%, rgba(196,149,106,0.06) 0%, transparent 70%)',
      position: 'relative',
    }}>
      {textModeButton}
      <EcgAnimation
        active={audio.isPlaying}
        dim={!audio.isPlaying}
        showScanline={false}
      />

      <div style={{ marginBottom: '24px' }}>
        <WaveformBars animated={audio.isPlaying} />
      </div>

      {/* Audio controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '16px' }}>
        <button onClick={() => audio.skipBackward()} title="Skip back 10s" style={controlButtonStyle()}>
          <FaUndo />
        </button>

        {audio.isPlaying ? (
          <button
            onClick={() => audio.pause()}
            style={{
              ...controlButtonStyle(),
              width: '48px', height: '48px',
              borderRadius: '50%',
              border: '2px solid var(--accent)',
              fontSize: '20px',
            }}
          >
            <FaPause />
          </button>
        ) : (
          <button
            onClick={() => {
              if (audio.totalDuration > 0 && audio.cumulativeTime >= audio.totalDuration - 0.5) {
                audio.seekToCumulativeTime(0);
                setTimeout(() => audio.play(), 50);
              } else {
                audio.play();
              }
            }}
            style={{
              ...controlButtonStyle(),
              width: '48px', height: '48px',
              borderRadius: '50%',
              border: '2px solid var(--accent)',
              fontSize: '20px',
            }}
          >
            <FaPlay style={{ marginLeft: '2px' }} />
          </button>
        )}

        <button onClick={() => audio.skipForward()} title="Skip forward 10s" style={controlButtonStyle()}>
          <FaRedo />
        </button>
      </div>

      {/* Progress bar */}
      <div style={{ width: '100%', maxWidth: '300px', marginBottom: '8px' }}>
        <div
          onClick={handleSeek}
          style={{
            width: '100%', height: '6px',
            backgroundColor: 'var(--bg-card, rgba(255,255,255,0.05))',
            borderRadius: '3px', cursor: 'pointer',
            position: 'relative', overflow: 'hidden',
          }}
        >
          <div style={{
            height: '100%',
            width: `${displayDuration > 0 ? (displayTime / displayDuration) * 100 : 0}%`,
            backgroundColor: 'var(--accent)',
            borderRadius: '3px',
            transition: 'width 0.1s linear',
          }} />
        </div>
        <div style={{
          display: 'flex', justifyContent: 'space-between',
          marginTop: '4px', color: 'var(--text-muted)',
          fontSize: '0.75rem', fontFamily: 'var(--sans)', fontWeight: 300,
        }}>
          <span>{formatTime(displayTime)}</span>
          <span>
            {formatTime(displayDuration)}
            {audio.generatingTTS && (
              <span style={{
                fontSize: '9px', color: 'var(--accent)',
                marginLeft: '4px', animation: 'pulseDotVoice 1.5s ease-in-out infinite',
              }}>●</span>
            )}
          </span>
        </div>
      </div>

      <ProposalInline
        size="roomy"
        content={llmContent}
        nodeId={lastLlmNodeIdRef.current}
        toolCallsMeta={toolCallsMeta}
        onContentChange={setLlmContent}
        onError={(msg) => addToast(msg)}
      />

      <div style={{ height: '32px' }} />

      <OfflineBanner style={{ marginBottom: '8px' }} />

      {/* Record button to continue */}
      <button
        onClick={() => handleContinue(voiceReset)}
        disabled={!isOnline}
        title={isOnline ? 'Continue' : "You're offline"}
        style={{
          width: '56px', height: '56px', borderRadius: '50%',
          border: `2px solid ${isOnline ? 'var(--accent)' : 'var(--text-muted)'}`,
          background: 'transparent',
          cursor: isOnline ? 'pointer' : 'not-allowed',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          transition: 'all 0.2s ease',
          opacity: isOnline ? 0.7 : 0.3,
        }}
        onMouseEnter={(e) => { if (isOnline) e.currentTarget.style.opacity = '1'; }}
        onMouseLeave={(e) => { if (isOnline) e.currentTarget.style.opacity = '0.7'; }}
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill={isOnline ? 'var(--accent)' : 'var(--text-muted)'}>
          <circle cx="12" cy="12" r="8" />
        </svg>
      </button>
    </div>
  );
}
