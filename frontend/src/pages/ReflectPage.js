import React from 'react';
import { useSearchParams } from 'react-router-dom';
import { FaPlay, FaPause, FaUndo, FaRedo } from 'react-icons/fa';
import { useVoiceSession } from '../hooks/useVoiceSession';

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
            animation: animated ? `waveBar 1.2s ease-in-out ${i * 0.05}s infinite alternate` : 'none',
            height: animated ? undefined : '4px',
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

function PulsingDot({ color = 'var(--accent)' }) {
  return (
    <span style={{
      display: 'inline-block',
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      background: color,
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
          animation: 'ecgScan 3s ease-in-out 2.1s infinite',
          opacity: 0,
        }} />
      )}
      <svg width="100%" height="100%" viewBox="0 0 280 168" fill="none">
        {/* Glow layer */}
        <path
          d={ECG_PATH}
          stroke="#c4956a"
          strokeWidth="8"
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity="0.15"
          filter="url(#ecgBlur)"
          style={active ? {
            strokeDasharray: 500,
            strokeDashoffset: 500,
            animation: 'ecgDrawLine 1.5s cubic-bezier(0.22, 1, 0.36, 1) 0.6s forwards',
          } : {
            strokeDasharray: 'none',
            opacity: 0.1,
          }}
        />
        {/* Main line */}
        <path
          d={ECG_PATH}
          stroke="#c4956a"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={active ? {
            strokeDasharray: 500,
            strokeDashoffset: 500,
            animation: 'ecgDrawLine 1.5s cubic-bezier(0.22, 1, 0.36, 1) 0.6s forwards',
          } : {
            strokeDasharray: 'none',
          }}
        />
        {/* Pulse (peak) with breathing */}
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
            animation: 'ecgDrawLine 1.5s cubic-bezier(0.22, 1, 0.36, 1) 0.6s forwards, ecgBreathe 3s ease-in-out 2.1s infinite',
          } : {
            opacity: 0.2,
            strokeDasharray: 'none',
          }}
        />
        <defs>
          <filter id="ecgBlur">
            <feGaussianBlur stdDeviation="4" />
          </filter>
        </defs>
      </svg>
      <style>{`
        @keyframes ecgDrawLine {
          to { stroke-dashoffset: 0; }
        }
        @keyframes ecgBreathe {
          0%, 100% { opacity: 0.25; filter: drop-shadow(0 0 8px var(--accent-glow)); }
          50% { opacity: 0.6; filter: drop-shadow(0 0 20px var(--accent)); }
        }
        @keyframes ecgScan {
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
    <svg width="20" height="20" viewBox="0 0 20 20" style={{ animation: 'spin 1s linear infinite' }}>
      <circle cx="10" cy="10" r="8" fill="none" stroke="var(--accent)" strokeWidth="2" strokeDasharray="40 20" strokeLinecap="round" />
      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </svg>
  );
}

export default function ReflectPage() {
  const [searchParams] = useSearchParams();
  const resumeId = searchParams.get('resume');
  const parentId = searchParams.get('parent');

  const {
    phase, isStopping, hasError, streaming, audio, handleStart, handleStop,
    handleContinue, handleCancelProcessing,
  } = useVoiceSession({
    apiEndpoint: '/reflect',
    ttsTitle: 'Reflection',
    initialLlmNodeId: resumeId ? Number(resumeId) : null,
    initialParentId: parentId ? Number(parentId) : null,
  });

  // Progress bar helpers
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

  // --- READY / RECORDING STATE ---
  if (phase === 'ready' || phase === 'recording') {
    return (
      <div style={containerStyle}>
        <p style={{
          fontFamily: 'var(--serif)',
          fontStyle: 'italic',
          fontSize: 'clamp(1.2rem, 2.5vw, 1.6rem)',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginBottom: '40px',
        }}>
          Speak what's present...
        </p>

        {/* ECG Animation */}
        <EcgAnimation
          key={phase}
          active={phase === 'recording'}
          dim={phase === 'ready'}
          showScanline={phase === 'recording'}
        />

        {/* Waveform — freeze when stopping */}
        {phase === 'recording' && <WaveformBars animated={!isStopping} />}

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

        {/* Error indicator */}
        {hasError && phase === 'ready' && (
          <div style={{ marginBottom: '16px' }}>
            <PulsingDot color="var(--error, #e74c3c)" />
          </div>
        )}

        {/* Start button */}
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

        {/* Stop button with visual feedback */}
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
        {/* ECG Animation — breathing while processing */}
        <EcgAnimation active={true} showScanline={false} />

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

        {/* Cancel button */}
        <button
          onClick={handleCancelProcessing}
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
    <div style={containerStyle}>
      {/* ECG Animation — breathes while playing */}
      <EcgAnimation
        active={audio.isPlaying}
        dim={!audio.isPlaying}
        showScanline={false}
      />

      {/* Waveform bars — animated while playing */}
      <div style={{ marginBottom: '24px' }}>
        <WaveformBars animated={audio.isPlaying} />
      </div>

      {/* Audio controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '16px' }}>
        <button
          onClick={() => audio.skipBackward()}
          title="Skip back 10s"
          style={controlButtonStyle()}
        >
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
              // If at end of audio, restart from beginning
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

        <button
          onClick={() => audio.skipForward()}
          title="Skip forward 10s"
          style={controlButtonStyle()}
        >
          <FaRedo />
        </button>
      </div>

      {/* Progress bar */}
      <div style={{ width: '100%', maxWidth: '300px', marginBottom: '8px' }}>
        <div
          onClick={handleSeek}
          style={{
            width: '100%',
            height: '6px',
            backgroundColor: 'var(--bg-card, rgba(255,255,255,0.05))',
            borderRadius: '3px',
            cursor: 'pointer',
            position: 'relative',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${displayDuration > 0 ? (displayTime / displayDuration) * 100 : 0}%`,
              backgroundColor: 'var(--accent)',
              borderRadius: '3px',
              transition: 'width 0.1s linear',
            }}
          />
        </div>

        {/* Time display */}
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginTop: '4px',
          color: 'var(--text-muted)',
          fontSize: '0.75rem',
          fontFamily: 'var(--sans)',
          fontWeight: 300,
        }}>
          <span>{formatTime(displayTime)}</span>
          <span>
            {formatTime(displayDuration)}
            {audio.generatingTTS && (
              <span
                style={{
                  fontSize: '9px',
                  color: 'var(--accent)',
                  marginLeft: '4px',
                  animation: 'pulseDot 1.5s ease-in-out infinite',
                }}
              >
                ●
              </span>
            )}
          </span>
        </div>
      </div>

      {/* Spacer */}
      <div style={{ height: '48px' }} />

      {/* Record button to start next recording */}
      <button
        onClick={() => handleContinue()}
        title="Continue reflecting"
        style={{
          width: '56px', height: '56px', borderRadius: '50%',
          border: '2px solid var(--accent)',
          background: 'transparent',
          cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          transition: 'all 0.2s ease',
          opacity: 0.7,
        }}
        onMouseEnter={(e) => e.currentTarget.style.opacity = '1'}
        onMouseLeave={(e) => e.currentTarget.style.opacity = '0.7'}
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="var(--accent)">
          <circle cx="12" cy="12" r="8" />
        </svg>
      </button>
    </div>
  );
}
