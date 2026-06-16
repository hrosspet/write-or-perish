import React, { useState, useEffect, useCallback, useRef } from 'react';
import api from '../api';
import { useCheckboxToggle, useTaskInsert, appendItemToSection } from '../utils/markdown';
import { formatDate } from '../utils/date';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';
import ArtifactsNav from '../components/ArtifactsNav';
import useSubmitShortcut from '../hooks/useSubmitShortcut';

/**
 * Parse markdown checklist into sections with nested items.
 * Sections are delimited by ## headings.
 * Items can be checkbox lines (- [ ] or - [x]) or plain list items (- text).
 * Plain items act as category headers — they nest children but have no checkbox.
 * Indentation determines nesting depth (2 spaces per level).
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

    // Auto-create a default section if items appear before any heading
    if (!currentSection && (line.match(/^(\s*)- \[([ xX])\]\s+(.+)/) || line.match(/^(\s*)- (.+)/))) {
      currentSection = { title: '', items: [] };
      sections.push(currentSection);
    }

    // Match checkbox items: - [ ] text or - [x] text
    const checkboxMatch = line.match(/^(\s*)- \[([ xX])\]\s+(.+)/);
    // Match plain list items: - text (but not checkbox lines)
    const plainMatch = !checkboxMatch && line.match(/^(\s*)- (.+)/);

    const itemMatch = checkboxMatch || plainMatch;
    if (itemMatch && currentSection) {
      const indent = itemMatch[1].length;
      const depth = Math.floor(indent / 2);
      const item = checkboxMatch
        ? {
            checked: checkboxMatch[2] !== ' ',
            text: checkboxMatch[3].trim(),
            raw: line,
            depth,
            children: [],
          }
        : {
            checked: null, // plain item, no checkbox
            text: plainMatch[2].trim(),
            raw: line,
            depth,
            children: [],
          };

      if (depth === 0) {
        currentSection.items.push(item);
      } else {
        // Find the parent item at (depth - 1) by walking the tree
        let parent = findParentAtDepth(currentSection.items, depth - 1);
        if (parent) {
          parent.children.push(item);
        } else {
          // Fallback: treat as top-level if no parent found
          currentSection.items.push(item);
        }
      }
    }
  }

  return sections;
}

/**
 * Find the last item at the given depth by walking children recursively.
 */
function findParentAtDepth(items, targetDepth) {
  if (items.length === 0) return null;
  const last = items[items.length - 1];
  if (last.depth === targetDepth) return last;
  if (last.children.length > 0) {
    return findParentAtDepth(last.children, targetDepth);
  }
  return null;
}

function countAllItems(items) {
  let count = 0;
  for (const item of items) {
    count += 1 + countAllItems(item.children);
  }
  return count;
}

