import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import MarkdownBody from '../components/MarkdownBody';
import IntentionsView from '../components/IntentionsView';
import api from '../api';
import ArtifactsNav from '../components/ArtifactsNav';
import VersionHistoryDrawer from '../components/VersionHistoryDrawer';
import useSubmitShortcut from '../hooks/useSubmitShortcut';
import useEscapeKey from '../hooks/useEscapeKey';
import { compareArtifacts } from '../utils/artifactKinds';
import { formatDate } from '../utils/date';

const KIND_BLURBS = {
  memory: "Durable facts the AI remembers about you across sessions. It updates this on its own as you talk.",
  scratchpad: "The AI's working notes for ongoing threads — where it left off, open questions.",
  intentions: "Your longer-running aspirations — noticed, clarified, and tracked together with the AI. Fulfilled or consciously released, both count.",
};

// Longer intro shown in a kind's empty state (above the generic "Nothing
// here yet"). Distilled from the agentic prompt's description of the kind.
const KIND_EMPTY_INTROS = {
  intentions: "Intentions are your longer-running aspirations — the communication layer between your conscious goals and your subconscious orientation: directional rather than specific, quietly shaping which opportunities you notice and reach for.",
  ai_preferences: "AI Interaction Preferences are your standing notes on how the AI should work with you — tone, style, boundaries, topics to leave alone — written by you or noted by the AI when you express one, and honored in every conversation.",
  external_digest: "The Saved References Digest is a compact topic map of the tweets and bookmarks you saved elsewhere and imported into Loore — the AI reads it to judge whether your references hold something relevant before searching them, and it rebuilds itself after each import.",
};

const titleFromKind = (k) =>
  k.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

