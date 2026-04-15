import React, { useState, useCallback, useRef, useEffect } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { FaPlay, FaPause, FaUndo, FaRedo } from 'react-icons/fa';
import { useVoiceSession } from '../hooks/useVoiceSession';
import { useUser } from '../contexts/UserContext';
import { useInterruptedRecovery } from '../hooks/useInterruptedRecovery';
import RecoveryBanner from '../components/RecoveryBanner';
import MarkdownBody from '../components/MarkdownBody';
import OfflineBanner from '../components/OfflineBanner';
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

function AiDot() {
  return (
    <span style={{
      display: 'inline-block',
      width: '6px',
      height: '6px',
      borderRadius: '50%',
      background: 'var(--accent)',
      animation: 'aiDotPulseVoice 2s ease infinite',
    }}>
      <style>{`
        @keyframes aiDotPulseVoice {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; box-shadow: 0 0 8px var(--accent-glow); }
        }
      `}</style>
    </span>
  );
}

function stripInlineMarkdown(text) {
  return text.replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1');
}

function parseTodoItems(text) {
  return text.split('\n')
    .map(l => l.replace(/^[-*]\s*/, '').replace(/^\[[ xX]\]\s*/, '').trim())
    .map(l => stripInlineMarkdown(l))
    .filter(Boolean);
}

function parsePriorityItems(text) {
  return text.split('\n')
    .filter(l => l.trim())
    .map(l => {
      const cleaned = l.replace(/^\d+[.)]\s*/, '').replace(/^[-*]\s*/, '').replace(/^\[[ xX]\]\s*/, '').trim();
      const dashMatch = cleaned.match(/^(.+?)\s*[—–]\s*(.+)$/);
      if (dashMatch) return { text: stripInlineMarkdown(dashMatch[1].trim()), hint: dashMatch[2].trim() };
      const parenMatch = cleaned.match(/^(.+?)\s*\(([^)]+)\)\s*$/);
      if (parenMatch) return { text: stripInlineMarkdown(parenMatch[1].trim()), hint: parenMatch[2].trim() };
      return { text: stripInlineMarkdown(cleaned), hint: '' };
    })
    .filter(item => item.text);
}

/**
 * Move an item between ### sections in the raw voice response.
 * `fromSection` / `toSection` are lowercase substrings matched against headings
 * (e.g. 'completed', 'new task').
 * `itemText` is the stripped display text to match against raw lines.
 */
function moveProposalItem(content, itemText, fromSection, toSection, { prepend = false } = {}) {
  const lines = content.split('\n');
  const sectionRegex = /^###\s+(.+)/;

  // Find section ranges: { heading, start, end } (end is exclusive)
  const sections = [];
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(sectionRegex);
    if (m) {
      if (sections.length > 0) sections[sections.length - 1].end = i;
      sections.push({ heading: m[1].trim().toLowerCase(), start: i, end: lines.length });
    }
  }
  if (sections.length > 0) sections[sections.length - 1].end = lines.length;

  const from = sections.find(s => s.heading.includes(fromSection));
  const to = sections.find(s => s.heading.includes(toSection));
  if (!from) return content;

  // Find the matching line in the 'from' section
  let matchIdx = -1;
  let rawLine = null;
  for (let i = from.start + 1; i < from.end; i++) {
    const stripped = stripInlineMarkdown(
      lines[i].replace(/^[-*]\s*/, '').replace(/^\[[ xX]\]\s*/, '').replace(/^\d+[.)]\s*/, '').trim()
    );
    if (stripped === itemText) {
      matchIdx = i;
      rawLine = lines[i];
      break;
    }
  }
  if (matchIdx < 0) return content;

  // Remove the line from 'from' section
  lines.splice(matchIdx, 1);

  if (to) {
    // Re-find 'to' section after splice (indices shifted if from was before to)
    let toInsert = -1;
    for (let i = 0; i < lines.length; i++) {
      const m = lines[i].match(sectionRegex);
      if (m && m[1].trim().toLowerCase().includes(toSection)) {
        toInsert = i + 1;
        if (!prepend) {
          // Append: skip past existing items
          while (toInsert < lines.length && !lines[toInsert].match(sectionRegex) && lines[toInsert].trim()) {
            toInsert++;
          }
        }
        break;
      }
    }
    if (toInsert >= 0) {
      lines.splice(toInsert, 0, rawLine);
    }
  }

  return lines.join('\n');
}

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
    else if (heading.includes('note')) sections.note = body.replace(/\s*\[\w+-proposal:[^\]]*\]/g, '');
    else if (heading.includes('issue title') || heading === 'title') sections.issueTitle = body;
    else if (heading === 'description') sections.issueDescription = body;
    else if (heading === 'category') sections.issueCategory = body.trim().toLowerCase();
  }
  return sections;
}

