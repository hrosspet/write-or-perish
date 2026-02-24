import React, { useState, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { FaRegCompass, FaPlay, FaPause, FaUndo, FaRedo } from 'react-icons/fa';
import { useVoiceSession } from '../hooks/useVoiceSession';
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
            animation: animated ? `waveBarOrient 1.2s ease-in-out ${i * 0.05}s infinite alternate` : 'none',
            height: animated ? undefined : '4px',
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

function PulsingDot({ color = 'var(--accent)' }) {
  return (
    <span style={{
      display: 'inline-block',
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      background: color,
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

function Spinner() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" style={{ animation: 'spinOrient 1s linear infinite' }}>
      <circle cx="10" cy="10" r="8" fill="none" stroke="var(--accent)" strokeWidth="2" strokeDasharray="40 20" strokeLinecap="round" />
      <style>{`
        @keyframes spinOrient {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </svg>
  );
}

function AiDot() {
  return (
    <span style={{
      display: 'inline-block',
      width: '6px',
      height: '6px',
      borderRadius: '50%',
      background: 'var(--accent)',
      animation: 'aiDotPulse 2s ease infinite',
    }}>
      <style>{`
        @keyframes aiDotPulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; box-shadow: 0 0 8px var(--accent-glow); }
        }
      `}</style>
    </span>
  );
}

/**
 * Parse markdown list items into an array of strings.
 */
function stripInlineMarkdown(text) {
  return text.replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1');
}

function parseTodoItems(text) {
  return text.split('\n')
    .map(l => stripInlineMarkdown(l.replace(/^[-*]\s*/, '').trim()))
    .filter(Boolean);
}

/**
 * Parse numbered priority items. Returns [{text, hint}].
 */
function parsePriorityItems(text) {
  return text.split('\n')
    .filter(l => l.trim())
    .map(l => {
      const cleaned = l.replace(/^\d+[.)]\s*/, '').trim();
      // Try to split on " — " or " - " for time hints
      const dashMatch = cleaned.match(/^(.+?)\s*[—–]\s*(.+)$/);
      if (dashMatch) return { text: stripInlineMarkdown(dashMatch[1].trim()), hint: dashMatch[2].trim() };
      // Try parenthetical hints
      const parenMatch = cleaned.match(/^(.+?)\s*\(([^)]+)\)\s*$/);
      if (parenMatch) return { text: stripInlineMarkdown(parenMatch[1].trim()), hint: parenMatch[2].trim() };
      return { text: stripInlineMarkdown(cleaned), hint: '' };
    })
    .filter(item => item.text);
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
  const [searchParams] = useSearchParams();
  const resumeId = searchParams.get('resume');
  const parentId = searchParams.get('parent');

  const [applied, setApplied] = useState(false);
  const [parsedResponse, setParsedResponse] = useState(null);
  const applyTriggeredForNodeRef = useRef(null);

  // Trigger backend to merge Orient update into the full todo
  const handleApplyTodo = useCallback(async (nodeId) => {
    if (!nodeId) return;
    try {
      await api.post(`/orient/${nodeId}/apply-todo`);
      setApplied(true);
    } catch (err) {
      console.error('Failed to apply todo:', err);
    }
  }, []);

  const selectedModel = localStorage.getItem('loore_selected_model') || null;

  const {
    phase, isStopping, hasError, streaming, audio, handleStart, handleStop,
    handleContinue, handleCancelProcessing,
  } = useVoiceSession({
    apiEndpoint: '/orient',
    ttsTitle: 'Orient',
    initialLlmNodeId: resumeId ? Number(resumeId) : null,
    initialParentId: parentId ? Number(parentId) : null,
    model: selectedModel,
    onLLMComplete: (nodeId, content) => {
      setParsedResponse(parseOrientResponse(content));
      // Auto-trigger todo merge
      if (applyTriggeredForNodeRef.current !== nodeId) {
        applyTriggeredForNodeRef.current = nodeId;
        handleApplyTodo(nodeId);
      }
    },
  });

  const orientReset = useCallback(() => {
    setApplied(false);
    setParsedResponse(null);
  }, []);

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

  const sectionLabelStyle = {
    fontSize: '0.68rem', letterSpacing: '0.18em', textTransform: 'uppercase',
    color: 'var(--accent)', opacity: 0.6, marginBottom: '1.2rem',
    display: 'flex', alignItems: 'center', gap: '8px',
    fontFamily: 'var(--sans)',
  };

  // --- READY / RECORDING STATE ---
  if (phase === 'ready' || phase === 'recording') {
    return (
      <div style={containerStyle}>
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
          <FaRegCompass
            size={48}
            color="var(--accent)"
            style={{
              filter: phase === 'recording' ? 'drop-shadow(0 0 12px var(--accent-glow))' : 'none',
              animation: phase === 'recording' ? 'compassPulse 2s ease-in-out infinite' : 'none',
            }}
          />
          <style>{`
            @keyframes compassPulse {
              0%, 100% { opacity: 0.7; transform: scale(1); }
              50% { opacity: 1; transform: scale(1.05); }
            }
          `}</style>
        </div>

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
        {/* Compass pulsing */}
        <div style={{ marginBottom: '32px' }}>
          <FaRegCompass
            size={48}
            color="var(--accent)"
            style={{
              filter: 'drop-shadow(0 0 12px var(--accent-glow))',
              animation: 'compassPulse 2s ease-in-out infinite',
            }}
          />
          <style>{`
            @keyframes compassPulse {
              0%, 100% { opacity: 0.7; transform: scale(1); }
              50% { opacity: 1; transform: scale(1.05); }
            }
          `}</style>
        </div>

        <PulsingDot />
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          marginTop: '16px',
        }}>
          Orienting...
        </p>

        {/* Cancel button */}
        <button
          onClick={() => handleCancelProcessing(orientReset)}
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
  const parsed = parsedResponse || {};
  const hasSections = parsed.completed || parsed.newTasks || parsed.priority || parsed.note;

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      minHeight: 'calc(100vh - 120px)',
      padding: '40px 24px',
      background: 'radial-gradient(ellipse at 50% 40%, rgba(196,149,106,0.06) 0%, transparent 70%)',
    }}>
      {/* Compass — pulsing while playing */}
      <div style={{ marginBottom: '24px' }}>
        <FaRegCompass
          size={48}
          color="var(--accent)"
          style={{
            filter: audio.isPlaying ? 'drop-shadow(0 0 12px var(--accent-glow))' : 'none',
            animation: audio.isPlaying ? 'compassPulse 2s ease-in-out infinite' : 'none',
            opacity: audio.isPlaying ? 1 : 0.5,
            transition: 'opacity 0.3s, filter 0.3s',
          }}
        />
        <style>{`
          @keyframes compassPulse {
            0%, 100% { opacity: 0.7; transform: scale(1); }
            50% { opacity: 1; transform: scale(1.05); }
          }
        `}</style>
      </div>

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
                  animation: 'pulseDotOrient 1.5s ease-in-out infinite',
                }}
              >
                ●
              </span>
            )}
          </span>
        </div>
      </div>

      {/* Todo diff — shown during playback */}
      {hasSections && (
        <div style={{ width: '100%', maxWidth: '620px', marginTop: '32px' }}>

          {/* Updated from your sharing */}
          {(parsed.completed || parsed.newTasks) && (
            <div style={{ marginBottom: '2.5rem' }}>
              <div style={sectionLabelStyle}>
                <AiDot /> Updated from your sharing
              </div>
              {/* Completed tasks */}
              {parsed.completed && parseTodoItems(parsed.completed).map((item, i) => (
                <div key={`done-${i}`} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '12px',
                  padding: '12px 0', borderBottom: '1px solid #1e1d1a',
                }}>
                  <div style={{
                    width: '18px', height: '18px', borderRadius: '50%',
                    border: '1.5px solid var(--accent-dim)', background: 'var(--accent-dim)',
                    flexShrink: 0, marginTop: '2px',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: '0.6rem', color: 'var(--bg-deep)', fontWeight: 600,
                  }}>
                    ✓
                  </div>
                  <div style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.92rem',
                    color: 'var(--text-secondary)', lineHeight: 1.5,
                    textDecoration: 'line-through', opacity: 0.4,
                  }}>
                    {item}
                  </div>
                </div>
              ))}
              {/* New/remaining tasks */}
              {parsed.newTasks && parseTodoItems(parsed.newTasks).map((item, i) => (
                <div key={`new-${i}`} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '12px',
                  padding: '12px 0', borderBottom: '1px solid #1e1d1a',
                }}>
                  <div style={{
                    width: '18px', height: '18px', borderRadius: '50%',
                    border: '1.5px solid var(--border-hover)',
                    flexShrink: 0, marginTop: '2px',
                  }} />
                  <div style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.92rem',
                    color: 'var(--text-secondary)', lineHeight: 1.5,
                  }}>
                    {item}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Suggested priority order */}
          {parsed.priority && (
            <div style={{ marginBottom: '2.5rem' }}>
              <div style={sectionLabelStyle}>
                <AiDot /> Suggested priority order
              </div>
              {parsePriorityItems(parsed.priority).map((item, i) => (
                <div key={`pri-${i}`} style={{
                  display: 'flex', alignItems: 'center', gap: '14px',
                  padding: '14px 16px', background: 'var(--bg-card)',
                  border: '1px solid var(--border)', borderRadius: '8px',
                  marginBottom: '8px', transition: 'all 0.3s',
                  cursor: 'default', overflow: 'hidden',
                }}>
                  <span style={{
                    fontFamily: 'var(--serif)', fontSize: '1.4rem',
                    color: 'var(--accent-dim)', opacity: 0.6,
                    width: '24px', textAlign: 'center', flexShrink: 0,
                  }}>
                    {i + 1}
                  </span>
                  <span style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.92rem',
                    color: 'var(--text-primary)', lineHeight: 1.4, flex: '1 1 auto',
                    minWidth: '40%', overflowWrap: 'break-word',
                  }}>
                    {item.text}
                  </span>
                  {item.hint && (
                    <span style={{
                      fontSize: '0.75rem', color: 'var(--text-muted)',
                      flex: '0 1 auto', minWidth: 0, overflowWrap: 'break-word',
                    }}>
                      {item.hint}
                    </span>
                  )}
                  <span style={{
                    color: 'var(--text-muted)', opacity: 0.3,
                    fontSize: '0.9rem', letterSpacing: '2px', flexShrink: 0,
                  }}>
                    ⋮⋮
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Personal note */}
          {parsed.note && (
            <div style={{
              marginTop: '2rem', fontFamily: 'var(--sans)', fontWeight: 300,
              fontSize: '0.88rem', lineHeight: 1.7, color: 'var(--text-muted)',
            }}>
              <span style={{ color: 'var(--text-secondary)' }}>A note: </span>
              {parsed.note}
            </div>
          )}

          {/* Applied indicator */}
          {applied && (
            <p style={{
              fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
              color: '#4ade80', textAlign: 'center', marginTop: '16px',
            }}>
              Todo updated
            </p>
          )}
        </div>
      )}

      {/* Spacer */}
      <div style={{ height: '32px' }} />

      {/* Record button to continue conversation */}
      <button
        onClick={() => handleContinue(orientReset)}
        title="Continue orienting"
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
