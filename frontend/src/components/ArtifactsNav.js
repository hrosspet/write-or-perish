import React, { useState, useEffect, useCallback } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import api from '../api';
import { BUILTIN_KIND_ORDER, isBuiltinKind } from '../utils/artifactKinds';

// Shared navigator row for the "documents workspace" — rendered identically
// on the Profile, Todo, and Artifacts pages so they cross-link. Bubbles, in
// order: Profile · Intentions · Todo · remaining built-in artifacts
// (canonical order) · custom artifacts (alphabetical) · {children} (e.g. the
// Artifacts page's + button).
// Module-level cache of the last-fetched artifact list. Lets the bubble row
// render instantly when ArtifactsNav remounts on cross-page navigation
// (Profile/Todo/Artifacts are separate routes) instead of flashing empty
// while it re-fetches. Refreshed in the background on every mount + on the
// loore_artifacts_changed event.
let _artifactsCache = [];

const bubbleStyle = (active) => ({
  display: 'inline-flex',
  alignItems: 'center',
  padding: '6px 14px',
  background: active ? 'var(--bg-card)' : 'none',
  border: '1px solid',
  borderColor: active ? 'var(--accent)' : 'var(--border)',
  borderRadius: '16px',
  color: active ? 'var(--text-primary)' : 'var(--text-muted)',
  fontFamily: 'var(--sans)',
  fontSize: '0.8rem',
  fontWeight: 300,
  textDecoration: 'none',
  cursor: 'pointer',
});

// Let cmd/ctrl/shift/alt-click (and native middle-click on the <a>) fall
// through to the browser so bubbles open in a new tab/window; intercept only
// plain left-clicks for in-app SPA navigation (which also respects the host
// page's unsaved-edits guard via `go`).
const isModifiedClick = (e) =>
  e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1;

export default function ArtifactsNav({ activeKind, onNavigate, creating, children }) {
  const location = useLocation();
  const navigate = useNavigate();
  const path = location.pathname;
  // onNavigate lets a host page intercept bubble clicks (e.g. the Artifacts
  // page guarding unsaved edits). Falls back to direct navigation.
  const go = (to) => (onNavigate ? onNavigate(to) : navigate(to));
  const [artifacts, setArtifacts] = useState(_artifactsCache);

  const fetchArtifacts = useCallback(async () => {
    try {
      const res = await api.get('/artifacts');
      _artifactsCache = res.data.artifacts || [];
      setArtifacts(_artifactsCache);
    } catch (err) {
      // Profile/Todo bubbles still render; the artifact list just stays empty.
      console.error('ArtifactsNav: failed to load artifacts', err);
    }
  }, []);

  useEffect(() => { fetchArtifacts(); }, [fetchArtifacts]);

  // Refetch when an artifact is created/updated elsewhere so the bubble row
  // updates without a reload. Dispatched by ArtifactsPage.handleSave.
  useEffect(() => {
    const handler = () => fetchArtifacts();
    window.addEventListener('loore_artifacts_changed', handler);
    return () => window.removeEventListener('loore_artifacts_changed', handler);
  }, [fetchArtifacts]);

  const byKind = Object.fromEntries(artifacts.map((a) => [a.kind, a]));
  // Intentions renders between Profile and Todo (aspirations before tasks);
  // the rest of the built-ins follow Todo in canonical order.
  const intentionsArtifact = byKind['intentions'];
  const pinned = BUILTIN_KIND_ORDER
    .filter((k) => k !== 'intentions' && byKind[k])
    .map((k) => byKind[k]);
  const custom = artifacts
    .filter((a) => !isBuiltinKind(a.kind))
    .slice()
    .sort((a, b) => (a.title || a.kind).localeCompare(b.title || b.kind));

  const onBubbleClick = (e, to) => {
    if (isModifiedClick(e)) return; // browser handles new-tab/window
    e.preventDefault();
    go(to);
  };

  const navBubble = (label, to) => (
    <a
      key={to}
      href={to}
      onClick={(e) => onBubbleClick(e, to)}
      style={bubbleStyle(path === to)}
    >
      {label}
    </a>
  );

  const artBubble = (a) => {
    const to = `/artifacts/${a.kind}`;
    // While creating a new artifact the "+" is the active item, so no
    // existing artifact bubble should highlight.
    const active = !creating
      && (path === to || (!!activeKind && a.kind === activeKind));
    return (
      <a
        key={a.kind}
        href={to}
        onClick={(e) => onBubbleClick(e, to)}
        title={a.description || undefined}
        style={bubbleStyle(active)}
      >
        {a.title || a.kind}
      </a>
    );
  };

  return (
    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '20px' }}>
      {navBubble('Profile', '/profile')}
      {intentionsArtifact && artBubble(intentionsArtifact)}
      {navBubble('Todo', '/todo')}
      {pinned.map(artBubble)}
      {custom.map(artBubble)}
      <a
        href="/artifacts?create=1"
        onClick={(e) => onBubbleClick(e, '/artifacts?create=1')}
        title="Create a new artifact"
        style={{
          display: 'inline-flex', alignItems: 'center',
          padding: '6px 14px',
          background: creating ? 'var(--bg-card)' : 'none',
          border: '1px solid',
          borderColor: creating ? 'var(--accent)' : 'var(--border)',
          borderRadius: '16px',
          color: creating ? 'var(--text-primary)' : 'var(--text-muted)',
          fontFamily: 'var(--sans)', fontSize: '0.8rem',
          textDecoration: 'none', cursor: 'pointer',
        }}
      >
        +
      </a>
      {children}
    </div>
  );
}
