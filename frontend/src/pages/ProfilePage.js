import React, { useState, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import api from '../api';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';

export default function ProfilePage() {
  const [profile, setProfile] = useState(null);
  const [versionNumber, setVersionNumber] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);

  // Version history drawer
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);

  const fetchProfile = useCallback(async () => {
    try {
      const res = await api.get('/dashboard');
      const latestProfile = res.data.latest_profile;
      setProfile(latestProfile);
      if (latestProfile) {
        setEditContent(latestProfile.content);
      }
      setLoading(false);
    } catch (err) {
      console.error('Failed to load profile:', err);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProfile();
  }, [fetchProfile]);

  // Fetch version count when profile loads
  useEffect(() => {
    if (profile) {
      api.get('/profile/versions').then(res => {
        setVersions(res.data.versions);
        setVersionNumber(res.data.versions.length);
      }).catch(() => {});
    }
  }, [profile]);

  const handleSave = async () => {
    if (!profile || !editContent.trim()) return;
    setSaving(true);
    try {
      await api.put(`/profile/${profile.id}`, { content: editContent });
      setEditing(false);
      await fetchProfile();
    } catch (err) {
      console.error('Failed to save profile:', err);
    }
    setSaving(false);
  };

  const handleOpenHistory = async () => {
    setDrawerOpen(true);
    try {
      const res = await api.get('/profile/versions');
      setVersions(res.data.versions);
    } catch (err) {
      console.error('Failed to load versions:', err);
    }
  };

  const handleSelectVersion = async (id) => {
    setSelectedVersionId(id);
    setVersionContent(null);
    try {
      const res = await api.get(`/profile/versions/${id}`);
      setVersionContent(res.data.profile.content);
    } catch (err) {
      console.error('Failed to load version:', err);
    }
  };

  const formatDate = (iso) => {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
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
          Profile
        </h1>

        {profile && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <button
              onClick={() => {
                if (editing) { handleSave(); } else { setEditing(true); setEditContent(profile.content); }
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
              v{versionNumber} &middot; {formatDate(profile.created_at)}
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
      {profile && (
        <p style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.75rem',
          fontWeight: 300,
          color: 'var(--text-muted)',
          margin: '0 0 16px 0',
          opacity: 0.7,
        }}>
          Generated from {profile.tokens_used?.toLocaleString() || 0} tokens &middot; {profile.generated_by}
        </p>
      )}

      {/* Accent divider */}
      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* Content */}
      {!profile && (
        <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>
          No profile generated yet. Your profile will be auto-generated as you use Loore.
        </p>
      )}

      {profile && editing && (
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
              onClick={() => setEditing(false)}
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

      {profile && !editing && (
        <div className="loore-profile" style={{
          fontFamily: 'var(--sans)',
          fontSize: '0.9rem',
          fontWeight: 300,
          color: 'var(--text-secondary)',
          lineHeight: 1.7,
        }}>
          <ReactMarkdown>{profile.content}</ReactMarkdown>
        </div>
      )}

      {/* Version History Drawer */}
      <VersionHistoryDrawer
        isOpen={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedVersionId(null); setVersionContent(null); }}
        title="Profile History"
        versions={versions}
        selectedVersionId={selectedVersionId}
        onSelectVersion={handleSelectVersion}
        versionContent={versionContent}
      />
    </div>
  );
}
