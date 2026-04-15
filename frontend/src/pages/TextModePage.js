import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useConversation } from '../hooks/useConversation';
import { useStreamingTranscription } from '../hooks/useStreamingTranscription';
import { useUser } from '../contexts/UserContext';
import MarkdownBody from '../components/MarkdownBody';
import SpeakerIcon from '../components/SpeakerIcon';
import api from '../api';

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
    else if (heading.includes('issue title') || heading === 'title') sections.issueTitle = body;
    else if (heading === 'description') sections.issueDescription = body;
    else if (heading === 'category') sections.issueCategory = body.trim().toLowerCase();
  }
  return sections;
}

function hasProposalSections(text) {
  if (!text) return false;
  const headings = (text.match(/^###\s+(.+)/gm) || []).map(h => h.replace(/^###\s+/, '').toLowerCase());
  const taskKeywords = ['completed', 'new task', 'new tasks', 'priority'];
  const hasTodo = headings.some(h => taskKeywords.some(kw => h.includes(kw)));
  const hasIssue = headings.some(h => h.includes('issue title') || h === 'title') &&
                   headings.some(h => h.includes('description'));
  return hasTodo || hasIssue;
}

function stripProposalSections(text) {
  if (!text) return text;
  const lines = text.split('\n');
  const result = [];
  let inProposal = false;
  const proposalHeadings = ['completed', 'new task', 'new tasks', 'priority', 'priority order',
    'note', 'issue title', 'title', 'description', 'category'];
  for (const line of lines) {
    const headingMatch = line.match(/^###\s+(.+)/);
    if (headingMatch) {
      const h = headingMatch[1].trim().toLowerCase();
      if (proposalHeadings.some(kw => h.includes(kw) || h === kw)) {
        inProposal = true;
        continue;
      } else {
        inProposal = false;
      }
    }
    if (!inProposal) {
      result.push(line);
    }
  }
  return result.join('\n').trim();
}

function parseTodoItems(text) {
  return text.split('\n')
    .map(l => l.replace(/^[-*]\s*/, '').replace(/^\[[ xX]\]\s*/, '').trim())
    .map(l => l.replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1'))
    .filter(Boolean);
}

function parsePriorityItems(text) {
  return text.split('\n')
    .filter(l => l.trim())
    .map(l => {
      const cleaned = l.replace(/^\d+[.)]\s*/, '').replace(/^[-*]\s*/, '').replace(/^\[[ xX]\]\s*/, '').trim();
      const stripped = cleaned.replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1');
      const dashMatch = stripped.match(/^(.+?)\s*[—–]\s*(.+)$/);
      if (dashMatch) return { text: dashMatch[1].trim(), hint: dashMatch[2].trim() };
      const parenMatch = stripped.match(/^(.+?)\s*\(([^)]+)\)\s*$/);
      if (parenMatch) return { text: parenMatch[1].trim(), hint: parenMatch[2].trim() };
      return { text: stripped, hint: '' };
    })
    .filter(item => item.text);
}

function ProposalInline({ content, nodeId, toolCallsMeta }) {
  const parsed = parseOrientResponse(content);
  const hasTodo = parsed.completed || parsed.newTasks || parsed.priority || parsed.note;
  const hasIssue = parsed.issueTitle && parsed.issueDescription;
  const [applyStatus, setApplyStatus] = useState(null);
  const [applyError, setApplyError] = useState(null);
  const [issueApplyStatus, setIssueApplyStatus] = useState(null);
  const [issueApplyError, setIssueApplyError] = useState(null);
  const [issueResult, setIssueResult] = useState(null);
  const mergePollingRef = useRef(null);

  // Check initial status from toolCallsMeta
  useEffect(() => {
    if (!toolCallsMeta) return;
    const todoEntry = toolCallsMeta.find(tc => tc.name === 'propose_todo');
    if (todoEntry) {
      if (todoEntry.apply_status === 'completed') setApplyStatus('completed');
      else if (todoEntry.apply_status === 'failed') {
        setApplyStatus('error');
        setApplyError(todoEntry.apply_error || 'Todo merge failed');
      } else if (todoEntry.apply_status === 'started') {
        setApplyStatus('started');
      }
    }
    const issueEntry = toolCallsMeta.find(tc => tc.name === 'propose_github_issue');
    if (issueEntry && issueEntry.apply_status === 'completed') {
      setIssueApplyStatus('completed');
      setIssueResult({ issue_url: issueEntry.issue_url, issue_number: issueEntry.issue_number });
    }
  }, [toolCallsMeta]);

  useEffect(() => {
    return () => {
      if (mergePollingRef.current) clearInterval(mergePollingRef.current);
    };
  }, []);

  const pollApplyStatus = useCallback((nId) => {
    mergePollingRef.current = setInterval(async () => {
      try {
        const res = await api.get(`/nodes/${nId}/llm-status`);
        const meta = res.data.tool_calls_meta;
        if (meta) {
          const todoEntry = meta.find(tc => tc.name === 'propose_todo');
          if (todoEntry?.apply_status === 'completed') {
            clearInterval(mergePollingRef.current);
            setApplyStatus('completed');
          } else if (todoEntry?.apply_status === 'failed') {
            clearInterval(mergePollingRef.current);
            setApplyStatus('error');
            setApplyError(todoEntry.apply_error || 'Todo merge failed');
          }
        }
      } catch { /* keep polling */ }
    }, 2000);
  }, []);

  const handleApplyTodo = useCallback(async () => {
    if (!nodeId) return;
    setApplyStatus('started');
    try {
      await api.post('/todo/apply-draft', { llm_node_id: nodeId });
      pollApplyStatus(nodeId);
    } catch (err) {
      const msg = err?.response?.data?.error || 'Todo update failed';
      setApplyStatus('error');
      setApplyError(msg);
    }
  }, [nodeId, pollApplyStatus]);

  const handleCreateIssue = useCallback(async () => {
    if (!nodeId) return;
    setIssueApplyStatus('started');
    try {
      const res = await api.post('/github/create-issue', { llm_node_id: nodeId });
      setIssueApplyStatus('completed');
      setIssueResult(res.data);
    } catch (err) {
      const msg = err?.response?.data?.error || 'Issue creation failed';
      setIssueApplyStatus('error');
      setIssueApplyError(msg);
    }
  }, [nodeId]);

  const hasTodoUpdate = hasTodo || toolCallsMeta?.some(tc => tc.name === 'propose_todo');
  const hasGithubIssue = hasIssue || toolCallsMeta?.some(tc => tc.name === 'propose_github_issue');

  if (!hasTodoUpdate && !hasGithubIssue) return null;

  const sectionLabelStyle = {
    fontSize: '0.68rem', letterSpacing: '0.18em', textTransform: 'uppercase',
    color: 'var(--accent)', opacity: 0.6, marginBottom: '0.8rem', marginTop: '1rem',
    display: 'flex', alignItems: 'center', gap: '8px',
    fontFamily: 'var(--sans)',
  };

  return (
    <div style={{ marginTop: '8px', width: '100%' }}>
      {hasTodo && (
        <div>
          {(parsed.completed || parsed.newTasks) && (
            <div style={{ marginBottom: '1rem' }}>
              <div style={sectionLabelStyle}>Updated from your sharing</div>
              {parsed.completed && parseTodoItems(parsed.completed).map((item, i) => (
                <div key={`done-${i}`} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '10px',
                  padding: '8px 0', borderBottom: '1px solid var(--border)',
                }}>
                  <div style={{
                    width: '16px', height: '16px', borderRadius: '50%',
                    border: '1.5px solid var(--accent-dim)', background: 'var(--accent-dim)',
                    flexShrink: 0, marginTop: '2px',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: '0.55rem', color: 'var(--bg-deep)', fontWeight: 600,
                  }}>✓</div>
                  <div style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.85rem',
                    color: 'var(--text-secondary)', lineHeight: 1.5,
                    textDecoration: 'line-through', opacity: 0.4,
                  }}>{item}</div>
                </div>
              ))}
              {parsed.newTasks && parseTodoItems(parsed.newTasks).map((item, i) => (
                <div key={`new-${i}`} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '10px',
                  padding: '8px 0', borderBottom: '1px solid var(--border)',
                }}>
                  <div style={{
                    width: '16px', height: '16px', borderRadius: '50%',
                    border: '1.5px solid var(--border-hover)',
                    flexShrink: 0, marginTop: '2px',
                  }} />
                  <div style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.85rem',
                    color: 'var(--text-secondary)', lineHeight: 1.5,
                  }}>{item}</div>
                </div>
              ))}
            </div>
          )}

          {parsed.priority && (
            <div style={{ marginBottom: '1rem' }}>
              <div style={sectionLabelStyle}>Suggested priority order</div>
              {parsePriorityItems(parsed.priority).map((item, i) => (
                <div key={`pri-${i}`} style={{
                  display: 'flex', alignItems: 'center', gap: '10px',
                  padding: '10px 12px', background: 'var(--bg-card)',
                  border: '1px solid var(--border)', borderRadius: '6px',
                  marginBottom: '6px',
                }}>
                  <span style={{
                    fontFamily: 'var(--serif)', fontSize: '1.1rem',
                    color: 'var(--accent-dim)', opacity: 0.6,
                    width: '20px', textAlign: 'center', flexShrink: 0,
                  }}>{i + 1}</span>
                  <span style={{
                    fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.85rem',
                    color: 'var(--text-primary)', lineHeight: 1.4, flex: '1 1 auto',
                  }}>{item.text}</span>
                  {item.hint && (
                    <span style={{
                      fontSize: '0.72rem', color: 'var(--text-muted)',
                    }}>{item.hint}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {parsed.note && (
            <div style={{
              fontFamily: 'var(--sans)', fontWeight: 300,
              fontSize: '0.82rem', lineHeight: 1.7, color: 'var(--text-muted)',
              marginBottom: '0.8rem',
            }}>
              <span style={{ color: 'var(--text-secondary)' }}>A note: </span>
              {parsed.note}
            </div>
          )}
        </div>
      )}

      {hasTodoUpdate && (
        <div style={{ marginTop: '8px' }}>
          {!applyStatus && (
            <button
              onClick={handleApplyTodo}
              style={{
                padding: '7px 18px', background: 'none',
                border: '1px solid var(--accent)', borderRadius: '6px',
                color: 'var(--accent)', fontFamily: 'var(--sans)',
                fontSize: '0.8rem', fontWeight: 400, cursor: 'pointer',
              }}
            >
              Confirm changes
            </button>
          )}
          {applyStatus === 'started' && (
            <span style={{ fontFamily: 'var(--sans)', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
              Applying...
            </span>
          )}
          {applyStatus === 'completed' && (
            <span style={{ fontFamily: 'var(--sans)', fontSize: '0.72rem', color: '#4ade80' }}>
              Todo updated
            </span>
          )}
          {applyStatus === 'error' && (
            <span style={{ fontFamily: 'var(--sans)', fontSize: '0.72rem', color: 'var(--accent)' }}>
              {applyError || 'Update failed'}
            </span>
          )}
        </div>
      )}

      {hasGithubIssue && parsed.issueTitle && (
        <div style={{ marginTop: '12px' }}>
          <div style={sectionLabelStyle}>GitHub Issue</div>
          <div style={{
            padding: '14px', background: 'var(--bg-card)',
            border: '1px solid var(--border)', borderRadius: '6px',
          }}>
            <h4 style={{
              fontFamily: 'var(--serif)', fontSize: '1rem',
              color: 'var(--text-primary)', margin: '0 0 8px 0', fontWeight: 400,
            }}>{parsed.issueTitle}</h4>
            {parsed.issueDescription && (
              <MarkdownBody style={{
                fontFamily: 'var(--sans)', fontSize: '0.82rem', fontWeight: 300,
                color: 'var(--text-secondary)', lineHeight: 1.7,
              }} paragraphMargin="0 0 6px 0">
                {parsed.issueDescription}
              </MarkdownBody>
            )}
            {parsed.issueCategory && (
              <span style={{
                display: 'inline-block', padding: '2px 8px',
                fontSize: '0.68rem', fontFamily: 'var(--sans)',
                fontWeight: 400, letterSpacing: '0.08em',
                textTransform: 'uppercase', borderRadius: '10px',
                border: '1px solid var(--accent)',
                color: 'var(--accent)', opacity: 0.8, marginTop: '6px',
              }}>{parsed.issueCategory}</span>
            )}
          </div>
          <div style={{ marginTop: '8px' }}>
            {!issueApplyStatus && (
              <button
                onClick={handleCreateIssue}
                style={{
                  padding: '7px 18px', background: 'none',
                  border: '1px solid var(--accent)', borderRadius: '6px',
                  color: 'var(--accent)', fontFamily: 'var(--sans)',
                  fontSize: '0.8rem', fontWeight: 400, cursor: 'pointer',
                }}
              >
                Create issue
              </button>
            )}
            {issueApplyStatus === 'started' && (
              <span style={{ fontFamily: 'var(--sans)', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                Creating...
              </span>
            )}
            {issueApplyStatus === 'completed' && (
              <span style={{ fontFamily: 'var(--sans)', fontSize: '0.72rem', color: '#4ade80' }}>
                Issue created{issueResult?.issue_url && (
                  <> — <a href={issueResult.issue_url} target="_blank" rel="noopener noreferrer"
                    style={{ color: '#4ade80', textDecoration: 'underline' }}>#{issueResult.issue_number}</a></>
                )}
              </span>
            )}
            {issueApplyStatus === 'error' && (
              <span style={{ fontFamily: 'var(--sans)', fontSize: '0.72rem', color: 'var(--accent)' }}>
                {issueApplyError || 'Issue creation failed'}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Message({ message }) {
  const isUser = message.role === 'user';
  const isPending = message.llm_task_status === 'pending' || message.llm_task_status === 'processing';
  const hasProposal = !isUser && message.content && hasProposalSections(message.content);
  const displayContent = hasProposal ? stripProposalSections(message.content) : message.content;

  return (
    <div style={{
      display: 'flex',
      justifyContent: isUser ? 'flex-end' : 'flex-start',
      marginBottom: '16px',
    }}>
      <div style={{
        maxWidth: '80%',
        padding: '12px 16px',
        borderRadius: '12px',
        background: isUser ? 'var(--accent-subtle)' : 'var(--bg-card)',
        border: isUser ? 'none' : '1px solid var(--border)',
      }}>
        {isPending && !message.content ? (
          <div style={{
            display: 'flex',
            gap: '4px',
            padding: '4px 0',
          }}>
            {[0, 1, 2].map(i => (
              <span key={i} style={{
                width: '6px', height: '6px', borderRadius: '50%',
                background: 'var(--text-muted)',
                animation: `typingDot 1.2s ease-in-out ${i * 0.2}s infinite`,
              }} />
            ))}
            <style>{`
              @keyframes typingDot {
                0%, 60%, 100% { opacity: 0.3; }
                30% { opacity: 1; }
              }
            `}</style>
          </div>
        ) : isUser ? (
          <div style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            color: 'var(--text-secondary)',
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
          }}>
            {message.content}
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '4px' }}>
              <MarkdownBody style={{
                fontFamily: 'var(--sans)',
                fontSize: '0.9rem',
                fontWeight: 300,
                color: 'var(--text-muted)',
                lineHeight: 1.6,
                flex: 1,
              }}>
                {displayContent}
              </MarkdownBody>
              {message.id && typeof message.id === 'number' && (
                <SpeakerIcon
                  nodeId={message.id}
                  content={message.content}
                  aiUsage="chat"
                />
              )}
            </div>
            {hasProposal && (
              <ProposalInline
                content={message.content}
                nodeId={message.id}
                toolCallsMeta={message.tool_calls_meta}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default function TextModePage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const resumeConvId = searchParams.get('resume');

  const { user } = useUser();
  const {
    messages, isWaitingForAI, latestLlmNodeId,
    sendMessage, loadExistingThread,
  } = useConversation({ aiUsage: user?.default_ai_usage || 'none' });
  const [inputText, setInputText] = useState('');
  const [isVoiceRecording, setIsVoiceRecording] = useState(false);
  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);
  const resumeLoadedRef = useRef(false);

  // Load existing thread when resuming from Voice mode
  useEffect(() => {
    if (resumeConvId && !resumeLoadedRef.current) {
      resumeLoadedRef.current = true;
      loadExistingThread(Number(resumeConvId));
    }
  }, [resumeConvId, loadExistingThread]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = useCallback(() => {
    const text = inputText.trim();
    if (!text || isWaitingForAI) return;
    sendMessage(text);
    setInputText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [inputText, isWaitingForAI, sendMessage]);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInputChange = (e) => {
    setInputText(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  };

  // Voice input via streaming transcription
  const voiceStreaming = useStreamingTranscription({
    privacyLevel: 'private',
    aiUsage: user?.default_ai_usage || 'none',
    onTranscriptUpdate: (text) => {
      setInputText(text);
    },
    onComplete: (data) => {
      const text = data.content || '';
      setInputText(text);
      setIsVoiceRecording(false);
      if (text.trim()) {
        sendMessage(text);
        setInputText('');
      }
    },
  });

  const handleVoiceToggle = async () => {
    if (isVoiceRecording) {
      voiceStreaming.stopStreaming();
    } else {
      try {
        setIsVoiceRecording(true);
        await voiceStreaming.startStreaming();
      } catch {
        setIsVoiceRecording(false);
      }
    }
  };

  const handleSwitchToVoice = useCallback(async () => {
    if (!latestLlmNodeId) {
      navigate('/voice');
      return;
    }
    try {
      const res = await api.post(`/voice/from-node/${latestLlmNodeId}`);
      const { llm_node_id, parent_id } = res.data;
      navigate(`/voice?resume=${llm_node_id}&parent=${parent_id}`);
    } catch {
      navigate('/voice');
    }
  }, [latestLlmNodeId, navigate]);

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: 'calc(100vh - 60px)',
      maxWidth: '800px',
      margin: '0 auto',
      padding: '0 16px',
    }}>
      {/* Mode switch button */}
      <div style={{
        display: 'flex',
        justifyContent: 'flex-end',
        padding: '8px 0 0 0',
      }}>
        <button
          onClick={handleSwitchToVoice}
          style={{
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
          Voice Mode
        </button>
      </div>

      {/* Messages area */}
      <div style={{
        flex: 1,
        overflow: 'auto',
        padding: '12px 0 24px 0',
      }}>
        {messages.length === 0 && (
          <div style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            opacity: 0.5,
          }}>
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1">
              <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
            </svg>
            <p style={{
              fontFamily: 'var(--serif)',
              fontStyle: 'italic',
              fontSize: '1rem',
              color: 'var(--text-muted)',
              marginTop: '16px',
            }}>
              Ask anything. Think out loud.
            </p>
          </div>
        )}

        {messages.map((msg) => (
          <Message key={msg.id} message={msg} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input bar */}
      <div style={{
        borderTop: '1px solid var(--border)',
        padding: '16px 0',
        display: 'flex',
        gap: '8px',
        alignItems: 'flex-end',
      }}>
        {/* Mic button */}
        <button
          onClick={handleVoiceToggle}
          style={{
            width: '40px',
            height: '40px',
            borderRadius: '50%',
            border: isVoiceRecording ? '2px solid #dc3545' : '1px solid var(--border)',
            background: isVoiceRecording ? 'rgba(220,53,69,0.1)' : 'transparent',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          {isVoiceRecording ? (
            <svg width="16" height="16" viewBox="0 0 20 20" fill="#dc3545">
              <rect x="4" y="4" width="12" height="12" rx="2" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="var(--text-muted)">
              <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
              <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
            </svg>
          )}
        </button>

        {/* Text input */}
        <textarea
          ref={textareaRef}
          value={inputText}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          placeholder="Type a message..."
          rows={1}
          style={{
            flex: 1,
            background: 'var(--bg-input)',
            border: '1px solid var(--border)',
            borderRadius: '12px',
            color: 'var(--text-primary)',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            padding: '10px 16px',
            lineHeight: 1.5,
            resize: 'none',
            maxHeight: '160px',
          }}
        />

        {/* Send button */}
        <button
          onClick={handleSend}
          disabled={!inputText.trim() || isWaitingForAI}
          style={{
            width: '40px',
            height: '40px',
            borderRadius: '50%',
            border: 'none',
            background: inputText.trim() && !isWaitingForAI ? 'var(--accent)' : 'var(--border)',
            cursor: inputText.trim() && !isWaitingForAI ? 'pointer' : 'not-allowed',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            transition: 'background 0.2s ease',
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill={inputText.trim() && !isWaitingForAI ? 'var(--bg-deep)' : 'var(--text-muted)'}>
            <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
