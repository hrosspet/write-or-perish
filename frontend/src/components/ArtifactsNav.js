import React, { useState, useEffect, useCallback } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import api from '../api';
import { BUILTIN_KIND_ORDER, isBuiltinKind } from '../utils/artifactKinds';

// Shared navigator row for the "documents workspace" — rendered identically
// on the Profile, Todo, and Artifacts pages so they cross-link. Bubbles, in
// order: Profile · Todo · built-in artifacts (canonical order) · custom
// artifacts (alphabetical) · {children} (e.g. the Artifacts page's + button).
const bubbleStyle = (active) => ({
  padding: '6px 14px',
  background: active ? 'var(--bg-card)' : 'none',
  border: '1px solid',
  borderColor: active ? 'var(--accent)' : 'var(--border)',
  borderRadius: '16px',
  color: active ? 'var(--text-primary)' : 'var(--text-muted)',
  fontFamily: 'var(--sans)',
  fontSize: '0.8rem',
  fontWeight: 300,
  cursor: 'pointer',
});

export default function ArtifactsNav({ activeKind, onNavigate, children }) {
  const location = useLocation();
  const navigate = useNavigate();
  const path = location.pathname;
  // onNavigate lets a host page intercept bubble clicks (e.g. the Artifacts
  // page guarding unsaved edits). Falls back to direct navigation.
  const go = (to) => (onNavigate ? onNavigate(to) : navigate(to));
  const [artifacts, setArtifacts] = useState([]);

  const fetchArtifacts = useCallback(async () => {
    try {
      const res = await api.get('/artifacts');
      setArtifacts(res.data.artifacts || []);
    } catch (err) {
      // Profile/Todo bubbles still render; the artifact list just stays empty.
      console.error('ArtifactsNav: failed to load artifacts', err);
    }
  }, []);

  useEffect(() => { fetchArtifacts(); }, [fetchArtifacts]);

  const byKind = Object.fromEntries(artifacts.map((a) => [a.kind, a]));
  const pinned = BUILTIN_KIND_ORDER
    .filter((k) => byKind[k])
    .map((k) => byKind[k]);
  const custom = artifacts
    .filter((a) => !isBuiltinKind(a.kind))
    .slice()
    .sort((a, b) => (a.title || a.kind).localeCompare(b.title || b.kind));

  const navBubble = (label, to) => (
    <button key={to} onClick={() => go(to)} style={bubbleStyle(path === to)}>
      {label}
    </button>
  );

  const artBubble = (a) => {
    const to = `/artifacts/${a.kind}`;
    const active = path === to || (!!activeKind && a.kind === activeKind);
    return (
      <button
        key={a.kind}
        onClick={() => go(to)}
        title={a.description || undefined}
        style={bubbleStyle(active)}
      >
        {a.title || a.kind}
      </button>
    );
  };

  return (
    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '20px' }}>
      {navBubble('Profile', '/profile')}
      {navBubble('Todo', '/todo')}
      {pinned.map(artBubble)}
      {custom.map(artBubble)}
      {children}
    </div>
  );
}
