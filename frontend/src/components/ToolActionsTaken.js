import React from "react";
import { Link } from "react-router-dom";

// What an AI reply's actions read as for someone who is not its owner.
// The server sends them each action's name and outcome only (no search
// query, link, artifact kind or error), so the line names the action and
// links nowhere: a link to /todo or /share would open the viewer's own.
const SHARED_LABELS = {
  propose_todo: () => 'Todo update proposed',
  propose_github_issue: () => 'Issue proposed',
  propose_feedback: () => 'Feedback proposed',
  propose_share: () => 'Share proposed',
  apply_todo_changes: ok => (ok ? 'Todo changes confirmed' : 'Todo apply failed'),
  apply_github_issue: ok => (ok ? 'Issue creation confirmed' : 'Issue creation failed'),
  apply_feedback: ok => (ok ? 'Feedback sent' : 'Feedback send failed'),
  apply_share: ok => (ok ? 'Share saved as a draft' : 'Share save failed'),
  update_ai_preferences: () => 'Preferences updated',
  update_artifact: () => 'Wrote an artifact',
  read_artifact: () => 'Read an artifact',
  read_todo: () => 'Read the todo list',
  semantic_search: () => 'Searched archive & references',
  read_full: ok => (ok ? 'Read in full' : 'Read in full (failed)'),
};

export const sharedToolLabel = (tc) => {
  const label = SHARED_LABELS[tc?.name];
  return label ? label(tc.status === 'success') : (tc?.name || 'Action');
};

const KNOWN_TOOLS = ['propose_todo', 'propose_github_issue', 'propose_feedback', 'propose_share', 'apply_todo_changes', 'apply_github_issue', 'apply_feedback', 'apply_share', 'update_ai_preferences', 'update_artifact', 'read_artifact', 'read_todo', 'semantic_search', 'read_full'];

// The owner's line: the action with its details and links.
function OwnerToolLabel({ tc }) {
  return (
    <>
      {tc.name === 'propose_todo' && (
        <>Todo update proposed{tc.apply_status === 'completed' && ' (applied)'}{tc.apply_status === 'started' && ' (applying...)'}{tc.apply_status === 'failed' && ' (failed)'}</>
      )}
      {tc.name === 'propose_github_issue' && (
        <>Issue proposed{tc.apply_status === 'completed' && ' (created)'}{tc.apply_status === 'failed' && ' (failed)'}</>
      )}
      {tc.name === 'propose_feedback' && (
        <>Feedback proposed{tc.apply_status === 'completed' && ' (sent)'}{tc.apply_status === 'failed' && ' (failed)'}</>
      )}
      {tc.name === 'propose_share' && (
        <>Share proposed{tc.apply_status === 'completed' && ' (saved as draft)'}{tc.apply_status === 'failed' && ' (failed)'}</>
      )}
      {tc.name === 'apply_todo_changes' && (
        tc.status !== 'success' ? 'Todo apply failed'
          : tc.apply_status === 'completed' ? 'Todo changes applied'
          : tc.apply_status === 'failed' ? `Todo apply failed${tc.apply_error ? ': ' + tc.apply_error : ''}`
          : 'Todo apply in progress...'
      )}
      {tc.name === 'apply_github_issue' && (
        tc.status === 'success' ? 'Issue creation confirmed' : 'Issue creation failed'
      )}
      {tc.name === 'apply_feedback' && (
        tc.status === 'success' ? 'Feedback sent' : 'Feedback send failed'
      )}
      {tc.name === 'apply_share' && (
        tc.status === 'success'
          ? <>Share saved as a draft — <Link to="/share" style={{ color: 'var(--accent)', textDecoration: 'none' }}>Share page</Link></>
          : 'Share save failed'
      )}
      {tc.name === 'update_ai_preferences' && 'Preferences updated'}
      {tc.name === 'update_artifact' && (
        <>{tc.created ? 'Created' : 'Updated'} artifact{tc.kind ? <> <Link to={`/artifacts/${tc.kind}`} style={{ color: 'var(--accent)', textDecoration: 'none' }}><code style={{ fontSize: '0.95em' }}>{tc.kind}</code></Link></> : ''}</>
      )}
      {tc.name === 'read_artifact' && (
        <>Read artifact{tc.kind ? <> <Link to={`/artifacts/${tc.kind}`} style={{ color: 'var(--accent)', textDecoration: 'none' }}><code style={{ fontSize: '0.95em' }}>{tc.kind}</code></Link></> : ''}</>
      )}
      {tc.name === 'read_todo' && (
        <>Read <Link to="/todo" style={{ color: 'var(--accent)', textDecoration: 'none' }}>todo list</Link></>
      )}
      {tc.name === 'semantic_search' && (
        <>Searched archive & references{tc.query ? <> — <span style={{ fontStyle: 'italic' }}>“{tc.query}”</span></> : ''}</>
      )}
      {tc.name === 'read_full' && (
        tc.status !== 'success' ? 'Read in full (failed)'
          : tc.kind === 'external' ? (
            tc.url
              ? <>Read in full — <a href={tc.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', textDecoration: 'none' }}>{tc.author_handle ? `@${tc.author_handle}'s post` : 'saved reference'}</a></>
              : <>Read a saved reference in full</>
          ) : (
            <>Read in full — <Link to={`/node/${tc.ref_id}`} style={{ color: 'var(--accent)', textDecoration: 'none' }}>entry #{tc.ref_id}</Link></>
          )
      )}
      {!KNOWN_TOOLS.includes(tc.name) && tc.name}
      {tc.error && <span style={{ color: 'var(--accent)', marginLeft: '8px' }}> — {tc.error}</span>}
    </>
  );
}

// "▸ Actions taken (N)" under an AI reply and, expanded, one line per
// action. *detailed* is true for the reply's owner, whose payload carries
// every field; anyone else gets the shared line (sharedToolLabel).
export default function ToolActionsTaken({ toolCallsMeta, detailed, expanded, onToggle }) {
  const visibleTools = (Array.isArray(toolCallsMeta) ? toolCallsMeta : [])
    .filter(tc => tc && (!tc.name || !tc.name.startsWith('_')));
  if (visibleTools.length === 0) return null;
  return (
    <div style={{ marginTop: '12px', borderTop: '1px solid var(--border)', paddingTop: '8px' }}>
      <button
        onClick={onToggle}
        style={{
          background: 'none', border: 'none', cursor: 'pointer', padding: 0,
          fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
          color: 'var(--text-muted)',
        }}
      >
        {expanded ? '▾' : '▸'} Actions taken ({visibleTools.length})
      </button>
      {expanded && (
        <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {visibleTools.map((tc, i) => (
            <div key={i} style={{
              fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
              color: 'var(--text-secondary)', padding: '6px 10px',
              background: 'var(--bg-surface)', borderRadius: '6px',
              border: '1px solid var(--border)',
            }}>
              {tc.status === 'success' ? '✓' : '✗'}{' '}
              {detailed ? <OwnerToolLabel tc={tc} /> : sharedToolLabel(tc)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
