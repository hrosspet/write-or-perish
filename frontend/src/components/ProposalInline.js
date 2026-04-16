import React, { useState, useRef, useEffect, useCallback } from 'react';
import api from '../api';
import MarkdownBody from './MarkdownBody';

export function stripInlineMarkdown(text) {
  return text.replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1');
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
    else if (heading.includes('note')) sections.note = body.replace(/\s*\[\w+-proposal:[^\]]*\]/g, '');
    else if (heading.includes('issue title') || heading === 'title') sections.issueTitle = body;
    else if (heading === 'description') sections.issueDescription = body;
    else if (heading === 'category') sections.issueCategory = body.trim().toLowerCase();
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

export default function ProposalInline({ content, nodeId, toolCallsMeta, onContentChange }) {
  const parsed = parseOrientResponse(content);
  const hasTodo = parsed.completed || parsed.newTasks || parsed.priority || parsed.note;
  const hasIssue = parsed.issueTitle && parsed.issueDescription;
  const [applyStatus, setApplyStatus] = useState(null);
  const [applyError, setApplyError] = useState(null);
  const [issueApplyStatus, setIssueApplyStatus] = useState(null);
  const [issueApplyError, setIssueApplyError] = useState(null);
  const [issueResult, setIssueResult] = useState(null);
  const mergePollingRef = useRef(null);
  // Interactive toggles are only enabled when the parent supplies an
  // onContentChange callback AND the proposal hasn't been applied yet.
  const toggleable = typeof onContentChange === 'function'
    && applyStatus !== 'completed'
    && applyStatus !== 'started';

  const handleToggleItem = useCallback((itemText, fromSection, toSection, opts) => {
    if (!toggleable || !nodeId) return;
    const newContent = moveProposalItem(content, itemText, fromSection, toSection, opts);
    if (newContent === content) return;
    // Optimistic update; parent updates its copy of the content.
    const prevContent = content;
    onContentChange(newContent);
    api.put(`/nodes/${nodeId}`, { content: newContent }).catch((err) => {
      console.error('Failed to toggle proposal item:', err);
      onContentChange(prevContent);
    });
  }, [content, nodeId, onContentChange, toggleable]);

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
                  <div
                    onClick={toggleable
                      ? () => handleToggleItem(item, 'completed', 'new task', { prepend: true })
                      : undefined}
                    title={toggleable ? 'Unmark as done' : undefined}
                    style={{
                      width: '16px', height: '16px', borderRadius: '50%',
                      border: '1.5px solid var(--accent-dim)', background: 'var(--accent-dim)',
                      flexShrink: 0, marginTop: '2px',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: '0.55rem', color: 'var(--bg-deep)', fontWeight: 600,
                      cursor: toggleable ? 'pointer' : 'default',
                      transition: 'all 0.2s',
                    }}
                  >✓</div>
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
                  <div
                    onClick={toggleable
                      ? () => handleToggleItem(item, 'new task', 'completed')
                      : undefined}
                    title={toggleable ? 'Mark as done' : undefined}
                    style={{
                      width: '16px', height: '16px', borderRadius: '50%',
                      border: '1.5px solid var(--border-hover)',
                      flexShrink: 0, marginTop: '2px',
                      cursor: toggleable ? 'pointer' : 'default',
                      transition: 'all 0.2s',
                    }}
                  />
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