export default function VoicePage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const resumeId = searchParams.get('resume');
  const parentId = searchParams.get('parent');

  const {
    interruptedDraft, checked: recoveryChecked,
    handleDiscard, clearInterrupted,
  } = useInterruptedRecovery();

  const [toolCallsMeta, setToolCallsMeta] = useState(null);
  // applyStatus: null | 'started' | 'completed' | 'error'
  const [applyStatus, setApplyStatus] = useState(null);
  const [applyError, setApplyError] = useState(null);
  const [parsedResponse, setParsedResponse] = useState(null);
  const [llmContent, setLlmContent] = useState(null);
  const { addToast } = useToast();
  // GitHub issue state
  const [issueApplyStatus, setIssueApplyStatus] = useState(null);
  const [issueApplyError, setIssueApplyError] = useState(null);
  const [issueResult, setIssueResult] = useState(null);
  const setThreadParentIdRef = useRef(null);
  const lastLlmNodeIdRef = useRef(null);
  const mergePollingRef = useRef(null);

  const pollApplyStatus = useCallback((nodeId) => {
    mergePollingRef.current = setInterval(async () => {
      try {
        const res = await api.get(`/nodes/${nodeId}/llm-status`);
        const meta = res.data.tool_calls_meta;
        if (meta) {
          const todoEntry = meta.find(tc => tc.name === 'propose_todo');
          if (todoEntry?.apply_status === 'completed') {
            clearInterval(mergePollingRef.current);
            mergePollingRef.current = null;
            setApplyStatus('completed');
          } else if (todoEntry?.apply_status === 'failed') {
            clearInterval(mergePollingRef.current);
            mergePollingRef.current = null;
            setApplyStatus('error');
            setApplyError(todoEntry.apply_error || 'Todo merge failed');
          }
        }
      } catch { /* keep polling */ }
    }, 2000);
  }, []);

  const handleApplyTodo = useCallback(async (nodeId) => {
    if (!nodeId) return;
    setApplyStatus('started');
    try {
      await api.post('/todo/apply-draft', { llm_node_id: nodeId });
      // Poll tool_calls_meta on the LLM node for apply_status
      pollApplyStatus(nodeId);
    } catch (err) {
      console.error('Failed to apply todo:', err);
      const msg = err?.response?.data?.error || 'Todo update failed';
      setApplyStatus('error');
      setApplyError(msg);
    }
  }, [pollApplyStatus]);

  const handleCreateIssue = useCallback(async (nodeId) => {
    if (!nodeId) return;
    setIssueApplyStatus('started');
    try {
      const res = await api.post('/github/create-issue', { llm_node_id: nodeId });
      setIssueApplyStatus('completed');
      setIssueResult(res.data);
    } catch (err) {
      console.error('Failed to create GitHub issue:', err);
      const msg = err?.response?.data?.error || 'Issue creation failed';
      setIssueApplyStatus('error');
      setIssueApplyError(msg);
    }
  }, []);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (mergePollingRef.current) clearInterval(mergePollingRef.current);
    };
  }, []);

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
      // Parse orient-style sections from LLM text (which IS the summary)
      const parsed = parseOrientResponse(content);
      if (parsed.completed || parsed.newTasks || parsed.priority || parsed.note || parsed.issueTitle) {
        setParsedResponse(parsed);
      } else {
        setParsedResponse(null);
      }

      // Fetch tool call metadata (for apply actions and auto-detected drafts)
      api.get(`/nodes/${nodeId}/llm-status`).then(res => {
        if (res.data.tool_calls_meta) {
          setToolCallsMeta(res.data.tool_calls_meta);

          // Check todo draft status (auto-detected or from apply_todo_changes)
          const todoEntry = res.data.tool_calls_meta.find(tc => tc.name === 'propose_todo');
          if (todoEntry) {
            if (todoEntry.apply_status === 'completed') {
              setApplyStatus('completed');
            } else if (todoEntry.apply_status === 'failed') {
              setApplyStatus('error');
              setApplyError(todoEntry.apply_error || 'Todo merge failed');
            } else if (todoEntry.apply_status === 'started') {
              setApplyStatus('started');
              pollApplyStatus(nodeId);
            }
          }
          const applyCall = res.data.tool_calls_meta.find(tc => tc.name === 'apply_todo_changes');
          if (applyCall && applyCall.status === 'success' && !todoEntry) {
            setApplyStatus('started');
            pollApplyStatus(nodeId);
          }

          // Check GitHub issue status
          const issueEntry = res.data.tool_calls_meta.find(tc => tc.name === 'propose_github_issue');
          if (issueEntry && issueEntry.apply_status === 'completed') {
            setIssueApplyStatus('completed');
            setIssueResult({ issue_url: issueEntry.issue_url, issue_number: issueEntry.issue_number });
          }
          const applyIssueCall = res.data.tool_calls_meta.find(tc => tc.name === 'apply_github_issue');
          if (applyIssueCall && applyIssueCall.status === 'success') {
            setIssueApplyStatus('completed');
            setIssueResult({ issue_url: applyIssueCall.issue_url, issue_number: applyIssueCall.issue_number });
          }
        }
      }).catch(() => { /* non-fatal */ });
    },
  });

  setThreadParentIdRef.current = setThreadParentId;

  const voiceReset = useCallback(() => {
    setApplyStatus(null);
    setApplyError(null);
    setParsedResponse(null);
    setLlmContent(null);
    setToolCallsMeta(null);
    setIssueApplyStatus(null);
    setIssueApplyError(null);
    setIssueResult(null);
    if (mergePollingRef.current) {
      clearInterval(mergePollingRef.current);
      mergePollingRef.current = null;
    }
  }, []);

  const handleProposalToggle = useCallback((itemText, fromSection, toSection, opts) => {
    if (!llmContent || !lastLlmNodeIdRef.current) return;
    const prevContent = llmContent;
    const newContent = moveProposalItem(llmContent, itemText, fromSection, toSection, opts);
    if (newContent === prevContent) return;
    // Optimistic update
    setLlmContent(newContent);
    setParsedResponse(parseOrientResponse(newContent));
    api.put(`/nodes/${lastLlmNodeIdRef.current}`, { content: newContent }).catch((err) => {
      console.error('Failed to toggle proposal item:', err);
      setLlmContent(prevContent);
      setParsedResponse(parseOrientResponse(prevContent));
      const reason = err.response?.data?.error || err.response?.statusText || err.message || 'Unknown error';
      addToast(`Couldn't save change — reverted (${reason})`);
    });
  }, [llmContent, addToast]);

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
    return <div style={containerStyle} />;
  }

  if (showRecovery) {
    return (
      <div style={containerStyle}>
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
  const parsed = parsedResponse || {};
  const hasSections = parsed.completed || parsed.newTasks || parsed.priority || parsed.note;
  // Detect proposals from heading structure (primary) or tool_calls_meta (fallback)
  const hasTodoUpdate = hasSections || toolCallsMeta?.some(tc => tc.name === 'propose_todo');
  const hasGithubIssue = (parsed.issueTitle && parsed.issueDescription) || toolCallsMeta?.some(tc => tc.name === 'propose_github_issue');
  const hasPrefsUpdate = toolCallsMeta?.some(tc => tc.name === 'update_ai_preferences' && tc.status === 'success');

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
      {/* Text Mode switch */}
      <button
        onClick={() => {
          const nodeId = lastLlmNodeIdRef.current;
          if (nodeId) {
            navigate(`/textmode?resume=${nodeId}`);
          } else {
            navigate('/textmode');
          }
        }}
        style={{
          position: 'absolute',
          top: '12px',
          right: '12px',
          background: 'none',
          border: 'none',
          fontFamily: 'var(--sans)',
          fontSize: '0.75rem',
          color: 'var(--text-muted)',
          cursor: 'pointer',
          padding: '4px 8px',
          opacity: 0.6,
          transition: 'opacity 0.2s',
        }}
        onMouseEnter={(e) => e.target.style.opacity = '1'}
        onMouseLeave={(e) => e.target.style.opacity = '0.6'}
      >
        Text Mode
      </button>

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

      {/* Todo update sections */}
      {hasSections && (
        <div style={{ width: '100%', maxWidth: '620px', marginTop: '32px' }}>
          {(parsed.completed || parsed.newTasks) && (
            <div style={{ marginBottom: '2.5rem' }}>
              <div style={sectionLabelStyle}>
                <AiDot /> Updated from your sharing
              </div>
              {parsed.completed && parseTodoItems(parsed.completed).map((item, i) => (
                <div key={`done-${i}`} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '12px',
                  padding: '12px 0', borderBottom: '1px solid #1e1d1a',
                }}>
                  <div
                    onClick={() => handleProposalToggle(item, 'completed', 'new task', { prepend: true })}
                    style={{
                      width: '18px', height: '18px', borderRadius: '50%',
                      border: '1.5px solid var(--accent-dim)', background: 'var(--accent-dim)',
                      flexShrink: 0, marginTop: '2px',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: '0.6rem', color: 'var(--bg-deep)', fontWeight: 600,
                      cursor: 'pointer', transition: 'all 0.3s',
                    }}>✓</div>
                  <div style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.92rem',
                    color: 'var(--text-secondary)', lineHeight: 1.5,
                    textDecoration: 'line-through', opacity: 0.4,
                  }}>{item}</div>
                </div>
              ))}
              {parsed.newTasks && parseTodoItems(parsed.newTasks).map((item, i) => (
                <div key={`new-${i}`} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '12px',
                  padding: '12px 0', borderBottom: '1px solid #1e1d1a',
                }}>
                  <div
                    onClick={() => handleProposalToggle(item, 'new task', 'completed')}
                    style={{
                      width: '18px', height: '18px', borderRadius: '50%',
                      border: '1.5px solid var(--border-hover)',
                      flexShrink: 0, marginTop: '2px',
                      cursor: 'pointer', transition: 'all 0.3s',
                    }} />
                  <div style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.92rem',
                    color: 'var(--text-secondary)', lineHeight: 1.5,
                  }}>{item}</div>
                </div>
              ))}
            </div>
          )}

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
                  }}>{i + 1}</span>
                  <span style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.92rem',
                    color: 'var(--text-primary)', lineHeight: 1.4, flex: '1 1 auto',
                    minWidth: '40%', overflowWrap: 'break-word',
                  }}>{item.text}</span>
                  {item.hint && (
                    <span style={{
                      fontSize: '0.75rem', color: 'var(--text-muted)',
                      flex: '0 1 auto', minWidth: 0, overflowWrap: 'break-word',
                    }}>{item.hint}</span>
                  )}
                  <span style={{
                    color: 'var(--text-muted)', opacity: 0.3,
                    fontSize: '0.9rem', letterSpacing: '2px', flexShrink: 0,
                  }}>⋮⋮</span>
                </div>
              ))}
            </div>
          )}

          {parsed.note && (
            <div style={{
              marginTop: '2rem', fontFamily: 'var(--sans)', fontWeight: 300,
              fontSize: '0.88rem', lineHeight: 1.7, color: 'var(--text-muted)',
            }}>
              <span style={{ color: 'var(--text-secondary)' }}>A note: </span>
              {parsed.note}
            </div>
          )}

          {/* Apply button for pending todo changes */}
          {hasTodoUpdate && (
            <div style={{ textAlign: 'center', marginTop: '24px' }}>
              {!applyStatus && (
                <button
                  onClick={() => handleApplyTodo(lastLlmNodeIdRef.current)}
                  style={{
                    padding: '10px 24px',
                    background: 'none',
                    border: '1px solid var(--accent)',
                    borderRadius: '6px',
                    color: 'var(--accent)',
                    fontFamily: 'var(--sans)',
                    fontSize: '0.85rem',
                    fontWeight: 400,
                    cursor: 'pointer',
                  }}
                >
                  Apply changes to my Todo
                </button>
              )}
              {applyStatus === 'started' && (
                <p style={{
                  fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                  color: 'var(--text-muted)',
                }}>
                  Todo update started...
                </p>
              )}
              {applyStatus === 'completed' && (
                <p style={{
                  fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                  color: '#4ade80',
                }}>
                  Todo updated
                </p>
              )}
              {applyStatus === 'error' && (
                <p style={{
                  fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                  color: 'var(--accent)',
                }}>
                  {applyError || 'Todo update failed'}
                </p>
              )}
            </div>
          )}
        </div>
      )}

      {/* GitHub issue proposal */}
      {hasGithubIssue && parsed.issueTitle && (
        <div style={{ width: '100%', maxWidth: '620px', marginTop: '32px' }}>
          <div style={sectionLabelStyle}>
            <AiDot /> GitHub Issue Proposal
          </div>
          <div style={{
            padding: '20px', background: 'var(--bg-card)',
            border: '1px solid var(--border)', borderRadius: '8px',
          }}>
            <h3 style={{
              fontFamily: 'var(--serif)', fontSize: '1.2rem',
              color: 'var(--text-primary)', margin: '0 0 12px 0', fontWeight: 400,
            }}>{parsed.issueTitle}</h3>
            {parsed.issueDescription && (
              <MarkdownBody style={{
                fontFamily: 'var(--sans)', fontSize: '0.88rem', fontWeight: 300,
                color: 'var(--text-secondary)', lineHeight: 1.7, margin: '0 0 12px 0',
              }} paragraphMargin="0 0 8px 0">
                {parsed.issueDescription}
              </MarkdownBody>
            )}
            {parsed.issueCategory && (
              <span style={{
                display: 'inline-block', padding: '3px 10px',
                fontSize: '0.72rem', fontFamily: 'var(--sans)',
                fontWeight: 400, letterSpacing: '0.08em',
                textTransform: 'uppercase',
                borderRadius: '12px',
                border: '1px solid var(--accent)',
                color: 'var(--accent)', opacity: 0.8,
              }}>{parsed.issueCategory}</span>
            )}
          </div>
          <div style={{ textAlign: 'center', marginTop: '16px' }}>
            {!issueApplyStatus && (
              <button
                onClick={() => handleCreateIssue(lastLlmNodeIdRef.current)}
                style={{
                  padding: '10px 24px',
                  background: 'none',
                  border: '1px solid var(--accent)',
                  borderRadius: '6px',
                  color: 'var(--accent)',
                  fontFamily: 'var(--sans)',
                  fontSize: '0.85rem',
                  fontWeight: 400,
                  cursor: 'pointer',
                }}
              >
                Create GitHub Issue
              </button>
            )}
            {issueApplyStatus === 'started' && (
              <p style={{
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--text-muted)',
              }}>
                Creating issue...
              </p>
            )}
            {issueApplyStatus === 'completed' && (
              <p style={{
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: '#4ade80',
              }}>
                Issue created{issueResult?.issue_url && (
                  <> — <a
                    href={issueResult.issue_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: '#4ade80', textDecoration: 'underline' }}
                  >#{issueResult.issue_number}</a></>
                )}
              </p>
            )}
            {issueApplyStatus === 'error' && (
              <p style={{
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--accent)',
              }}>
                {issueApplyError || 'Issue creation failed'}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Preferences update indicator */}
      {hasPrefsUpdate && (
        <p style={{
          fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
          color: '#4ade80', textAlign: 'center', marginTop: '16px',
        }}>
          Preferences updated
        </p>
      )}

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
