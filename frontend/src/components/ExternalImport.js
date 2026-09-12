import React, { useEffect, useRef, useState } from 'react';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import NewTokenDialog from './NewTokenDialog';
import ReferenceList from './ReferenceList';

/**
 * References import (#155 / Download substrate): Community Archive
 * tweets + X bookmarks + web clips from the Chrome clipper (#232).
 * Imported items become semantically searchable (Cmd+K → Semantic)
 * alongside your own entries — and never enter the profile.
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

const ghostButtonStyle = {
  ...buttonStyle, background: 'none',
  border: '1px solid var(--border)', color: 'var(--text-muted)',
};

const codeStyle = {
  fontFamily: 'var(--mono, ui-monospace, monospace)', fontSize: '0.8rem',
  color: 'var(--text-secondary)', background: 'var(--bg-input)',
  padding: '1px 5px', borderRadius: '4px',
};

// Floating "Copied" label above the chrome://extensions address. Native
// title tooltips can't be shown on demand, so this is a small custom one.
const copiedTipStyle = {
  position: 'absolute', left: '50%', bottom: 'calc(100% + 6px)',
  transform: 'translateX(-50%)',
  background: 'var(--bg-deep)', border: '1px solid var(--border)',
  borderRadius: '4px', padding: '3px 8px', whiteSpace: 'nowrap',
  fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 400,
  color: 'var(--text-primary)', pointerEvents: 'none', zIndex: 2,
};

const tokenRowStyle = {
  display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap',
  fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.8rem',
  color: 'var(--text-muted)', padding: '8px 0',
  borderTop: '1px solid var(--border)',
};

export default function ExternalImport() {
  const { user } = useUser();
  const [counts, setCounts] = useState({});
  const [caUsername, setCaUsername] = useState('');
  const [caStatus, setCaStatus] = useState(null);
  const [xStatus, setXStatus] = useState(null);
  const [xSyncMsg, setXSyncMsg] = useState(null);
  const [syncing, setSyncing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [tokens, setTokens] = useState([]);
  const [newToken, setNewToken] = useState(null);  // {id, token}: plaintext, shown once
  const [tokenMsg, setTokenMsg] = useState(null);
  const [addressCopied, setAddressCopied] = useState(false);
  // Bumped whenever counts change so the references list reloads.
  const [listKey, setListKey] = useState(0);
  const fileRef = useRef(null);
  const pollRef = useRef(null);

  const refresh = async () => {
    try {
      const [itemsRes, xRes] = await Promise.all([
        api.get('/external/items', { params: { per_page: 1 } }),
        api.get('/external/twitter/status'),
      ]);
      setCounts((prev) => {
        const next = itemsRes.data.counts || {};
        if (JSON.stringify(prev) !== JSON.stringify(next)) setListKey((k) => k + 1);
        return next;
      });
      setXStatus(xRes.data);
    } catch (e) { /* page still works without counts */ }
  };

  const refreshTokens = async () => {
    try {
      const res = await api.get('/external/tokens');
      setTokens(res.data.tokens || []);
    } catch (e) { /* card still renders */ }
  };

  useEffect(() => {
    refresh();
    refreshTokens();
    return () => pollRef.current && clearInterval(pollRef.current);
  }, []);

  const createToken = async () => {
    setBusy(true);
    setTokenMsg(null);
    try {
      const res = await api.post('/external/tokens', { name: 'Chrome clipper' });
      setNewToken({ id: res.data.id, token: res.data.token });
      refreshTokens();
    } catch (e) {
      setTokenMsg(e.response?.data?.error || 'Could not create a token.');
    }
    setBusy(false);
  };

  // Chrome refuses to open chrome:// pages from a web page (link or
  // window.open), so the address is copy-to-paste instead of a link.
  const copyExtensionsAddress = async () => {
    try {
      await navigator.clipboard.writeText('chrome://extensions');
      setAddressCopied(true);
      setTimeout(() => setAddressCopied(false), 1500);
    } catch (e) {
      setAddressCopied(false);
    }
  };

  const revokeToken = async (id) => {
    setBusy(true);
    setTokenMsg(null);
    try {
      await api.delete(`/external/tokens/${id}`);
      setTokens((prev) => prev.filter((t) => t.id !== id));
      // A revoked token's plaintext has no use anymore.
      setNewToken((cur) => (cur && cur.id === id ? null : cur));
    } catch (e) {
      setTokenMsg(e.response?.data?.error || 'Could not revoke the token.');
    }
    setBusy(false);
  };

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
    setImporting(true);
    setImportMsg(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const res = await api.post('/external/bookmarks/import',
        parsed instanceof Array ? { bookmarks: parsed } : parsed);
      setImportMsg(`Imported ${res.data.created} bookmark` +
        `${res.data.created === 1 ? '' : 's'} ` +
        `(${res.data.skipped} already known).`);
      refresh();
    } catch (e) {
      setImportMsg(e.response?.data?.error
        || 'Import failed — is it valid JSON?');
    }
    if (fileRef.current) fileRef.current.value = '';
    setImporting(false);
    setBusy(false);
  };

  const syncX = async () => {
    setBusy(true);
    setSyncing(true);
    setXSyncMsg(null);  // result text appears when the sync lands
    const baselineCount = counts.twitter_bookmark || 0;
    const baselineSynced = xStatus?.last_synced_at || null;
    try {
      await api.post('/external/twitter/sync');
    } catch (e) {
      setXSyncMsg(e.response?.data?.error || 'Sync failed.');
      setSyncing(false);
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
          setSyncing(false);
          setBusy(false);
        } else if (xRes.data.revoked) {
          clearInterval(pollRef.current);
          setXSyncMsg('X access was revoked \u2014 reconnect below.');
          setSyncing(false);
          setBusy(false);
        } else if (ticks >= 20) {
          clearInterval(pollRef.current);
          setXSyncMsg('Still syncing in the background \u2014 check back in a minute.');
          setSyncing(false);
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
        {(counts.community_archive || counts.twitter_bookmark || counts.web_clip) ? (
          <> Imported so far:{' '}
            {[
              counts.community_archive && `${counts.community_archive} archive tweets`,
              counts.twitter_bookmark && `${counts.twitter_bookmark} bookmarks`,
              counts.web_clip && `${counts.web_clip} clipped pages`,
            ].filter(Boolean).join(' · ')}.
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
              {syncing ? 'Syncing\u2026' : 'Sync bookmarks'}
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
              Connect your X account to pull in your bookmarks. X's API
              serves roughly the 100 most recent; after that, new bookmarks
              sync in nightly.
            </p>
            <a href={`${process.env.REACT_APP_BACKEND_URL}/api/external/twitter/connect`}>
              <button style={buttonStyle}>Connect X</button>
            </a>
          </>
        ) : (
          <p style={helpStyle}>
            Direct sync isn't configured yet. You can still import a
            bookmarks JSON export:
          </p>
        )}
        {(!(xStatus && xStatus.configured) || (user && user.craft_mode)) && (
          <div style={{ marginTop: '10px' }}>
            {xStatus && xStatus.configured && (
              <p style={helpStyle}>
                Craft: import a bookmarks JSON export (browser-exporter
                format) — covers bookmarks beyond the API's recent window.
              </p>
            )}
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
              {importing ? 'Importing\u2026' : 'Import bookmarks JSON'}
            </button>
            {importMsg && (
              <p style={{ ...helpStyle, marginTop: '8px', color: 'var(--text-secondary)' }}>
                {importMsg}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Rides the external-content opt-in (Account, shipped off): the
          clipper only makes sense once Loore searches references. */}
      {user && user.external_content_enabled && (
      <div style={cardStyle}>
        <h3 style={titleStyle}>Chrome clipper</h3>
        <p style={helpStyle}>
          Save any open tab into your references with one key press. The
          extension reads the page in your browser, sends the text to
          Loore, and closes the tab. Clips are references, not your
          writing: they are searchable and quotable, and never enter your
          profile.
        </p>
        <p style={helpStyle}>
          Install: open{' '}
          <span style={{ position: 'relative', display: 'inline-block' }}>
            <code
              onClick={copyExtensionsAddress}
              title="Click to copy"
              style={{ ...codeStyle, cursor: 'pointer' }}
            >
              chrome://extensions
            </code>
            {addressCopied && (
              <span role="status" style={copiedTipStyle}>Copied</span>
            )}
          </span>
          , turn on Developer mode, choose “Load unpacked” and pick the{' '}
          <code style={codeStyle}>extension/</code> folder of the Loore
          repository. Then paste a token below into the extension’s options.
          A token can only add references; it cannot read anything.
        </p>
        {tokens.length > 0 && (
          <div style={{ marginBottom: '12px' }}>
            {tokens.map((t) => (
              <div key={t.id} style={tokenRowStyle}>
                <span style={{ color: 'var(--text-primary)' }}>{t.name}</span>
                <code style={codeStyle}>loore_{t.prefix}…</code>
                <span>created {t.created_at ? t.created_at.slice(0, 10) : '?'}</span>
                <span>
                  {t.last_used_at ? `last used ${t.last_used_at.slice(0, 10)}` : 'never used'}
                </span>
                <button
                  onClick={() => revokeToken(t.id)}
                  disabled={busy}
                  style={{ ...ghostButtonStyle, padding: '4px 10px', marginLeft: 'auto' }}
                >
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
        <button onClick={createToken} disabled={busy} style={buttonStyle}>
          Create token
        </button>
        {tokenMsg && (
          <p style={{ ...helpStyle, marginTop: '8px', color: 'var(--text-secondary)' }}>
            {tokenMsg}
          </p>
        )}
      </div>
      )}

      <ReferenceList refreshKey={listKey} />

      <NewTokenDialog
        token={newToken ? newToken.token : null}
        onClose={() => setNewToken(null)}
      />
    </div>
  );
}
