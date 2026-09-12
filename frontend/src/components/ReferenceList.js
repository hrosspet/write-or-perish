import React, { useEffect, useState } from 'react';
import api from '../api';
import MarkdownBody from './MarkdownBody';

/**
 * Recent references on the Import page (#232): the answer to "did my
 * clip land, and what did it capture?". Newest saved first; a row opens
 * to the stored text; the source link goes to the original; delete
 * removes a bad clip. Visible only with the external-content opt-in,
 * like the rest of the References section.
 */

const PAGE_SIZE = 20;

const SOURCE_LABEL = {
  web_clip: 'page',
  twitter_bookmark: 'tweet',
  community_archive: 'archive tweet',
};

const listStyle = {
  borderTop: '1px solid var(--border)',
  marginTop: '4px',
};

const rowStyle = {
  borderBottom: '1px solid var(--border)',
  padding: '10px 0',
  fontFamily: 'var(--sans)',
  fontWeight: 300,
  fontSize: '0.85rem',
  color: 'var(--text-secondary)',
};

const headStyle = {
  display: 'flex',
  gap: '10px',
  alignItems: 'baseline',
  cursor: 'pointer',
};

const titleTextStyle = {
  color: 'var(--text-primary)',
  fontWeight: 400,
  flex: '1 1 auto',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const metaStyle = {
  color: 'var(--text-muted)',
  fontSize: '0.78rem',
  whiteSpace: 'nowrap',
  flex: '0 0 auto',
};

const bodyStyle = {
  marginTop: '8px',
  padding: '10px 14px',
  background: 'var(--bg-deep)',
  border: '1px solid var(--border)',
  borderRadius: '8px',
  fontSize: '0.85rem',
  lineHeight: 1.6,
  maxHeight: '60vh',
  overflowY: 'auto',
};

const actionsStyle = {
  display: 'flex',
  gap: '14px',
  marginTop: '8px',
  fontSize: '0.78rem',
};

const linkButtonStyle = {
  background: 'none',
  border: 'none',
  padding: 0,
  cursor: 'pointer',
  fontFamily: 'var(--sans)',
  fontSize: '0.78rem',
  fontWeight: 300,
  color: 'var(--text-muted)',
};

const moreButtonStyle = {
  ...linkButtonStyle,
  fontSize: '0.82rem',
  marginTop: '10px',
};

function firstLine(text) {
  const line = (text || '').split('\n').find((l) => l.trim());
  return line ? line.replace(/^#+\s*/, '').trim() : '';
}

function ReferenceRow({ item, onDeleted }) {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const toggle = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (content !== null) return;
    setLoading(true);
    try {
      const res = await api.get(`/external/items/${item.id}`);
      setContent(res.data.content || '');
    } catch (e) {
      setContent('Could not load this reference.');
    }
    setLoading(false);
  };

  const remove = async () => {
    // Two clicks, no browser dialog: the first arms, the second deletes.
    if (!confirming) { setConfirming(true); return; }
    try {
      await api.delete(`/external/items/${item.id}`);
      onDeleted(item.id);
    } catch (e) {
      setConfirming(false);
    }
  };

  const label = SOURCE_LABEL[item.source] || item.source;
  const title = item.title || firstLine(item.preview) || item.url || '(untitled)';
  const who = item.author_handle
    ? (item.source === 'web_clip' ? item.author_handle : `@${item.author_handle}`)
    : null;
  const when = (item.posted_at || item.fetched_at || '').slice(0, 10);

  return (
    <div style={rowStyle}>
      <div style={headStyle} onClick={toggle} title={open ? 'Collapse' : 'Read the saved text'}>
        <span style={metaStyle}>{label}</span>
        <span style={titleTextStyle}>{title}</span>
        <span style={metaStyle}>{[who, when].filter(Boolean).join(' · ')}</span>
      </div>
      {open && (
        <>
          <div style={bodyStyle}>
            {loading ? (
              <span style={{ color: 'var(--text-muted)' }}>Loading…</span>
            ) : (
              <MarkdownBody paragraphMargin="0 0 0.6em 0">{content || ''}</MarkdownBody>
            )}
          </div>
          <div style={actionsStyle}>
            {item.url && (
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ ...linkButtonStyle, textDecoration: 'none', color: 'var(--accent)' }}
              >
                Open source
              </a>
            )}
            <button
              onClick={remove}
              onBlur={() => setConfirming(false)}
              style={{ ...linkButtonStyle, color: confirming ? 'var(--accent)' : 'var(--text-muted)' }}
            >
              {confirming ? 'Delete for good?' : 'Delete'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default function ReferenceList({ refreshKey }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const load = async (nextPage, replace) => {
    setLoading(true);
    try {
      const res = await api.get('/external/items', {
        params: { sort: 'saved', per_page: PAGE_SIZE, page: nextPage },
      });
      setItems((prev) => (replace ? res.data.items : [...prev, ...res.data.items]));
      setTotal(res.data.total || 0);
      setPage(nextPage);
    } catch (e) { /* the section above still works */ }
    setLoading(false);
  };

  // refreshKey changes when the page learns of new items (imports,
  // syncs, a manual refresh), so the list reloads from the top.
  useEffect(() => { load(1, true); }, [refreshKey]);  // eslint-disable-line react-hooks/exhaustive-deps

  const onDeleted = (id) => {
    setItems((prev) => prev.filter((i) => i.id !== id));
    setTotal((t) => Math.max(0, t - 1));
  };

  if (!items.length && !loading) return null;

  return (
    <div style={{ marginTop: '18px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
        <h3 style={{
          fontFamily: 'var(--serif)', fontWeight: 300, fontSize: '1.2rem',
          color: 'var(--text-primary)', margin: '0 0 6px 0',
        }}>
          Recent references
        </h3>
        <button onClick={() => load(1, true)} disabled={loading} style={linkButtonStyle}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>
      <div style={listStyle}>
        {items.map((item) => (
          <ReferenceRow key={item.id} item={item} onDeleted={onDeleted} />
        ))}
      </div>
      {items.length < total && (
        <button onClick={() => load(page + 1, false)} disabled={loading} style={moreButtonStyle}>
          {loading ? 'Loading…' : `Show more (${total - items.length} older)`}
        </button>
      )}
    </div>
  );
}
