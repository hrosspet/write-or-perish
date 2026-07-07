import React, { useState, useEffect, useCallback, useRef } from 'react';
import MarkdownBody from '../components/MarkdownBody';
import api from '../api';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';
import ArtifactsNav from '../components/ArtifactsNav';
import SpeakerIcon from '../components/SpeakerIcon';
import RegenerateTtsDialog from '../components/RegenerateTtsDialog';
import { useUser } from '../contexts/UserContext';
import useSubmitShortcut from '../hooks/useSubmitShortcut';
import useEscapeKey from '../hooks/useEscapeKey';
import { formatDate } from '../utils/date';

export default function ProfilePage() {
  const { user } = useUser();
  const [profile, setProfile] = useState(null);
  const [versionNumber, setVersionNumber] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const editTextareaRef = useRef(null);
  // Ask whether to keep/regenerate stale TTS when editing a profile that
  // has generated audio (#66).
  const [showTtsDialog, setShowTtsDialog] = useState(false);

  // Version history drawer
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);

  // Profile generation progress — read from localStorage or backend user
  const [generationTaskId, setGenerationTaskId] = useState(
    () => localStorage.getItem('loore_profile_task_id')
  );
  const [failMessage, setFailMessage] = useState('');

  // Pick up task ID from backend if localStorage doesn't have it (cross-browser)
  useEffect(() => {
    if (!generationTaskId && user && user.profile_generation_task_id) {
      const backendTaskId = user.profile_generation_task_id;
      localStorage.setItem('loore_profile_task_id', backendTaskId);
      setGenerationTaskId(backendTaskId);
    }
  }, [user, generationTaskId]);

  // Listen for generation started from NavBar (handles already-mounted case)
  useEffect(() => {
    const handler = (e) => {
      const taskId = e.detail?.taskId;
      if (taskId) setGenerationTaskId(taskId);
    };
    window.addEventListener('loore_profile_started', handler);
    return () => window.removeEventListener('loore_profile_started', handler);
  }, []);

  // Progress + completion arrive from the app-wide
  // ProfileGenerationWatcher (#131) — this page no longer polls.
  const [genProgress, setGenProgress] = useState(0);
  const [genData, setGenData] = useState(null);

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
    const onProgress = (e) => {
      setGenProgress(e.detail?.progress || 0);
      setGenData(e.detail?.message ? { message: e.detail.message } : null);
      if (e.detail?.status === 'failed') {
        setFailMessage('Generation failed');
        setTimeout(() => setFailMessage(''), 5000);
      }
    };
    const onDone = () => {
      setGenerationTaskId(null);
      setGenProgress(0);
      setGenData(null);
      fetchProfile();
    };
    window.addEventListener('loore_profile_progress', onProgress);
    window.addEventListener('loore_profile_done', onDone);
    return () => {
      window.removeEventListener('loore_profile_progress', onProgress);
      window.removeEventListener('loore_profile_done', onDone);
    };
  }, [fetchProfile]);

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

  const handleSave = async (regenerateTts) => {
    if (!editContent.trim()) return;

    // Editing a profile that has generated TTS and whose text changed:
    // ask whether to keep or regenerate the now-stale audio first (#66).
    if (
      profile && profile.has_tts && regenerateTts === undefined
      && editContent !== profile.content
    ) {
      setShowTtsDialog(true);
      return;
    }

    setSaving(true);
    try {
      if (profile) {
        await api.put(`/profile/${profile.id}`, {
          content: editContent,
          ...(regenerateTts && { regenerate_tts: true }),
        });
      } else {
        await api.post('/profile', { content: editContent });
      }
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

  // Cmd+Return / Ctrl+Enter saves the profile while editing (#129).
  useSubmitShortcut(editTextareaRef, () => handleSave(), editing && !saving && !!editContent.trim());
  // Esc cancels the edit (matches the Cancel button).
  useEscapeKey(() => setEditing(false), editing && !saving);

  if (loading) {
    return (
      <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
        <ArtifactsNav />
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
          Profile
        </h1>

        {profile && (
          <SpeakerIcon
            profileId={profile.id}
            content={profile.content}
            isPublic={profile.privacy_level === 'public'}
            aiUsage={profile.ai_usage}
            onTtsGenerated={() => setProfile(prev => prev ? { ...prev, has_tts: true } : prev)}
          />
        )}

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
                background: 'var(--success)', display: 'inline-block',
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

        {/* Generation progress indicator */}
        {(generationTaskId || failMessage) && (
          <span style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
            fontWeight: 300,
            color: failMessage ? 'var(--text-muted)' : 'var(--accent)',
            animation: generationTaskId ? 'pulse 2s ease-in-out infinite' : 'none',
          }}>
            {failMessage || (genData?.message
              ? `${genData.message} \u00b7 ${genProgress || 0}%`
              : 'Starting generation...')}
          </span>
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
          {profile.source_tokens_used
            ? `Built from ~${profile.source_tokens_used.toLocaleString()} tokens of writing`
            : `Generated from ${profile.tokens_used?.toLocaleString() || 0} tokens`}
          {' '}&middot; {profile.generated_by}
          {profile.source_data_cutoff && (
            <> &middot; Data through {formatDate(profile.source_data_cutoff, { relative: false })}</>
          )}
        </p>
      )}

      {/* Accent divider */}
      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* Content — the empty state doubles as the first-visit explainer for
          the artifacts workspace (the top-nav "Artifacts" link lands here). */}
      {!profile && !editing && (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <p style={{
            color: 'var(--text-secondary)',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            lineHeight: 1.7,
            maxWidth: '38em',
            margin: '0 auto 16px',
          }}>
            Your profile is a living document the AI writes about you as you
            use Loore — what you're working on, what you care about, how
            you've changed.
          </p>
          <p style={{
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            lineHeight: 1.7,
            maxWidth: '38em',
            margin: '0 auto 16px',
          }}>
            It's the first of your artifacts — the row above: documents you
            and the AI keep together. Todo and Intentions hold what you mean
            to do; Memory holds what the AI has learned. They start empty and
            fill in as you write and talk.
          </p>
          <p style={{
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            marginBottom: '20px',
          }}>
            There's nothing to set up. Start writing, and this page will
            follow — or write the first version yourself.
          </p>
          <button
            onClick={() => { setEditing(true); setEditContent(''); }}
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
            Write Profile
          </button>
        </div>
      )}

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
              onClick={() => handleSave()}
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
          <MarkdownBody>{profile.content}</MarkdownBody>
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
      <RegenerateTtsDialog
        open={showTtsDialog}
        onClose={() => setShowTtsDialog(false)}
        onChoice={(regenerate) => {
          setShowTtsDialog(false);
          handleSave(regenerate);
        }}
      />
    </div>
  );
}
