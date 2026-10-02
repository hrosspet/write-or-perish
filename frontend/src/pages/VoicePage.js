import React, { useState, useCallback, useRef, useEffect } from 'react';
import { useSearchParams, useNavigate, Link } from 'react-router-dom';
import { FaPlay, FaPause, FaUndo, FaRedo, FaKeyboard } from 'react-icons/fa';
import { useVoiceSession } from '../hooks/useVoiceSession';
import { useUser } from '../contexts/UserContext';
import { useInterruptedRecovery } from '../hooks/useInterruptedRecovery';
import RecoveryBanner from '../components/RecoveryBanner';
import OfflineBanner from '../components/OfflineBanner';
import ProposalInline from '../components/ProposalInline';
import { useToast } from '../contexts/ToastContext';
import { isSpendBlocked, notifySpendBlocked, spendCapToastMessage } from '../utils/spendCap';
import { isAiAllowed } from '../utils/aiUsage';
import api from '../api';
import { entryQuestion, EVERYDAY_QUESTION } from '../utils/entryPrompt';

// Chain-chapter numerals — turns cap at a handful of nodes (tool-round
// budget), so a static list covers it.
const ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII'];

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
          stroke="var(--accent)"
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
          stroke="var(--accent)"
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
          stroke="var(--accent)"
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

const containerStyle = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  minHeight: 'calc(100vh - 120px)',
  padding: '40px 24px',
  background: 'radial-gradient(ellipse at 50% 40%, rgba(196,149,106,0.06) 0%, transparent 70%)',
  position: 'relative',
};

// Shown instead of the record button when AI usage keeps the account (a
// fresh thread) or the thread Voice would continue away from AI. Voice mode
// sends what is said to a model to get a reply, so it does not record
// there. Same text as the server's refusal (backend/utils/llm_nodes.py) and
// the iPhone app.
export const VOICE_NEEDS_AI_TEXT = {
  account: "Voice mode needs AI to listen and reply. Your Default AI usage "
    + "is set to None, so Loore keeps your entries away from AI. You can "
    + "change it in Account settings.",
  thread: "Voice mode needs AI to listen and reply. AI usage in this thread "
    + "is set to None, so Loore keeps it away from AI. You can change it "
    + "when you edit the thread's entries, and the default for new entries "
    + "in Account settings.",
};

const needsAiLinkStyle = {
  fontFamily: 'var(--sans)',
  fontSize: '0.9rem',
  fontWeight: 300,
  color: 'var(--accent)',
  textDecoration: 'none',
  borderBottom: '1px solid var(--border)',
  paddingBottom: '2px',
};

export function VoiceNeedsAi({ scope, threadId }) {
  return (
    <div style={containerStyle}>
      <p style={{
        fontFamily: 'var(--serif)',
        fontStyle: 'italic',
        fontSize: 'clamp(1.2rem, 2.5vw, 1.6rem)',
        fontWeight: 300,
        color: 'var(--text-muted)',
        margin: '0 0 32px 0',
        textAlign: 'center',
      }}>
        Voice mode needs AI
      </p>
      <EcgAnimation active={false} dim={true} showScanline={false} />
      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.95rem',
        fontWeight: 300,
        color: 'var(--text-secondary)',
        lineHeight: 1.7,
        maxWidth: '380px',
        margin: '0 0 28px 0',
        textAlign: 'center',
      }}>
        {VOICE_NEEDS_AI_TEXT[scope] || VOICE_NEEDS_AI_TEXT.account}
      </p>
      <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap', justifyContent: 'center' }}>
        {scope === 'thread' && threadId && (
          <Link to={`/node/${threadId}`} style={needsAiLinkStyle}>
            Back to the thread
          </Link>
        )}
        <Link to="/account#ai-usage" style={needsAiLinkStyle}>
          Account settings
        </Link>
      </div>
    </div>
  );
}

