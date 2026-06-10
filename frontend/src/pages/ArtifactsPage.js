import React, { useState, useEffect, useCallback } from 'react';
import MarkdownBody from '../components/MarkdownBody';
import api from '../api';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';
import { formatDate } from '../utils/date';

const KIND_BLURBS = {
  memory: "Durable facts the AI remembers about you across sessions. It updates this on its own as you talk.",
  scratchpad: "The AI's working notes for ongoing threads — where it left off, open questions.",
  intentions: "Your longer-running aspirations — noticed, clarified, and tracked together with the AI. Fulfilled or consciously released, both count.",
};

export default function ArtifactsPage() {
  const [artifacts, setArtifacts] = useState([]);
  const [activeKind, setActiveKind] = useState('memory');
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [creatingKind, setCreatingKind] = useState(false);
  const [newKind, setNewKind] = useState('');

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);

  const fetchArtifacts = useCallback(async () => {
    try {
      const res = await api.get('/artifacts');
      setArtifacts(res.data.artifacts);
      setLoading(false);
    } catch (err) {
      console.error('Failed to load artifacts:', err);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchArtifacts();
  }, [fetchArtifacts]);

  const active = artifacts.find((a) => a.kind === activeKind) || null;

  const handleSave = async (kind) => {
    setSaving(true);
    try {
      await api.put(`/artifacts/${kind}`, {
        content: editContent,
        generated_by: 'user',
      });
      await fetchArtifacts();
      setEditing(false);
      setCreatingKind(false);
      setActiveKind(kind);
    } catch (err) {
      console.error('Failed to save artifact:', err);
    }
    setSaving(false);
  };

  const handleOpenHistory = async () => {
    setDrawerOpen(true);
    try {
      const res = await api.get(`/artifacts/${activeKind}/versions`);
      setVersions(res.data.versions);
    } catch (err) {
      console.error('Failed to load versions:', err);
    }
  };

  const handleSelectVersion = async (id) => {
    setSelectedVersionId(id);
    setVersionContent(null);
    try {
      const res = await api.get(`/artifacts/versions/${id}`);
      setVersionContent(res.data.artifact.content);
    } catch (err) {
      console.error('Failed to load version:', err);
    }
  };

  const generatedByLabel = (g) => {
    if (!g) return null;
    if (g === 'user' || g === 'manual') return 'edited manually';
    if (g === 'agentic_session') return 'AI session';
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
      <h1 style={{
        fontFamily: 'var(--serif)',
        fontSize: '2rem',
        fontWeight: 300,
        color: 'var(--text-primary)',
        margin: '0 0 16px 0',
      }}>
        Artifacts
      </h1>

      {/* Kind tabs */}
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '20px' }}>
        {artifacts.map((a) => (
          <button
            key={a.kind}
            onClick={() => { setActiveKind(a.kind); setEditing(false); }}
            style={{
              padding: '6px 14px',
              background: a.kind === activeKind ? 'var(--bg-card)' : 'none',
              border: '1px solid',
              borderColor: a.kind === activeKind ? 'var(--accent)' : 'var(--border)',
              borderRadius: '16px',
              color: a.kind === activeKind ? 'var(--text-primary)' : 'var(--text-muted)',
              fontFamily: 'var(--sans)',
              fontSize: '0.8rem',
              fontWeight: 300,
              cursor: 'pointer',
            }}
          >
            {a.title}
          </button>
        ))}
        <button
          onClick={() => { setCreatingKind(true); setNewKind(''); setEditContent(''); setEditing(true); }}
          title="Create a new artifact"
          style={{
            padding: '6px 14px',
            background: 'none',
            border: '1px dashed var(--border)',
            borderRadius: '16px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.8rem',
            cursor: 'pointer',
          }}
        >
          +
        </button>
      </div>

      {/* Meta line */}
      {active && !creatingKind && (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px', marginBottom: '16px', flexWrap: 'wrap' }}>
          <p style={{
            fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
            color: 'var(--text-muted)', margin: 0, opacity: 0.7,
          }}>
            {KIND_BLURBS[active.kind] || 'A persistent document shared between you and the AI.'}
            {active.created_at && (
              <> &middot; last updated by {generatedByLabel(active.generated_by)} &middot; {formatDate(active.created_at)}</>
            )}
          </p>
          {active.created_at && (
            <button
              onClick={handleOpenHistory}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--text-muted)', opacity: 0.7, textDecoration: 'underline',
              }}
            >
              history
            </button>
          )}
          {!editing && (
            <button
              onClick={() => { setEditing(true); setEditContent(active.content || ''); }}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--accent)', textDecoration: 'underline',
              }}
            >
              edit
            </button>
          )}
        </div>
      )}

      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* New-kind name input */}
      {creatingKind && editing && (
        <input
          value={newKind}
          onChange={(e) => setNewKind(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '-'))}
          placeholder="artifact-name (lowercase, dashes)"
          style={{
            width: '100%', marginBottom: '12px',
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            borderRadius: '8px', color: 'var(--text-primary)',
            fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
            padding: '10px 16px',
          }}
        />
      )}

      {/* Editing mode */}
      {editing && (
        <div>
          <textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            placeholder={activeKind === 'memory' && !creatingKind
              ? 'Facts the AI should remember about you...'
              : 'Artifact content (markdown)...'}
            style={{
              width: '100%', minHeight: '300px',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              borderRadius: '8px', color: 'var(--text-primary)',
              fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
              padding: '16px', lineHeight: 1.6, resize: 'vertical',
            }}
          />
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px' }}>
            <button
              onClick={() => handleSave(creatingKind ? newKind : activeKind)}
              disabled={saving || (creatingKind && !newKind)}
              style={{
                padding: '8px 20px', background: 'var(--accent)', border: 'none',
                borderRadius: '6px', color: 'var(--bg-deep)',
                fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
                cursor: saving ? 'not-allowed' : 'pointer', opacity: saving ? 0.6 : 1,
              }}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
            <button
              onClick={() => { setEditing(false); setCreatingKind(false); }}
              style={{
                padding: '8px 20px', background: 'none',
                border: '1px solid var(--border)', borderRadius: '6px',
                color: 'var(--text-muted)', fontFamily: 'var(--sans)',
                fontSize: '0.85rem', cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Rendered content */}
      {active && !editing && (
        active.content ? (
          <div className="loore-profile" style={{
            fontFamily: 'var(--sans)', fontSize: '0.92rem', fontWeight: 300,
            color: 'var(--text-secondary)', lineHeight: 1.7,
          }}>
            <MarkdownBody>{active.content}</MarkdownBody>
          </div>
        ) : (
          <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>
            Nothing here yet. The AI fills this in during Voice and Text sessions — or click edit to start it yourself.
          </p>
        )
      )}

      <VersionHistoryDrawer
        isOpen={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedVersionId(null); setVersionContent(null); }}
        title={`${active ? active.title : ''} History`}
        versions={versions}
        selectedVersionId={selectedVersionId}
        onSelectVersion={handleSelectVersion}
        versionContent={versionContent}
      />
    </div>
  );
}
