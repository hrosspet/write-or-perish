import React, { useState, useEffect, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import api from '../api';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';

export default function PromptDetailPage() {
  const { promptKey } = useParams();
  const [prompt, setPrompt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);

  // Version history
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);

  const fetchPrompt = useCallback(async () => {
    try {
      const res = await api.get(`/prompts/${promptKey}`);
      setPrompt(res.data.prompt);
      setEditContent(res.data.prompt.content);
      setLoading(false);
    } catch (err) {
      console.error('Failed to load prompt:', err);
      setLoading(false);
    }
  }, [promptKey]);

  useEffect(() => {
    fetchPrompt();
  }, [fetchPrompt]);

  const handleSave = async () => {
    if (!editContent.trim()) return;
    setSaving(true);
    try {
      const res = await api.put(`/prompts/${promptKey}`, { content: editContent });
      setPrompt(res.data.prompt);
      setEditContent(res.data.prompt.content);
      setEditing(false);
    } catch (err) {
      console.error('Failed to save prompt:', err);
    }
    setSaving(false);
  };

  const handleOpenHistory = async () => {
    setDrawerOpen(true);
    try {
      const res = await api.get(`/prompts/${promptKey}/versions`);
      setVersions(res.data.versions);
    } catch (err) {
      console.error('Failed to load versions:', err);
    }
  };

  const handleSelectVersion = async (id) => {
    setSelectedVersionId(id);
    setVersionContent(null);
    try {
      if (id === 'default') {
        const res = await api.get(`/prompts/${promptKey}/default`);
        setVersionContent(res.data.prompt.content);
      } else {
        const res = await api.get(`/prompts/${promptKey}/versions/${id}`);
        setVersionContent(res.data.prompt.content);
      }
    } catch (err) {
      console.error('Failed to load version:', err);
    }
  };

  const handleRevert = async (id) => {
    try {
      const endpoint = id === 'default'
        ? `/prompts/${promptKey}/revert-to-default`
        : `/prompts/${promptKey}/revert/${id}`;
      const res = await api.post(endpoint);
      setPrompt(res.data.prompt);
      setEditContent(res.data.prompt.content);
      setDrawerOpen(false);
      setSelectedVersionId(null);
      setVersionContent(null);
    } catch (err) {
      console.error('Failed to revert:', err);
    }
  };

  const formatDate = (iso) => {
    if (!iso) return 'default';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const generatedByLabel = (g) => {
    if (g === 'default') return 'default';
    if (g === 'user') return 'edited manually';
    if (g === 'revert') return 'reverted';
    return g;
  };

  if (loading) {
    return (
      <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
        <p style={{ color: 'var(--text-muted)' }}>Loading...</p>
      </div>
    );
  }

  if (!prompt) {
    return (
      <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
        <p style={{ color: 'var(--text-muted)' }}>Prompt not found.</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
      {/* Back link */}
      <Link to="/prompts" style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.75rem',
        fontWeight: 300,
        color: 'var(--text-muted)',
        textDecoration: 'none',
        opacity: 0.7,
        display: 'inline-block',
        marginBottom: '16px',
      }}>
        &larr; All prompts
      </Link>

      {/* Header */}
      <div style={{ marginBottom: '8px', display: 'flex', alignItems: 'baseline', gap: '16px', flexWrap: 'wrap' }}>
        <h1 style={{
          fontFamily: 'var(--serif)',
          fontSize: '2rem',
          fontWeight: 300,
          color: 'var(--text-primary)',
          margin: 0,
        }}>
          {prompt.title}
        </h1>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <button
            onClick={() => {
              if (editing) { handleSave(); } else { setEditing(true); setEditContent(prompt.content); }
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
            v{prompt.version_number} &middot; {formatDate(prompt.created_at)}
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
      </div>

      {/* Meta line */}
      <p style={{
        fontFamily: 'var(--sans)',
        fontSize: '0.75rem',
        fontWeight: 300,
        color: 'var(--text-muted)',
        margin: '0 0 16px 0',
        opacity: 0.7,
      }}>
        {generatedByLabel(prompt.generated_by)}
        {prompt.created_at && <> &middot; {formatDate(prompt.created_at)}</>}
      </p>

      {/* Accent divider */}
      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* Edit mode */}
      {editing && (
        <div>
          <textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            style={{
              width: '100%',
              minHeight: '500px',
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: '8px',
              color: 'var(--text-primary)',
              fontFamily: 'var(--mono, var(--sans))',
              fontSize: '0.8rem',
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
              onClick={() => { setEditing(false); setEditContent(prompt.content); }}
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

      {/* Display mode */}
      {!editing && (
        <pre style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.85rem',
          fontWeight: 300,
          color: 'var(--text-secondary)',
          lineHeight: 1.7,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          margin: 0,
        }}>
          {prompt.content}
        </pre>
      )}

      {/* Version History Drawer */}
      <VersionHistoryDrawer
        isOpen={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedVersionId(null); setVersionContent(null); }}
        title={`${prompt.title} History`}
        versions={versions}
        selectedVersionId={selectedVersionId}
        onSelectVersion={handleSelectVersion}
        versionContent={versionContent}
        onRevert={handleRevert}
      />
    </div>
  );
}
