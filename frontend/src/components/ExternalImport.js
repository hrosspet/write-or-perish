import React, { useEffect, useRef, useState } from 'react';
import api from '../api';

/**
 * References import (#155 / Download substrate): Community Archive
 * tweets + X bookmarks. Imported items become semantically searchable
 * (Cmd+K → Semantic) alongside your own entries.
 */

const cardStyle = {
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderRadius: '10px',
  padding: '20px 24px',
  marginBottom: '16px',
};

const titleStyle = {
  fontFamily: 'var(--serif)', fontWeight: 300, fontSize: '1.2rem',
  color: 'var(--text-primary)', margin: '0 0 6px 0',
};

const helpStyle = {
  fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.8rem',
  color: 'var(--text-muted)', margin: '0 0 14px 0', lineHeight: 1.6,
};

const inputStyle = {
  background: 'var(--bg-input)', border: '1px solid var(--border)',
  borderRadius: '6px', color: 'var(--text-primary)',
  fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
  padding: '8px 12px',
};

const buttonStyle = {
  padding: '8px 18px', background: 'var(--accent)', border: 'none',
  borderRadius: '6px', color: 'var(--bg-deep)',
  fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
  cursor: 'pointer',
};

export default function ExternalImport() {
  const [counts, setCounts] = useState({});
  const [caUsername, setCaUsername] = useState('');
  const [caStatus, setCaStatus] = useState(null);
  const [xStatus, setXStatus] = useState(null);
  const [xSyncMsg, setXSyncMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);
  const pollRef = useRef(null);

  const refresh = async () => {
    try {
      const [itemsRes, xRes] = await Promise.all([
        api.get('/external/items', { params: { per_page: 1 } }),
        api.get('/external/twitter/status'),
      ]);
      setCounts(itemsRes.data.counts || {});
      setXStatus(xRes.data);
    } catch (e) { /* page still works without counts */ }
  };

  useEffect(() => {
    refresh();
    return () => pollRef.current && clearInterval(pollRef.current);
  }, []);

  const pollCounts = () => {
    // Fetch tasks run in the background — refresh counts a few times.
    let ticks = 0;
    pollRef.current && clearInterval(pollRef.current);
    pollRef.current = setInterval(() => {
      refresh();
      if (++ticks >= 12) clearInterval(pollRef.current);
    }, 5000);
  };

  const fetchCA = async () => {
    if (!caUsername.trim()) return;
    setBusy(true);
    setCaStatus(null);
    try {
      await api.post('/external/community-archive/fetch', {
        username: caUsername.trim(),
      });
      setCaStatus('Fetching in the background — counts update below.');
      pollCounts();
    } catch (e) {
      setCaStatus(e.response?.data?.error || 'Fetch failed.');
    }
    setBusy(false);
  };

  const importBookmarksFile = async (file) => {
    setBusy(true);
    try {
      const text = await file.text();
      const res = await api.post('/external/bookmarks/import',
        JSON.parse(text) instanceof Array
          ? { bookmarks: JSON.parse(text) } : JSON.parse(text));
      setCaStatus(null);
      setXStatus((prev) => ({ ...prev }));
      alert(`Imported ${res.data.created} bookmarks ` +
            `(${res.data.skipped} already known).`);
      refresh();
    } catch (e) {
      alert(e.response?.data?.error || 'Import failed — is it valid JSON?');
    }
    setBusy(false);
  };

  const syncX = async () => {
    setBusy(true);
    setXSyncMsg(null);  // result text appears when the sync lands
    const baselineCount = counts.twitter_bookmark || 0;
    const baselineSynced = xStatus?.last_synced_at || null;
    try {
      await api.post('/external/twitter/sync');
    } catch (e) {
      setXSyncMsg(e.response?.data?.error || 'Sync failed.');
      setBusy(false);
      return;
    }
    // The sync runs in the background; poll until last_synced_at moves,
    // then report what actually happened.
    let ticks = 0;
    pollRef.current && clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      ticks += 1;
      try {
        const [itemsRes, xRes] = await Promise.all([
          api.get('/external/items', { params: { per_page: 1 } }),
          api.get('/external/twitter/status'),
        ]);
        setCounts(itemsRes.data.counts || {});
        setXStatus(xRes.data);
        if (xRes.data.last_synced_at
            && xRes.data.last_synced_at !== baselineSynced) {
          clearInterval(pollRef.current);
          const created =
            (itemsRes.data.counts?.twitter_bookmark || 0) - baselineCount;
          setXSyncMsg(created > 0
            ? `Synced \u2014 ${created} new bookmark${created === 1 ? '' : 's'}.`
            : 'Synced \u2014 no new bookmarks.');
          setBusy(false);
        } else if (xRes.data.revoked) {
          clearInterval(pollRef.current);
          setXSyncMsg('X access was revoked \u2014 reconnect below.');
          setBusy(false);
        } else if (ticks >= 20) {
          clearInterval(pollRef.current);
          setXSyncMsg('Still syncing in the background \u2014 check back in a minute.');
          setBusy(false);
        }
      } catch (e) { /* transient — keep polling */ }
    }, 3000);
  };

  return (
    <div style={{ marginTop: '32px' }}>
      <h2 style={{
        fontFamily: 'var(--serif)', fontWeight: 300, fontSize: '1.5rem',
        color: 'var(--text-primary)', margin: '0 0 4px 0',
      }}>
        References
      </h2>
      <p style={helpStyle}>
        Content you've saved elsewhere, made searchable next to your own
        writing (Cmd+K → Semantic).
        {(counts.community_archive || counts.twitter_bookmark) ? (
          <> Imported so far:
            {counts.community_archive ? ` ${counts.community_archive} archive tweets` : ''}
            {counts.community_archive && counts.twitter_bookmark ? ' ·' : ''}
            {counts.twitter_bookmark ? ` ${counts.twitter_bookmark} bookmarks` : ''}.
          </>
        ) : null}
      </p>

      <div style={cardStyle}>
        <h3 style={titleStyle}>Community Archive</h3>
        <p style={helpStyle}>
          Fetch tweets from the open Community Archive — any account that
          donated its archive. Try your own handle or someone you follow.
        </p>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <input
            value={caUsername}
            onChange={(e) => setCaUsername(e.target.value)}
            placeholder="@username"
            style={{ ...inputStyle, flex: '1 1 180px' }}
          />
          <button onClick={fetchCA} disabled={busy} style={buttonStyle}>
            Fetch tweets
          </button>
        </div>
        {caStatus && <p style={{ ...helpStyle, margin: '10px 0 0 0' }}>{caStatus}</p>}
      </div>

      <div style={cardStyle}>
        <h3 style={titleStyle}>X Bookmarks</h3>
        {xStatus && xStatus.revoked ? (
          <>
            <p style={helpStyle}>
              X disconnected {xStatus.handle ? `(@${xStatus.handle}) ` : ''}—
              access was revoked or expired. Reconnect to resume nightly
              bookmark sync.
            </p>
            <a href={`${process.env.REACT_APP_BACKEND_URL}/api/external/twitter/connect`}>
              <button style={buttonStyle}>Reconnect X</button>
            </a>
          </>
        ) : xStatus && xStatus.connected ? (
          <>
            <p style={helpStyle}>
              Connected as @{xStatus.handle}
              {xStatus.last_synced_at ? ` · last synced ${xStatus.last_synced_at.slice(0, 10)}` : ''}
            </p>
            <button onClick={syncX} disabled={busy} style={buttonStyle}>
              {busy ? 'Syncing\u2026' : 'Sync bookmarks'}
            </button>
            {xSyncMsg && (
              <p style={{ ...helpStyle, marginTop: '8px', color: 'var(--text-secondary)' }}>
                {xSyncMsg}
              </p>
            )}
            <p style={{ ...helpStyle, marginTop: '8px' }}>
              New bookmarks sync automatically once a night — the button is
              just for syncing right now.
            </p>
          </>
        ) : xStatus && xStatus.configured ? (
          <>
            <p style={helpStyle}>
              Connect your X account to pull in your bookmarks
              (the 800 most recent — X's cap).
            </p>
            <a href={`${process.env.REACT_APP_BACKEND_URL}/api/external/twitter/connect`}>
              <button style={buttonStyle}>Connect X</button>
            </a>
          </>
        ) : (
          <>
            <p style={helpStyle}>
              Direct sync isn't configured yet. You can still import a
              bookmarks JSON export:
            </p>
            <div style={{ marginTop: '10px' }}>
              <input
                ref={fileRef}
                type="file"
                accept="application/json"
                style={{ display: 'none' }}
                onChange={(e) => e.target.files[0] && importBookmarksFile(e.target.files[0])}
              />
              <button
                onClick={() => fileRef.current && fileRef.current.click()}
                disabled={busy}
                style={{
                  ...buttonStyle, background: 'none',
                  border: '1px solid var(--border)', color: 'var(--text-muted)',
                }}
              >
                Import bookmarks JSON
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