export default function ArtifactsPage() {
  const { kind: kindParam } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [artifacts, setArtifacts] = useState([]);
  const [activeKind, setActiveKind] = useState(kindParam || 'memory');
  const [versionNumber, setVersionNumber] = useState(null);
  const [pendingNav, setPendingNav] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [saving, setSaving] = useState(false);
  const [creatingKind, setCreatingKind] = useState(false);
  const [newKind, setNewKind] = useState('');

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState(null);
  const [versionContent, setVersionContent] = useState(null);
  const [previousVersionContent, setPreviousVersionContent] = useState(null);

  const editTextareaRef = useRef(null);
  const editDescRef = useRef(null);
  const newKindRef = useRef(null);

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

  // Deep-link: /artifacts/:kind selects that artifact (e.g. predictions, or
  // a custom one opened from the nav dropdown). Switching kinds always
  // leaves edit mode so one artifact's draft can't leak onto another.
  useEffect(() => {
    if (kindParam) {
      setActiveKind(kindParam);
      setEditing(false);
      setCreatingKind(false);
    }
  }, [kindParam]);

  // The nav "+" (on any page) routes here with ?create=1 — open the inline
  // create form, then clear the flag.
  useEffect(() => {
    if (searchParams.get('create') === '1') {
      setCreatingKind(true);
      setNewKind('');
      setEditContent('');
      setEditDescription('');
      setEditing(true);
      const next = new URLSearchParams(searchParams);
      next.delete('create');
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Version count for the active artifact's header badge (only when it has
  // content). Refetched on kind switch and after a save (artifacts reload).
  useEffect(() => {
    const a = artifacts.find((x) => x.kind === activeKind);
    if (!a || !a.created_at) { setVersionNumber(null); return; }
    api.get(`/artifacts/${activeKind}/versions`)
      .then((res) => setVersionNumber(res.data.versions.length))
      .catch(() => setVersionNumber(null));
  }, [activeKind, artifacts]);

  // Surface a kind that has no row yet (not a default, not created) as an
  // empty, editable artifact so deep links never land on a blank page.
  const synthetic = !artifacts.some((a) => a.kind === activeKind)
    ? {
        id: null, kind: activeKind, title: titleFromKind(activeKind),
        content: '', generated_by: null, created_at: null,
        privacy_level: 'private', ai_usage: 'chat',
      }
    : null;
  // Built-in kinds first (canonical order), then custom kinds alphabetically
  // — same ordering as the nav dropdown.
  const tabArtifacts = (synthetic ? [...artifacts, synthetic] : artifacts)
    .slice()
    .sort(compareArtifacts);
  const active = tabArtifacts.find((a) => a.kind === activeKind) || null;

  // Unsaved-changes guard: block in-app navigation (bubble clicks, top nav,
  // back) while the edit UI holds changes, so edits aren't silently lost or
  // misattributed to whatever the user navigates to.
  const dirty = editing && (
    creatingKind
      ? !!(newKind || editContent.trim() || editDescription.trim())
      : (editContent !== (active?.content || '')
         || editDescription !== (active?.description || ''))
  );
  // The app uses a component <BrowserRouter>, so router-level useBlocker
  // isn't available. Guard the ArtifactsNav bubbles directly (the reported
  // vector) and use beforeunload for tab close — same pattern as
  // useVoiceSession. Switching kinds also resets edit state (see the
  // kindParam effect), so a draft can never be saved to the wrong artifact.
  const handleNavGuard = (to) => {
    if (dirty) setPendingNav(to);
    else navigate(to);
  };

  // Warn on tab close / reload while editing with unsaved changes.
  useEffect(() => {
    if (!dirty) return undefined;
    const handler = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  const handleSave = async (kind) => {
    setSaving(true);
    try {
      await api.put(`/artifacts/${kind}`, {
        content: editContent,
        description: editDescription.trim(),
        generated_by: 'user',
      });
      await fetchArtifacts();
      window.dispatchEvent(new CustomEvent('loore_artifacts_changed'));
      setEditing(false);
      setCreatingKind(false);
      setActiveKind(kind);
      // Keep the URL in sync with the active artifact (a fresh create came
      // from a different kind's URL) so only one bubble highlights.
      navigate(`/artifacts/${kind}`);
    } catch (err) {
      console.error('Failed to save artifact:', err);
    }
    setSaving(false);
  };

  // Cmd+Return / Ctrl+Enter saves while editing (#129), from any of the edit
  // fields. Matches the Save button's enabled state: a description is always
  // required, and a new kind also needs a name (content stays optional).
  const saveEnabled = editing && !saving
    && !(creatingKind && !newKind) && !!editDescription.trim();
  const saveShortcut = () => handleSave(creatingKind ? newKind : activeKind);
  useSubmitShortcut(newKindRef, saveShortcut, saveEnabled);
  useSubmitShortcut(editDescRef, saveShortcut, saveEnabled);
  useSubmitShortcut(editTextareaRef, saveShortcut, saveEnabled);
  // Esc cancels the edit/create form (matches the Cancel button at l.343).
  useEscapeKey(() => { setEditing(false); setCreatingKind(false); }, editing && !saving);

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
    setPreviousVersionContent(null);
    try {
      // versions is newest-first, so the next entry is the previous
      // version — fetched alongside so the drawer can render a diff.
      const idx = versions.findIndex(v => v.id === id);
      const prev = idx >= 0 ? versions[idx + 1] : null;
      const [res, prevRes] = await Promise.all([
        api.get(`/artifacts/versions/${id}`),
        prev ? api.get(`/artifacts/versions/${prev.id}`) : Promise.resolve(null),
      ]);
      setVersionContent(res.data.artifact.content);
      if (prevRes) setPreviousVersionContent(prevRes.data.artifact.content);
    } catch (err) {
      console.error('Failed to load version:', err);
    }
  };

  const handleRevert = async (id) => {
    try {
      await api.post(`/artifacts/${activeKind}/revert/${id}`);
      await fetchArtifacts();
      window.dispatchEvent(new CustomEvent('loore_artifacts_changed'));
      setDrawerOpen(false);
      setSelectedVersionId(null);
      setVersionContent(null);
      setPreviousVersionContent(null);
    } catch (err) {
      console.error('Failed to revert:', err);
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
        <ArtifactsNav activeKind={activeKind} onNavigate={handleNavGuard} creating={creatingKind} />
      </div>
    );
  }

  return (
    <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
      <ArtifactsNav activeKind={activeKind} onNavigate={handleNavGuard} creating={creatingKind} />

      {/* Header: artifact title + version (click -> edit) + date + history */}
      <div style={{ marginBottom: '8px', display: 'flex', alignItems: 'baseline', gap: '16px', flexWrap: 'wrap' }}>
        <h1 style={{
          fontFamily: 'var(--serif)', fontSize: '2rem', fontWeight: 300,
          color: 'var(--text-primary)', margin: 0,
        }}>
          {creatingKind ? 'New artifact' : (active ? active.title : 'Artifacts')}
        </h1>

        {active && !creatingKind && active.created_at && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <button
              onClick={() => {
                if (!editing) {
                  setEditing(true);
                  setEditContent(active.content || '');
                  setEditDescription(active.description || '');
                }
              }}
              title="Edit"
              style={{
                background: 'none', border: 'none',
                cursor: editing ? 'default' : 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
                color: editing ? 'var(--accent)' : 'var(--text-muted)',
                display: 'flex', alignItems: 'center', gap: '6px',
              }}
            >
              <span style={{
                width: '6px', height: '6px', borderRadius: '50%',
                background: 'var(--success)', display: 'inline-block',
              }} />
              v{versionNumber || 1}
            </button>
            <span style={{
              fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
              color: 'var(--text-muted)', opacity: 0.7,
            }}>
              {formatDate(active.created_at)}
            </span>
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
          </div>
        )}

      </div>

      {/* Subtitle: description + provenance */}
      {active && !creatingKind && (
        <p style={{
          fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
          color: 'var(--text-muted)', margin: '0 0 16px 0', opacity: 0.7,
        }}>
          {active.description || KIND_BLURBS[active.kind] || 'A persistent document shared between you and the AI.'}
          {active.created_at && (
            <> &middot; last updated by {generatedByLabel(active.generated_by)}</>
          )}
        </p>
      )}

      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, marginBottom: '24px' }} />

      {/* New-kind name input */}
      {creatingKind && editing && (
        <input
          ref={newKindRef}
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
          <input
            ref={editDescRef}
            value={editDescription}
            onChange={(e) => setEditDescription(e.target.value)}
            placeholder="One-line description — what this artifact is for"
            style={{
              width: '100%', marginBottom: '12px',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              borderRadius: '8px', color: 'var(--text-primary)',
              fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
              padding: '10px 16px',
            }}
          />
          <textarea
            ref={editTextareaRef}
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
              disabled={!saveEnabled}
              style={{
                padding: '8px 20px', background: 'var(--accent)', border: 'none',
                borderRadius: '6px', color: 'var(--bg-deep)',
                fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
                cursor: !saveEnabled ? 'not-allowed' : 'pointer', opacity: !saveEnabled ? 0.6 : 1,
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
            {active.kind === 'intentions'
              ? <IntentionsView content={active.content} />
              : <MarkdownBody>{active.content}</MarkdownBody>}
          </div>
        ) : (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            {KIND_EMPTY_INTROS[active.kind] && (
              <p style={{
                color: 'var(--text-secondary)', fontFamily: 'var(--sans)',
                fontSize: '0.9rem', fontWeight: 300, lineHeight: 1.7,
                maxWidth: '38em', margin: '0 auto 16px',
              }}>
                {KIND_EMPTY_INTROS[active.kind]}
              </p>
            )}
            <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem', marginBottom: '16px' }}>
              Nothing here yet. The AI fills this in during Voice and Text sessions,
              <br />or write your own.
            </p>
            <button
              onClick={() => { setEditing(true); setEditContent(''); setEditDescription(active.description || ''); }}
              style={{
                padding: '10px 24px', background: 'var(--accent)', border: 'none',
                borderRadius: '6px', color: 'var(--bg-deep)',
                fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
                cursor: 'pointer',
              }}
            >
              Write {active.title}
            </button>
          </div>
        )
      )}

      {pendingNav && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          zIndex: 2000, padding: '24px',
        }}>
          <div style={{
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '12px', padding: '24px', width: 'min(420px, 92vw)',
            boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          }}>
            <h2 style={{
              fontFamily: 'var(--serif)', fontWeight: 300, fontSize: '1.3rem',
              color: 'var(--text-primary)', margin: '0 0 12px 0',
            }}>
              Unsaved changes
            </h2>
            <p style={{
              fontFamily: 'var(--sans)', fontSize: '0.9rem', fontWeight: 300,
              color: 'var(--text-secondary)', margin: '0 0 20px 0', lineHeight: 1.5,
            }}>
              You have unsaved edits to this artifact. Leave without saving?
            </p>
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                onClick={() => setPendingNav(null)}
                style={{
                  padding: '8px 18px', background: 'none',
                  border: '1px solid var(--border)', borderRadius: '6px',
                  color: 'var(--text-muted)', fontFamily: 'var(--sans)',
                  fontSize: '0.85rem', cursor: 'pointer',
                }}
              >
                Cancel
              </button>
              <button
                onClick={() => { setEditing(false); setCreatingKind(false); const to = pendingNav; setPendingNav(null); navigate(to); }}
                style={{
                  padding: '8px 18px', background: 'var(--accent)', border: 'none',
                  borderRadius: '6px', color: 'var(--bg-deep)',
                  fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
                  cursor: 'pointer',
                }}
              >
                Leave
              </button>
            </div>
          </div>
        </div>
      )}

      <VersionHistoryDrawer
        isOpen={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedVersionId(null); setVersionContent(null); setPreviousVersionContent(null); }}
        title={`${active ? active.title : ''} History`}
        versions={versions}
        selectedVersionId={selectedVersionId}
        onSelectVersion={handleSelectVersion}
        versionContent={versionContent}
        previousVersionContent={previousVersionContent}
        onRevert={handleRevert}
      />
    </div>
  );
}
