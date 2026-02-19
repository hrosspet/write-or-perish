import React, { useState, useEffect, useCallback } from 'react';
import api from '../api';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';

/**
 * Parse markdown checklist into sections.
 * Sections are delimited by ## headings.
 * Items are lines starting with - [ ] or - [x].
 */
function parseTodoSections(content) {
  if (!content) return [];
  const lines = content.split('\n');
  const sections = [];
  let currentSection = null;

  for (const line of lines) {
    const headingMatch = line.match(/^##\s+(.+)/);
    if (headingMatch) {
      currentSection = { title: headingMatch[1].trim(), items: [] };
      sections.push(currentSection);
      continue;
    }

    const itemMatch = line.match(/^- \[([ xX])\]\s+(.+)/);
    if (itemMatch && currentSection) {
      currentSection.items.push({
        checked: itemMatch[1] !== ' ',
        text: itemMatch[2].trim(),
        raw: line,
      });
    }
  }

  return sections;
}

/**
 * Toggle a checkbox in the raw markdown content.
 */
function toggleCheckbox(content, itemText, currentChecked) {
  const lines = content.split('\n');
  const newLines = lines.map(line => {
    const itemMatch = line.match(/^- \[([ xX])\]\s+(.+)/);
    if (itemMatch && itemMatch[2].trim() === itemText) {
      return currentChecked
        ? `- [ ] ${itemMatch[2]}`
        : `- [x] ${itemMatch[2]}`;
    }
    return line;
  });
  return newLines.join('\n');
}

function TodoItem({ item, onToggle }) {
  return (
    <div
      onClick={() => onToggle(item)}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: '12px',
        padding: '12px 0',
        borderBottom: '1px solid #1e1d1a',
        cursor: 'pointer',
      }}
    >
      <div style={{
        width: '18px', height: '18px', borderRadius: '50%',
        border: `1.5px solid ${item.checked ? 'var(--accent-dim)' : 'var(--border-hover)'}`,
        background: item.checked ? 'var(--accent-dim)' : 'none',
        flexShrink: 0, marginTop: '2px',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: '0.6rem', color: 'var(--bg-deep)', fontWeight: 600,
        transition: 'all 0.3s',
      }}>
        {item.checked && '✓'}
      </div>
      <span style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.92rem',
        fontWeight: 300,
        color: 'var(--text-secondary)',
        textDecoration: item.checked ? 'line-through' : 'none',
        opacity: item.checked ? 0.4 : 1,
        lineHeight: 1.5,
      }}>
        {item.text}
      </span>
    </div>
  );
}

