import React, { useState, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import api from '../api';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';

export default function AiPreferencesPage() {
  const [prefs, setPrefs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);

  const fetchPrefs = useCallback(async () => {
    try {
      const res = await api.get('/ai-preferences');
      setPrefs(res.data.ai_preferences);
      if (res.data.ai_preferences) {
        setEditContent(res.data.ai_preferences.content);
      }
      setLoading(false);
    } catch (err) {
      console.error('Failed to load AI preferences:', err);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPrefs();
  }, [fetchPrefs]);

  const handleSave = async () => {
    if (!editContent.trim()) return;
    setSaving(true);
    try {
      const res = await api.put('/ai-preferences', {
        content: editContent,
        generated_by: 'user',
      });
      setPrefs(res.data.ai_preferences);
      setEditing(false);
    } catch (err) {
      console.error('Failed to save AI preferences:', err);
    }
    setSaving(false);
  };

  const handleCreate = () => {
    setEditContent('');
    setEditing(true);
  };

  const handleOpenHistory = async () => {
    setDrawerOpen(true);
    try {
      const res = await api.get('/ai-preferences/versions');
      setVersions(res.data.versions);
    } catch (err) {
      console.error('Failed to load versions:', err);
    }
  };

  const handleSelectVersion = async (id) => {
    setSelectedVersionId(id);
    setVersionContent(null);
    try {
      const res = await api.get(`/ai-preferences/versions/${id}`);
      setVersionContent(res.data.ai_preferences.content);
    } catch (err) {
      console.error('Failed to load version:', err);
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
    if (g === 'voice_session') return 'Voice session';
    return g;
  };

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
          AI Preferences
        </h1>

        {prefs && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <button
              onClick={() => {
                if (editing) { handleSave(); } else { setEditing(true); setEditContent(prefs.content); }
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
              v{prefs.version_number} &middot; {formatDate(prefs.created_at)}
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
      {prefs && (
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.75rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          margin: '0 0 16px 0',
          opacity: 0.7,
        }}>
          Last updated by {generatedByLabel(prefs.generated_by)} &middot; {formatDate(prefs.created_at)}
        </p>
      )}

      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* No preferences yet */}
      {!prefs && !editing && (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem', marginBottom: '16px' }}>
            No AI preferences yet. They'll be created automatically during Voice sessions, or you can write them manually.
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
            Create Preferences
          </button>
        </div>
      )}

      {/* Editing mode */}
      {editing && (
        <div>
          <textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            placeholder="How should AI interact with you? Tone, style, boundaries, topics to avoid..."
            style={{
              width: '100%',
              minHeight: '300px',
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
              onClick={() => { setEditing(false); if (prefs) setEditContent(prefs.content); }}
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

      {/* Rendered content */}
      {prefs && !editing && (
        <div className="loore-profile" style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.92rem',
          fontWeight: 300,
          color: 'var(--text-secondary)',
          lineHeight: 1.7,
        }}>
          <ReactMarkdown>{prefs.content}</ReactMarkdown>
        </div>
      )}

      {/* Version History Drawer */}
      <VersionHistoryDrawer
        isOpen={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedVersionId(null); setVersionContent(null); }}
        title="AI Preferences History"
        versions={versions}
        selectedVersionId={selectedVersionId}
        onSelectVersion={handleSelectVersion}
        versionContent={versionContent}
      />
    </div>
  );
}