function TodoItem({ item, onToggle, onInsertAfter, addingKey, setAddingKey, depth = 0 }) {
  const [collapsed, setCollapsed] = useState(true);
  const [hovered, setHovered] = useState(false);
  const [addText, setAddText] = useState('');
  const hasChildren = item.children && item.children.length > 0;
  const childCount = hasChildren ? countAllItems(item.children) : 0;
  const addable = item.checked !== null && typeof onInsertAfter === 'function';
  const adding = addingKey === item.text;
  const closeAdd = () => { setAddingKey(null); setAddText(''); };

  const submitAdd = () => {
    const t = addText.trim();
    if (t) onInsertAfter(item, t);
    closeAdd();
  };

  return (
    <div>
      <div
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '12px',
          padding: '12px 0',
          borderBottom: '1px solid var(--bg-surface)',
          paddingLeft: depth * 24,
        }}
      >
        {item.checked !== null ? (
          <div
            onClick={() => onToggle(item)}
            style={{
              width: '18px', height: '18px', borderRadius: '50%',
              border: `1.5px solid ${item.checked ? 'var(--accent-dim)' : 'var(--border-hover)'}`,
              background: item.checked ? 'var(--accent-dim)' : 'none',
              flexShrink: 0, marginTop: '2px',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '0.6rem', color: 'var(--bg-deep)', fontWeight: 600,
              transition: 'all 0.3s', cursor: 'pointer',
            }}>
            {item.checked && '✓'}
          </div>
        ) : (
          <div style={{ width: '18px', flexShrink: 0 }} />
        )}
        <span style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.92rem',
          fontWeight: item.checked === null ? 400 : 300,
          color: item.checked === null ? 'var(--text-primary)' : 'var(--text-secondary)',
          textDecoration: item.checked ? 'line-through' : 'none',
          opacity: item.checked ? 0.4 : 1,
          lineHeight: 1.5,
        }}>
          {item.text}
        </span>
        {hasChildren && (
          <div
            onClick={() => setCollapsed(!collapsed)}
            style={{
              display: 'flex', alignItems: 'center', gap: '4px',
              cursor: 'pointer', userSelect: 'none',
              marginTop: '3px',
            }}
          >
            <span style={{
              color: 'var(--text-muted)', fontSize: '0.55rem',
              transition: 'transform 0.2s',
              transform: collapsed ? 'rotate(0deg)' : 'rotate(90deg)',
              display: 'inline-block',
            }}>
              ▶
            </span>
            <span style={{
              color: 'var(--text-muted)', fontWeight: 300,
              fontSize: '0.65rem', letterSpacing: '0.05em',
            }}>
              {childCount}
            </span>
          </div>
        )}
        {addable && (
          <button
            type="button"
            onMouseDown={(e) => { e.preventDefault(); setAddText(''); setAddingKey(item.text); }}
            onClick={() => { setAddText(''); setAddingKey(item.text); }}
            title="Add an item below"
            aria-label="Add an item below"
            style={{
              opacity: hovered ? 1 : 0,
              transition: 'opacity 0.12s ease',
              background: 'none', border: 'none',
              color: 'var(--accent)', cursor: 'pointer',
              fontSize: '1.2rem', lineHeight: 1, padding: '0 4px',
              alignSelf: 'center',
            }}
          >+</button>
        )}
      </div>
      {adding && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '8px 0', paddingLeft: depth * 24 }}>
          <span style={{ width: '18px', height: '18px', borderRadius: '50%', border: '1.5px dashed var(--border-hover)', flexShrink: 0, opacity: 0.5 }} />
          <input
            autoFocus
            value={addText}
            onChange={(e) => setAddText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); submitAdd(); }
              else if (e.key === 'Escape') { e.preventDefault(); closeAdd(); }
            }}
            onBlur={() => { if (!addText.trim()) closeAdd(); }}
            placeholder="New item…"
            style={{ flex: 1, minWidth: 0, background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontFamily: 'var(--sans)', fontSize: '0.92rem', padding: '6px 10px' }}
          />
        </div>
      )}
      {hasChildren && !collapsed && (
        <div>
          {item.children.map((child, k) => (
            <TodoItem key={k} item={child} onToggle={onToggle} onInsertAfter={onInsertAfter} addingKey={addingKey} setAddingKey={setAddingKey} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function TodoPage() {
  const [todo, setTodo] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);

  // Which per-row "+" inline add-input is open (keyed by item text). Lifted so
  // opening one closes any other, and clicking a second "+" switches to it.
  const [addingKey, setAddingKey] = useState(null);

  // Quick-add task (#108): reveal a small input that appends to "## Today".
  const [quickAddOpen, setQuickAddOpen] = useState(false);
  const [quickAddText, setQuickAddText] = useState('');
  const [quickAddSaving, setQuickAddSaving] = useState(false);
  const quickAddInputRef = useRef(null);
  const editTextareaRef = useRef(null);

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

  const getTodoContent = useCallback(() => todo?.content, [todo]);
  const setTodoContent = useCallback((newContent) => {
    setTodo(prev => prev ? { ...prev, content: newContent } : prev);
    setEditContent(newContent);
  }, []);
  const saveTodoContent = useCallback((newContent) => api.patch('/todo', { content: newContent }), []);
  const checkboxToggle = useCheckboxToggle(getTodoContent, setTodoContent, saveTodoContent);
  const taskInsert = useTaskInsert(getTodoContent, setTodoContent, saveTodoContent);

  const handleToggle = (item) => {
    checkboxToggle(item.text, item.checked);
  };
  const handleInsertAfter = (item, text) => {
    taskInsert(item.text, text);
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

  const handleQuickAdd = async () => {
    const task = quickAddText.trim();
    if (!task || quickAddSaving || !todo) return;
    const prevContent = todo.content;
    const newContent = appendItemToSection(prevContent, 'Today', task, { createAtStart: true });
    setQuickAddSaving(true);
    // Optimistic update so the new task appears immediately.
    setTodo(prev => prev ? { ...prev, content: newContent } : prev);
    setEditContent(newContent);
    setQuickAddText('');
    try {
      const res = await api.patch('/todo', { content: newContent });
      if (res.data?.todo) setTodo(res.data.todo);
      // Keep the input open and focused for rapid entry of multiple tasks.
      if (quickAddInputRef.current) quickAddInputRef.current.focus();
    } catch (err) {
      console.error('Failed to add task:', err);
      // Revert optimistic update on failure.
      setTodo(prev => prev ? { ...prev, content: prevContent } : prev);
      setEditContent(prevContent);
      setQuickAddText(task);
    }
    setQuickAddSaving(false);
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

  // Cmd+Return / Ctrl+Enter primary-submit (#129): save the edit textarea,
  // or add the quick-add task. Plain Enter still inserts a newline in the
  // textarea; the quick-add input handles plain Enter itself.
  useSubmitShortcut(editTextareaRef, () => handleSave(), editing && !saving && !!editContent.trim());
  useSubmitShortcut(quickAddInputRef, () => handleQuickAdd(), quickAddOpen && !quickAddSaving && !!quickAddText.trim());

  const generatedByLabel = (g) => {
    if (g === 'user' || g === 'manual') return 'edited manually';
    if (g === 'orient_session') return 'Orient session';
    if (g === 'voice_session') return 'Voice';
    if (g === 'revert') return 'reverted';
    if (g === 'import') return 'imported';
    return g;
  };

  const sections = todo ? parseTodoSections(todo.content) : [];

  if (loading) {
    return (
      <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
        <ArtifactsNav />
        <p style={{ color: 'var(--text-muted)' }}>Loading...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
      <ArtifactsNav />
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
                background: 'var(--success)', display: 'inline-block',
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
            {/* Quick-add task (#108) */}
            {!editing && (
              <button
                onClick={() => {
                  setQuickAddOpen(v => !v);
                  // Focus the input on the next tick once it's mounted.
                  setTimeout(() => {
                    if (quickAddInputRef.current) quickAddInputRef.current.focus();
                  }, 0);
                }}
                aria-label="Quick-add task"
                title="Quick-add task to Today"
                style={{
                  background: 'none',
                  border: '1px solid var(--border)',
                  borderRadius: '50%',
                  width: '24px', height: '24px',
                  cursor: 'pointer', padding: 0,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontFamily: 'var(--sans)', fontSize: '1rem', fontWeight: 300,
                  lineHeight: 1,
                  color: quickAddOpen ? 'var(--accent)' : 'var(--text-muted)',
                  borderColor: quickAddOpen ? 'var(--accent)' : 'var(--border)',
                  transition: 'all 0.2s ease',
                }}
              >
                {quickAddOpen ? '×' : '+'}
              </button>
            )}
          </div>
        )}
      </div>

      {/* Quick-add input row (#108) */}
      {todo && quickAddOpen && !editing && (
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '8px' }}>
          <input
            ref={quickAddInputRef}
            value={quickAddText}
            onChange={(e) => setQuickAddText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey) {
                e.preventDefault();
                handleQuickAdd();
              } else if (e.key === 'Escape') {
                setQuickAddOpen(false);
                setQuickAddText('');
              }
            }}
            placeholder="Add a task to Today and press Enter"
            disabled={quickAddSaving}
            style={{
              flex: 1,
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: '6px',
              color: 'var(--text-primary)',
              fontFamily: 'var(--sans)',
              fontSize: '0.85rem',
              fontWeight: 300,
              padding: '8px 12px',
            }}
          />
        </div>
      )}

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
            ref={editTextareaRef}
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
                  {countAllItems(section.items)}
                </span>
              </div>
              {section.items.map((item, j) => (
                <TodoItem key={j} item={item} onToggle={handleToggle} onInsertAfter={handleInsertAfter} addingKey={addingKey} setAddingKey={setAddingKey} />
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