export default function TodoPage() {
  const [todo, setTodo] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);

  // Version history
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);

  const fetchTodo = useCallback(async () => {
    try {
      const res = await api.get('/todo');
      setTodo(res.data.todo);
      if (res.data.todo) {
        setEditContent(res.data.todo.content);
      }
      setLoading(false);
    } catch (err) {
      console.error('Failed to load todo:', err);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTodo();
  }, [fetchTodo]);

  const handleToggle = async (item) => {
    if (!todo) return;
    const newContent = toggleCheckbox(todo.content, item.text, item.checked);
    try {
      const res = await api.put('/todo', {
        content: newContent,
        generated_by: 'user',
      });
      setTodo(res.data.todo);
      setEditContent(res.data.todo.content);
    } catch (err) {
      console.error('Failed to toggle item:', err);
    }
  };

  const handleSave = async () => {
    if (!editContent.trim()) return;
    setSaving(true);
    try {
      const res = await api.put('/todo', {
        content: editContent,
        generated_by: 'user',
      });
      setTodo(res.data.todo);
      setEditing(false);
    } catch (err) {
      console.error('Failed to save todo:', err);
    }
    setSaving(false);
  };

  const handleCreate = async () => {
    const defaultContent = `## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n`;
    setEditContent(defaultContent);
    setEditing(true);
  };

  const handleOpenHistory = async () => {
    setDrawerOpen(true);
    try {
      const res = await api.get('/todo/versions');
      setVersions(res.data.versions);
    } catch (err) {
      console.error('Failed to load versions:', err);
    }
  };

  const handleSelectVersion = async (id) => {
    setSelectedVersionId(id);
    setVersionContent(null);
    try {
      const res = await api.get(`/todo/versions/${id}`);
      setVersionContent(res.data.todo.content);
    } catch (err) {
      console.error('Failed to load version:', err);
    }
  };

  const handleRevert = async (id) => {
    try {
      const res = await api.post(`/todo/revert/${id}`);
      setTodo(res.data.todo);
      setEditContent(res.data.todo.content);
      setDrawerOpen(false);
      setSelectedVersionId(null);
      setVersionContent(null);
    } catch (err) {
      console.error('Failed to revert:', err);
    }
  };

  const formatDate = (iso) => {
    const d = new Date(iso);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return 'today';
    const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return 'yesterday';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const generatedByLabel = (g) => {
    if (g === 'user' || g === 'manual') return 'edited manually';
    if (g === 'orient_session') return 'Orient session';
    if (g === 'revert') return 'reverted';
    if (g === 'import') return 'imported';
    return g;
  };

  const sections = todo ? parseTodoSections(todo.content) : [];

  if (loading) {
    return (
      <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
        <p style={{ color: 'var(--text-muted)' }}>Loading...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: '8px', display: 'flex', alignItems: 'baseline', gap: '16px', flexWrap: 'wrap' }}>
        <h1 style={{
          fontFamily: 'var(--serif)',
          fontSize: '2rem',
          fontWeight: 300,
          color: 'var(--text-primary)',
          margin: 0,
        }}>
          Todo
        </h1>

        {todo && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <button
              onClick={() => {
                if (editing) { handleSave(); } else { setEditing(true); setEditContent(todo.content); }
              }}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
                color: 'var(--text-muted)',
                display: 'flex', alignItems: 'center', gap: '6px',
              }}
            >
              <span style={{
                width: '6px', height: '6px', borderRadius: '50%',
                background: '#4ade80', display: 'inline-block',
              }} />
              v{todo.version_number} &middot; {formatDate(todo.created_at)}
            </button>
            <button
              onClick={handleOpenHistory}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--text-muted)', opacity: 0.7,
                textDecoration: 'underline',
              }}
            >
              history
            </button>
          </div>
        )}
      </div>

      {/* Meta line */}
      {todo && (
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.75rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          margin: '0 0 16px 0',
          opacity: 0.7,
        }}>
          Last updated by {generatedByLabel(todo.generated_by)} &middot; {formatDate(todo.created_at)}
        </p>
      )}

      {/* Accent divider */}
      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* No todo yet */}
      {!todo && !editing && (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem', marginBottom: '16px' }}>
            No todo list yet. Create one to track your tasks.
          </p>
          <button
            onClick={handleCreate}
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
            Create Todo
          </button>
        </div>
      )}

      {/* Editing mode */}
      {editing && (
        <div>
          <textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            style={{
              width: '100%',
              minHeight: '400px',
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: '8px',
              color: 'var(--text-primary)',
              fontFamily: 'var(--sans)',
              fontSize: '0.85rem',
              fontWeight: 300,
              padding: '16px',
              lineHeight: 1.6,
              resize: 'vertical',
            }}
          />
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px' }}>
            <button
              onClick={handleSave}
              disabled={saving}
              style={{
                padding: '8px 20px',
                background: 'var(--accent)',
                border: 'none',
                borderRadius: '6px',
                color: 'var(--bg-deep)',
                fontFamily: 'var(--sans)',
                fontSize: '0.85rem',
                fontWeight: 400,
                cursor: saving ? 'not-allowed' : 'pointer',
                opacity: saving ? 0.6 : 1,
              }}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
            <button
              onClick={() => { setEditing(false); if (todo) setEditContent(todo.content); }}
              style={{
                padding: '8px 20px',
                background: 'none',
                border: '1px solid var(--border)',
                borderRadius: '6px',
                color: 'var(--text-muted)',
                fontFamily: 'var(--sans)',
                fontSize: '0.85rem',
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Rendered checklist */}
      {todo && !editing && (
        <div>
          {sections.map((section, i) => (
            <div key={i} style={{ marginBottom: '2.5rem' }}>
              <div style={{
                fontSize: '0.68rem', letterSpacing: '0.18em', textTransform: 'uppercase',
                color: 'var(--accent)', opacity: 0.6, marginBottom: '1.2rem',
                display: 'flex', alignItems: 'center', gap: '8px',
                fontFamily: 'var(--sans)', fontWeight: 500,
              }}>
                <span>{section.title}</span>
                <span style={{
                  color: 'var(--text-muted)', fontWeight: 300,
                  fontSize: '0.65rem', letterSpacing: '0.05em',
                  textTransform: 'none',
                }}>
                  {section.items.length}
                </span>
              </div>
              {section.items.map((item, j) => (
                <TodoItem key={j} item={item} onToggle={handleToggle} />
              ))}
              {section.items.length === 0 && (
                <p style={{ color: 'var(--text-muted)', fontSize: '0.8rem', fontStyle: 'italic', padding: '4px 0' }}>
                  No items
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Version History Drawer */}
      <VersionHistoryDrawer
        isOpen={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedVersionId(null); setVersionContent(null); }}
        title="Todo History"
        versions={versions}
        selectedVersionId={selectedVersionId}
        onSelectVersion={handleSelectVersion}
        versionContent={versionContent}
        onRevert={handleRevert}
      />
    </div>
  );
}