// Said on the banner of an interrupted recording where Voice mode is
// blocked and finishing it gets no reply: continuing reopens the mic only
// to finish that recording, and finalize saves it as an entry without a
// reply (backend VOICE_REPLY_SKIPPED_AI_USAGE, shown as a toast after).
export const FINISH_WITHOUT_REPLY_TEXT = "AI usage is set to None, so Loore "
  + "won't reply. If you continue, your recording is saved as an entry when "
  + "you finish.";

// GET /voice/availability for the thread *parent* continues (none: the
// account's Default AI usage decides): null when Voice may record there and
// reply, else the scope that keeps it away from AI. Rejects when the server
// cannot be asked.
function fetchVoiceRefusal(parent) {
  return api.get('/voice/availability', parent != null ? { params: { parent } } : {})
    .then((res) => {
      const data = res.data || {};
      if (data.allowed !== false) return null;
      return data.scope === 'thread' ? 'thread' : 'account';
    });
}

// Voice mode records only where a reply may follow: a fresh thread when the
// account's Default AI usage lets AI read it, a continued one (?parent= /
// ?resume=) when the server says the thread does (GET /voice/availability,
// the rule streaming init applies). A refusal while recording starts (a
// setting changed elsewhere) leads to the same message.
//
// An interrupted recording is still offered where Voice mode is blocked:
// this page is the web's only place to finish or discard one (Voice or
// dictation), so the check runs here and its banner comes before the block,
// as in the iPhone app.
export default function VoicePage() {
  const [searchParams] = useSearchParams();
  const { user } = useUser();
  const threadId = searchParams.get('parent') || searchParams.get('resume');
  // undefined while the server is asked; null: record; else the scope.
  const [refusal, setRefusal] = useState(threadId ? undefined : null);
  const recovery = useInterruptedRecovery();
  // Continue was chosen on an interrupted recording while blocked: the
  // session stays mounted to finish it, and its ready phase shows the block.
  const [finishing, setFinishing] = useState(false);

  useEffect(() => {
    if (!threadId) {
      setRefusal(null);
      return undefined;
    }
    let cancelled = false;
    setRefusal(undefined);
    fetchVoiceRefusal(threadId)
      .then((scope) => {
        if (!cancelled) setRefusal(scope);
      })
      .catch(() => {
        // Offline, or a server without the route: record as before; the
        // server still refuses a turn it may not reply to (init's 403
        // brings this message back).
        if (!cancelled) setRefusal(null);
      });
    return () => { cancelled = true; };
  }, [threadId]);

  const accountRefuses = !threadId && !!user && !isAiAllowed(user.default_ai_usage);
  const shown = accountRefuses ? 'account' : refusal;
  if (shown === undefined || (shown && !recovery.checked)) {
    return <div style={containerStyle} />;
  }
  if (shown && !recovery.interruptedDraft && !finishing) {
    return <VoiceNeedsAi scope={shown} threadId={threadId} />;
  }
  return (
    <VoiceSession
      recovery={recovery}
      blocked={shown || null}
      threadId={threadId}
      onAiUsageRefused={setRefusal}
      onFinishInterrupted={() => setFinishing(true)}
    />
  );
}

// Whether finishing *draft* (an interrupted recording) gets a reply, asked
// only where Voice mode is blocked so the banner can say what Continue leads
// to. Finalize applies the Voice rule to the thread the recording continues:
// the draft's own parent, else the page's (useVoiceSession keeps it), else
// the account's Default AI usage. undefined while asking (or no draft);
// null: a reply follows; else the scope that keeps it away from AI.
function useFinishRefusal(draft, pageParentId) {
  const parent = draft ? (draft.parent_id ?? pageParentId ?? null) : null;
  const [finishRefusal, setFinishRefusal] = useState(undefined);
  useEffect(() => {
    setFinishRefusal(undefined);
    if (!draft) return undefined;
    let cancelled = false;
    fetchVoiceRefusal(parent)
      .then((scope) => {
        if (!cancelled) setFinishRefusal(scope);
      })
      .catch(() => {
        // Unknown: no note. Finalize still refuses a reply AI usage keeps
        // out, and says so in its toast.
        if (!cancelled) setFinishRefusal(null);
      });
    return () => { cancelled = true; };
  }, [draft, parent]);
  return finishRefusal;
}

