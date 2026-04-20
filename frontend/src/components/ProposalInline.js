import React, { useState, useRef, useEffect, useCallback } from 'react';
import api from '../api';
import MarkdownBody from './MarkdownBody';

export function stripInlineMarkdown(text) {
  return text.replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1');
}

export function stripProposalTag(text) {
  return text.replace(/\s*\[\w+-proposal:[^\]]*\]/g, '');
}

export function parseTodoItems(text) {
  return text.split('\n')
    .map(l => l.replace(/^[-*]\s*/, '').replace(/^\[[ xX]\]\s*/, '').trim())
    .map(l => stripInlineMarkdown(l))
    .filter(Boolean);
}

export function parsePriorityItems(text) {
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

export function parseOrientResponse(text) {
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
    else if (heading.includes('note')) sections.note = stripProposalTag(body);
    else if (heading.includes('issue title') || heading === 'title') sections.issueTitle = stripProposalTag(body).trim();
    else if (heading === 'description') sections.issueDescription = stripProposalTag(body).trim();
    else if (heading === 'category') sections.issueCategory = stripProposalTag(body).trim().toLowerCase();
  }
  return sections;
}

export function hasProposalSections(text) {
  if (!text) return false;
  const headings = (text.match(/^###\s+(.+)/gm) || []).map(h => h.replace(/^###\s+/, '').toLowerCase());
  const taskKeywords = ['completed', 'new task', 'new tasks', 'priority'];
  const hasTodo = headings.some(h => taskKeywords.some(kw => h.includes(kw)));
  const hasIssue = headings.some(h => h.includes('issue title') || h === 'title') &&
                   headings.some(h => h.includes('description'));
  return hasTodo || hasIssue;
}

/**
 * Move a todo-list item between `###` sections in raw LLM content.
 * Used by the interactive proposal toggles to flip a task between
 * "completed" and "new task" (or back again) and have the LLM content
 * reflect that decision so subsequent turns see the edited state.
 *
 * `fromSection` / `toSection` are lowercase substrings matched against
 * heading text (e.g. 'completed', 'new task').
 * `itemText` is the stripped display text to match raw lines against.
 * Returns the original content unchanged if the item can't be found.
 */
export function moveProposalItem(content, itemText, fromSection, toSection, { prepend = false } = {}) {
  const lines = content.split('\n');
  const sectionRegex = /^###\s+(.+)/;

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

  lines.splice(matchIdx, 1);

  if (to) {
    let toInsert = -1;
    for (let i = 0; i < lines.length; i++) {
      const m = lines[i].match(sectionRegex);
      if (m && m[1].trim().toLowerCase().includes(toSection)) {
        toInsert = i + 1;
        if (!prepend) {
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

export function stripProposalSections(text) {
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

// Decorative pulsing dot rendered next to section labels in the roomy
// variant. Inline keyframes keep the component self-contained.
function AiDot() {
  return (
    <span style={{
      display: 'inline-block',
      width: '6px',
      height: '6px',
      borderRadius: '50%',
      background: 'var(--accent)',
      animation: 'proposalAiDotPulse 2s ease infinite',
    }}>
      <style>{`
        @keyframes proposalAiDotPulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; box-shadow: 0 0 8px var(--accent-glow); }
        }
      `}</style>
    </span>
  );
}

// Per-variant styling. Compact (default) suits inline rendering under a
// highlighted card in NodeDetail. Roomy matches Voice mode's larger,
// more breathable layout for "playing back" a response at arm's length.
function sizeStyles(size) {
  const roomy = size === 'roomy';
  return {
    roomy,
    wrapper: roomy
      ? { width: '100%', maxWidth: '620px', marginTop: '32px' }
      : { marginTop: '8px', width: '100%' },
    sectionWrapper: roomy
      ? { marginBottom: '2.5rem' }
      : { marginBottom: '1rem' },
    sectionLabel: {
      fontSize: '0.68rem', letterSpacing: '0.18em', textTransform: 'uppercase',
      color: 'var(--accent)', opacity: 0.6,
      marginBottom: roomy ? '1.2rem' : '0.8rem',
      marginTop: roomy ? undefined : '1rem',
      display: 'flex', alignItems: 'center', gap: '8px',
      fontFamily: 'var(--sans)',
    },
    row: {
      display: 'flex', alignItems: 'flex-start',
      gap: roomy ? '12px' : '10px',
      padding: roomy ? '12px 0' : '8px 0',
      borderBottom: roomy ? '1px solid #1e1d1a' : '1px solid var(--border)',
    },
    circleBase: {
      width: roomy ? '18px' : '16px',
      height: roomy ? '18px' : '16px',
      borderRadius: '50%',
      flexShrink: 0,
      marginTop: '2px',
      transition: 'all 0.3s',
    },
    circleCompletedExtra: {
      border: '1.5px solid var(--accent-dim)',
      background: 'var(--accent-dim)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: roomy ? '0.6rem' : '0.55rem',
      color: 'var(--bg-deep)', fontWeight: 600,
    },
    circleNewExtra: {
      border: '1.5px solid var(--border-hover)',
    },
    itemText: {
      fontFamily: 'var(--sans)', fontWeight: 300,
      fontSize: roomy ? '0.92rem' : '0.85rem',
      color: 'var(--text-secondary)', lineHeight: 1.5,
    },
    priorityCard: {
      display: 'flex', alignItems: 'center',
      gap: roomy ? '14px' : '10px',
      padding: roomy ? '14px 16px' : '10px 12px',
      background: 'var(--bg-card)',
      border: '1px solid var(--border)',
      borderRadius: roomy ? '8px' : '6px',
      marginBottom: roomy ? '8px' : '6px',
      transition: roomy ? 'all 0.3s' : undefined,
      cursor: 'default',
      overflow: roomy ? 'hidden' : undefined,
    },
    priorityNumber: {
      fontFamily: 'var(--serif)',
      fontSize: roomy ? '1.4rem' : '1.1rem',
      color: 'var(--accent-dim)', opacity: 0.6,
      width: roomy ? '24px' : '20px',
      textAlign: 'center', flexShrink: 0,
    },
    priorityText: {
      fontFamily: 'var(--sans)', fontWeight: 300,
      fontSize: roomy ? '0.92rem' : '0.85rem',
      color: 'var(--text-primary)', lineHeight: 1.4,
      flex: '1 1 auto',
      minWidth: roomy ? '40%' : undefined,
      overflowWrap: roomy ? 'break-word' : undefined,
    },
    priorityHint: {
      fontSize: roomy ? '0.75rem' : '0.72rem',
      color: 'var(--text-muted)',
      flex: roomy ? '0 1 auto' : undefined,
      minWidth: roomy ? 0 : undefined,
      overflowWrap: roomy ? 'break-word' : undefined,
    },
    noteText: {
      fontFamily: 'var(--sans)', fontWeight: 300,
      fontSize: roomy ? '0.88rem' : '0.82rem',
      lineHeight: 1.7, color: 'var(--text-muted)',
      marginTop: roomy ? '2rem' : undefined,
      marginBottom: roomy ? undefined : '0.8rem',
    },
    applyWrapper: roomy
      ? { textAlign: 'center', marginTop: '24px' }
      : { marginTop: '8px' },
    button: {
      padding: roomy ? '10px 24px' : '7px 18px',
      background: 'none',
      border: '1px solid var(--accent)',
      borderRadius: '6px',
      color: 'var(--accent)',
      fontFamily: 'var(--sans)',
      fontSize: roomy ? '0.85rem' : '0.8rem',
      fontWeight: 400,
      cursor: 'pointer',
    },
    statusText: roomy
      ? { fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300 }
      : { fontFamily: 'var(--sans)', fontSize: '0.72rem' },
    statusTag: roomy ? 'p' : 'span',
    issueWrapper: roomy
      ? { width: '100%', maxWidth: '620px', marginTop: '32px' }
      : { marginTop: '12px' },
    issueCard: {
      padding: roomy ? '20px' : '14px',
      background: 'var(--bg-card)',
      border: '1px solid var(--border)',
      borderRadius: roomy ? '8px' : '6px',
    },
    issueTitleStyle: {
      fontFamily: 'var(--serif)',
      fontSize: roomy ? '1.2rem' : '1rem',
      color: 'var(--text-primary)',
      margin: roomy ? '0 0 12px 0' : '0 0 8px 0',
      fontWeight: 400,
    },
    issueDescStyle: {
      fontFamily: 'var(--sans)',
      fontSize: roomy ? '0.88rem' : '0.82rem',
      fontWeight: 300,
      color: 'var(--text-secondary)',
      lineHeight: 1.7,
      margin: roomy ? '0 0 12px 0' : undefined,
    },
    issueCategory: {
      display: 'inline-block',
      padding: roomy ? '3px 10px' : '2px 8px',
      fontSize: roomy ? '0.72rem' : '0.68rem',
      fontFamily: 'var(--sans)',
      fontWeight: 400, letterSpacing: '0.08em',
      textTransform: 'uppercase',
      borderRadius: roomy ? '12px' : '10px',
      border: '1px solid var(--accent)',
      color: 'var(--accent)', opacity: 0.8,
      marginTop: roomy ? undefined : '6px',
    },
    issueButtonWrapper: roomy
      ? { textAlign: 'center', marginTop: '16px' }
      : { marginTop: '8px' },
  };
}

export default function ProposalInline({
  content,
  nodeId,
  toolCallsMeta,
  onContentChange,
  onError,
  size = 'compact',
}) {
  const parsed = parseOrientResponse(content || '');
  const hasTodo = parsed.completed || parsed.newTasks || parsed.priority || parsed.note;
  const hasIssue = parsed.issueTitle && parsed.issueDescription;
  const [applyStatus, setApplyStatus] = useState(null);
  const [applyError, setApplyError] = useState(null);
  const [issueApplyStatus, setIssueApplyStatus] = useState(null);
  const [issueApplyError, setIssueApplyError] = useState(null);
  const [issueResult, setIssueResult] = useState(null);
  const mergePollingRef = useRef(null);
  const styles = sizeStyles(size);
  const toggleable = typeof onContentChange === 'function'
    && applyStatus !== 'completed'
    && applyStatus !== 'started';

  const handleToggleItem = useCallback((itemText, fromSection, toSection, opts) => {
    if (!toggleable || !nodeId) return;
    const newContent = moveProposalItem(content, itemText, fromSection, toSection, opts);
    if (newContent === content) return;
    const prevContent = content;
    onContentChange(newContent);
    api.put(`/nodes/${nodeId}`, { content: newContent }).catch((err) => {
      console.error('Failed to toggle proposal item:', err);
      onContentChange(prevContent);
      if (onError) {
        const reason = err.response?.data?.error || err.response?.statusText || err.message || 'Unknown error';
        onError(`Couldn't save change — reverted (${reason})`);
      }
    });
  }, [content, nodeId, onContentChange, onError, toggleable]);

  useEffect(() => {
    if (!toolCallsMeta) return;
    const todoEntry = toolCallsMeta.find(tc => tc.name === 'propose_todo');
    const applyTodoCall = toolCallsMeta.find(tc => tc.name === 'apply_todo_changes');
    if (todoEntry) {
      if (todoEntry.apply_status === 'completed') setApplyStatus('completed');
      else if (todoEntry.apply_status === 'failed') {
        setApplyStatus('error');
        setApplyError(todoEntry.apply_error || 'Todo merge failed');
      } else if (todoEntry.apply_status === 'started') {
        setApplyStatus('started');
      }
    } else if (applyTodoCall && applyTodoCall.status === 'success') {
      // Direct apply without a preceding proposal.
      setApplyStatus('completed');
    }
    const issueEntry = toolCallsMeta.find(tc => tc.name === 'propose_github_issue');
    const applyIssueCall = toolCallsMeta.find(tc => tc.name === 'apply_github_issue');
    if (issueEntry && issueEntry.apply_status === 'completed') {
      setIssueApplyStatus('completed');
      setIssueResult({ issue_url: issueEntry.issue_url, issue_number: issueEntry.issue_number });
    } else if (applyIssueCall && applyIssueCall.status === 'success') {
      setIssueApplyStatus('completed');
      setIssueResult({ issue_url: applyIssueCall.issue_url, issue_number: applyIssueCall.issue_number });
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
  const hasPrefsUpdate = toolCallsMeta?.some(
    tc => tc.name === 'update_ai_preferences' && tc.status === 'success'
  );

  if (!hasTodoUpdate && !hasGithubIssue && !hasPrefsUpdate) return null;

  const StatusTag = styles.statusTag;
  const sectionLabel = (text) => (
    <div style={styles.sectionLabel}>
      {styles.roomy && <AiDot />}
      {text}
    </div>
  );

  return (
    <div style={styles.wrapper}>
      {hasTodo && (
        <div>
          {(parsed.completed || parsed.newTasks) && (
            <div style={styles.sectionWrapper}>
              {sectionLabel('Updated from your sharing')}
              {parsed.completed && parseTodoItems(parsed.completed).map((item, i) => (
                <div key={`done-${i}`} style={styles.row}>
                  <div
                    onClick={toggleable
                      ? () => handleToggleItem(item, 'completed', 'new task', { prepend: true })
                      : undefined}
                    title={toggleable ? 'Unmark as done' : undefined}
                    style={{
                      ...styles.circleBase,
                      ...styles.circleCompletedExtra,
                      cursor: toggleable ? 'pointer' : 'default',
                    }}
                  >✓</div>
                  <div style={{
                    ...styles.itemText,
                    textDecoration: 'line-through', opacity: 0.4,
                  }}>{item}</div>
                </div>
              ))}
              {parsed.newTasks && parseTodoItems(parsed.newTasks).map((item, i) => (
                <div key={`new-${i}`} style={styles.row}>
                  <div
                    onClick={toggleable
                      ? () => handleToggleItem(item, 'new task', 'completed')
                      : undefined}
                    title={toggleable ? 'Mark as done' : undefined}
                    style={{
                      ...styles.circleBase,
                      ...styles.circleNewExtra,
                      cursor: toggleable ? 'pointer' : 'default',
                    }}
                  />
                  <div style={styles.itemText}>{item}</div>
                </div>
              ))}
            </div>
          )}

          {parsed.priority && (
            <div style={styles.sectionWrapper}>
              {sectionLabel('Suggested priority order')}
              {parsePriorityItems(parsed.priority).map((item, i) => (
                <div key={`pri-${i}`} style={styles.priorityCard}>
                  <span style={styles.priorityNumber}>{i + 1}</span>
                  <span style={styles.priorityText}>{item.text}</span>
                  {item.hint && (
                    <span style={styles.priorityHint}>{item.hint}</span>
                  )}
                  {styles.roomy && (
                    <span style={{
                      color: 'var(--text-muted)', opacity: 0.3,
                      fontSize: '0.9rem', letterSpacing: '2px', flexShrink: 0,
                    }}>⋮⋮</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {parsed.note && (
            <div style={styles.noteText}>
              <span style={{ color: 'var(--text-secondary)' }}>A note: </span>
              {parsed.note}
            </div>
          )}
        </div>
      )}

      {hasTodoUpdate && (
        <div style={styles.applyWrapper}>
          {!applyStatus && (
            <button onClick={handleApplyTodo} style={styles.button}>
              Apply changes to my Todo
            </button>
          )}
          {applyStatus === 'started' && (
            <StatusTag style={{ ...styles.statusText, color: 'var(--text-muted)' }}>
              Todo update started…
            </StatusTag>
          )}
          {applyStatus === 'completed' && (
            <StatusTag style={{ ...styles.statusText, color: '#4ade80' }}>
              Todo updated
            </StatusTag>
          )}
          {applyStatus === 'error' && (
            <StatusTag style={{ ...styles.statusText, color: 'var(--accent)' }}>
              {applyError || 'Todo update failed'}
            </StatusTag>
          )}
        </div>
      )}

      {hasGithubIssue && parsed.issueTitle && (
        <div style={styles.issueWrapper}>
          {sectionLabel(styles.roomy ? 'GitHub Issue Proposal' : 'GitHub Issue')}
          <div style={styles.issueCard}>
            <h3 style={styles.issueTitleStyle}>{parsed.issueTitle}</h3>
            {parsed.issueDescription && (
              <MarkdownBody
                style={styles.issueDescStyle}
                paragraphMargin={styles.roomy ? '0 0 8px 0' : '0 0 6px 0'}
              >
                {parsed.issueDescription}
              </MarkdownBody>
            )}
            {parsed.issueCategory && (
              <span style={styles.issueCategory}>{parsed.issueCategory}</span>
            )}
          </div>
          <div style={styles.issueButtonWrapper}>
            {!issueApplyStatus && (
              <button onClick={handleCreateIssue} style={styles.button}>
                {styles.roomy ? 'Create GitHub Issue' : 'Create issue'}
              </button>
            )}
            {issueApplyStatus === 'started' && (
              <StatusTag style={{ ...styles.statusText, color: 'var(--text-muted)' }}>
                Creating issue…
              </StatusTag>
            )}
            {issueApplyStatus === 'completed' && (
              <StatusTag style={{ ...styles.statusText, color: '#4ade80' }}>
                Issue created{issueResult?.issue_url && (
                  <> — <a
                    href={issueResult.issue_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: '#4ade80', textDecoration: 'underline' }}
                  >#{issueResult.issue_number}</a></>
                )}
              </StatusTag>
            )}
            {issueApplyStatus === 'error' && (
              <StatusTag style={{ ...styles.statusText, color: 'var(--accent)' }}>
                {issueApplyError || 'Issue creation failed'}
              </StatusTag>
            )}
          </div>
        </div>
      )}

      {hasPrefsUpdate && (
        <p style={{
          fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
          color: '#4ade80',
          textAlign: styles.roomy ? 'center' : 'left',
          marginTop: '16px',
        }}>
          Preferences updated
        </p>
      )}
    </div>
  );
}