// *blocked*: the scope VoicePage blocks Voice mode for (null when it may
// record). The session is mounted while blocked only to finish an
// interrupted recording; its ready phase then shows the block, never the
// record button, and a ?resume= reply is not played.
function VoiceSession({ recovery, blocked, threadId, onAiUsageRefused, onFinishInterrupted }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const resumeId = searchParams.get('resume');
  const parentId = searchParams.get('parent');

  const {
    interruptedDraft, checked: recoveryChecked,
    handleDiscard, clearInterrupted,
  } = recovery;
  const finishRefusal = useFinishRefusal(blocked ? interruptedDraft : null, parentId);

  const [toolCallsMeta, setToolCallsMeta] = useState(null);
  const [llmContent, setLlmContent] = useState(null);
  const { addToast } = useToast();
  const setThreadParentIdRef = useRef(null);
  const lastLlmNodeIdRef = useRef(null);

  const { user, markHasOwnEntries } = useUser();
  const selectedModel = user?.preferred_model || null;
  // A fresh session asks the welcome question until the user's first entry
  // (#391); a continued thread keeps the everyday one.
  const question = threadId ? EVERYDAY_QUESTION : entryQuestion(user);

  const {
    phase, isStopping, hasError, isOnline, streaming, audio, handleStart, handleStop,
    handleContinue, handleResumeSession, handleCancelProcessing, setThreadParentId,
    handleResumeRecording,
  } = useVoiceSession({
    apiEndpoint: '/voice',
    ttsTitle: 'Voice',
    initialLlmNodeId: resumeId && !blocked ? Number(resumeId) : null,
    initialParentId: parentId ? Number(parentId) : null,
    model: selectedModel,
    aiUsage: user?.default_ai_usage || 'none',
    onAiUsageRefused,
    onLLMComplete: (nodeId, content, isResume) => {
      lastLlmNodeIdRef.current = nodeId;
      // A reply to what the user just said: their entry is saved.
      if (!isResume) markHasOwnEntries();
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
  // Chain-node chapters recorded by useVoiceSession as each node's audio
  // joins the queue (empty for single-node turns). Starts come from the
  // shared AudioContext resolver, which derives them from the LIVE
  // per-chunk durations — see chapterStartTime in AudioContext.
  const chapterStart = audio.chapterStartTime;
  const chapters = audio.currentAudio?.chapters || [];

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

  // Rendered in every phase so the user can escape to Text Mode. Anchored
  // to the top of the container (position: absolute) so it scrolls with
  // content instead of overlaying notes on small screens.
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
        position: 'absolute',
        top: '12px',
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
        {!blocked && textModeButton}
        <RecoveryBanner
          draft={interruptedDraft}
          note={blocked && finishRefusal ? FINISH_WITHOUT_REPLY_TEXT : null}
          onContinue={() => {
            // No spend-cap check: resuming finishes a recording that has
            // already started, which is always allowed (#341). Where Voice
            // mode is blocked, the same holds: this reopens the mic only for
            // that recording, and finalize decides whether a reply follows.
            const { session_id, id, chunk_count, parent_id, streaming_mime_type } = interruptedDraft;
            if (blocked && onFinishInterrupted) onFinishInterrupted();
            clearInterrupted();
            handleResumeSession({
              sessionId: session_id,
              draftId: id,
              chunkCount: chunk_count,
              parentId: parent_id,
              mimeType: streaming_mime_type,
            });
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

  // Blocked: no fresh turn. Shown after an interrupted recording was
  // finished or could not be resumed.
  if (blocked && phase === 'ready') {
    return <VoiceNeedsAi scope={blocked} threadId={threadId} />;
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
          maxWidth: '640px',
          textAlign: 'center',
        }}>
          {question}
        </p>

        <EcgAnimation
          key={phase}
          active={phase === 'recording' && !streaming.isPaused}
          dim={phase === 'ready'}
          showScanline={phase === 'recording' && !streaming.isPaused}
        />

        {phase === 'recording' && (
          <WaveformBars animated={!isStopping && !streaming.isPaused} />
        )}

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
            {streaming.isPaused && ' · paused'}
          </p>
        )}

        {phase === 'recording' && streaming.isInterrupted && (
          <p style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            fontWeight: 300,
            color: 'var(--error)',
            margin: '0 0 24px 0',
            maxWidth: '320px',
            lineHeight: 1.5,
          }}>
            Recording paused — another app took the microphone.
            Everything up to the interruption is saved.
          </p>
        )}

        {phase === 'ready' && <OfflineBanner />}

        {hasError && phase === 'ready' && (
          <div style={{ marginBottom: '16px' }}>
            <PulsingDot color="var(--error)" />
          </div>
        )}

        {phase === 'ready' && (
          <button
            onClick={() => {
              // Block before any recording starts — a long recording stopped
              // only at the end would be lost work (issue #85).
              if (isSpendBlocked()) {
                notifySpendBlocked();
                addToast(spendCapToastMessage('record'), 8000);
                return;
              }
              handleStart();
            }}
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
          streaming.isPaused && !isStopping ? (
            // Paused (mic interruption or lock-screen pause): a single Play
            // button. Stopping is play-then-stop — one state, one action.
            <button
              onClick={handleResumeRecording}
              title="Resume recording"
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
                <path d="M8 5v14l11-7z" />
              </svg>
            </button>
          ) : (
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
          )
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
          {/* Chapter boundary ticks (chain nodes) */}
          {displayDuration > 0 && chapters.length > 1 && chapters.slice(1).map((ch, i) => (
            <div
              key={i}
              style={{
                position: 'absolute',
                left: `${(chapterStart(ch) / displayDuration) * 100}%`,
                top: 0, width: '1px', height: '100%',
                background: 'var(--bg-deep, #000)',
                boxShadow: '0.5px 0 0 var(--accent)',
                opacity: 0.9,
                pointerEvents: 'none',
              }}
            />
          ))}
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

      {/* Chapters — one movement per chain node, like sections of a piece.
          Roman numeral in serif italic, first words in sans; the movement
          under the playhead is lit. Tap to jump (and resume if paused). */}
      {chapters.length > 1 && (
        <div style={{
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          gap: '3px', maxWidth: '420px', margin: '2px 0 10px',
        }}>
          {chapters.map((ch, i) => {
            const next = chapters[i + 1];
            const start = chapterStart(ch);
            const isActive = displayTime >= start
              && (!next || displayTime < chapterStart(next));
            return (
              <button
                key={i}
                onClick={() => {
                  audio.seekToCumulativeTime(start);
                  if (!audio.isPlaying) {
                    setTimeout(() => audio.play(), 50);
                  }
                }}
                title={ch.title}
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  display: 'inline-flex', alignItems: 'baseline', gap: '8px',
                  padding: '2px 0',
                  opacity: isActive ? 1 : 0.5,
                  transition: 'opacity 0.3s ease',
                }}
              >
                <span style={{
                  fontFamily: 'var(--sans)', fontWeight: 300,
                  fontSize: '0.72rem', letterSpacing: '0.02em',
                  lineHeight: 1,
                  color: isActive ? 'var(--accent)' : 'var(--text-muted)',
                }}>
                  {ROMAN[i] || i + 1}
                </span>
                <span style={{
                  fontFamily: 'var(--sans)', fontWeight: 300,
                  fontSize: '0.72rem', letterSpacing: '0.02em',
                  color: isActive ? 'var(--text-secondary)' : 'var(--text-muted)',
                  maxWidth: '280px', whiteSpace: 'nowrap',
                  overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {ch.title}
                </span>
              </button>
            );
          })}
        </div>
      )}

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
        onClick={() => {
          if (isSpendBlocked()) {
            notifySpendBlocked();
            addToast(spendCapToastMessage('record'), 8000);
            return;
          }
          handleContinue(voiceReset);
        }}
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
