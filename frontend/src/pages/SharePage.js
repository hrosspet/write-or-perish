import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import api from '../api';
import MarkdownBody from '../components/MarkdownBody';
import useSubmitShortcut from '../hooks/useSubmitShortcut';
import useEscapeKey from '../hooks/useEscapeKey';
import { useUser } from '../contexts/UserContext';
import { formatDate } from '../utils/date';

const SHARE_TYPES = ['need', 'offering', 'insight', 'exploration', 'intention', 'other'];

const quietAction = {
  background: 'none', border: 'none', cursor: 'pointer', padding: 0,
  fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
  color: 'var(--text-muted)', textDecoration: 'underline',
};

function SectionHeader({ children }) {
  return (
    <div style={{
      fontSize: '0.68rem', letterSpacing: '0.18em', textTransform: 'uppercase',
      color: 'var(--accent)', opacity: 0.6, marginBottom: '1.2rem',
      fontFamily: 'var(--sans)', fontWeight: 500,
    }}>
      {children}
    </div>
  );
}

function TypeBadge({ type }) {
  return (
    <span style={{
      fontFamily: 'var(--sans)', fontSize: '0.65rem', fontWeight: 300,
      letterSpacing: '0.08em', textTransform: 'uppercase',
      color: 'var(--text-muted)', border: '1px solid var(--border)',
      borderRadius: '4px', padding: '2px 8px', whiteSpace: 'nowrap',
    }}>
      {type}
    </span>
  );
}

export default function SharePage() {
  const { user } = useUser();
  const [shares, setShares] = useState([]);
  const [loading, setLoading] = useState(true);

  // editingId: null | 'new' | share id
  const [editingId, setEditingId] = useState(null);
  const [editContent, setEditContent] = useState('');
  const [editType, setEditType] = useState('other');
  const [saving, setSaving] = useState(false);

  // Inline confirms — at most one open at a time.
  const [confirmPublishId, setConfirmPublishId] = useState(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  const editTextareaRef = useRef(null);

  const fetchShares = useCallback(async () => {
    try {
      const res = await api.get('/share');
      setShares(res.data.shares);
    } catch (err) {
      console.error('Failed to load shares:', err);
    }
    setLoading(false);
  }, []);

  useEffect(() => { fetchShares(); }, [fetchShares]);

  const openEdit = (share) => {
    setEditingId(share.id);
    setEditContent(share.content || '');
    setEditType(share.share_type || 'other');
    setConfirmPublishId(null);
    setConfirmDeleteId(null);
  };

  const openNew = () => {
    setEditingId('new');
    setEditContent('');
    setEditType('other');
    setConfirmPublishId(null);
    setConfirmDeleteId(null);
  };

  const closeEdit = () => { setEditingId(null); setEditContent(''); };

  const handleSave = async () => {
    if (!editContent.trim() || saving) return;
    setSaving(true);
    try {
      if (editingId === 'new') {
        await api.post('/share', { content: editContent, share_type: editType });
      } else {
        await api.patch(`/share/${editingId}`, { content: editContent, share_type: editType });
      }
      await fetchShares();
      closeEdit();
    } catch (err) {
      console.error('Failed to save share:', err);
    }
    setSaving(false);
  };

  const saveEnabled = editingId !== null && !saving && !!editContent.trim();
  useSubmitShortcut(editTextareaRef, handleSave, saveEnabled);
  useEscapeKey(closeEdit, editingId !== null && !saving);

  const handlePublish = async (id) => {
    setConfirmPublishId(null);
    try {
      await api.post(`/share/${id}/publish`);
      await fetchShares();
    } catch (err) {
      console.error('Failed to publish share:', err);
    }
  };

  const handleRevoke = async (id) => {
    try {
      await api.post(`/share/${id}/revoke`);
      await fetchShares();
    } catch (err) {
      console.error('Failed to revoke share:', err);
    }
  };

  const handleDelete = async (id) => {
    setConfirmDeleteId(null);
    try {
      await api.delete(`/share/${id}`);
      await fetchShares();
    } catch (err) {
      console.error('Failed to delete share:', err);
    }
  };

  // Defense in depth: the nav link is hidden when the flag is off, but the
  // route may still be reachable directly.
  if (user && !user.share_v1_enabled) {
    return (
      <div style={{ padding: '120px 24px', textAlign: 'center' }}>
        <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>
          Not available.
        </p>
      </div>
    );
  }

  const renderEditForm = () => (
    <div style={{ marginBottom: '2rem' }}>
      <textarea
        ref={editTextareaRef}
        autoFocus
        value={editContent}
        onChange={(e) => setEditContent(e.target.value)}
        placeholder="What would you like to give outward? (markdown)"
        style={{
          width: '100%', minHeight: '160px',
          background: 'var(--bg-input)', border: '1px solid var(--border)',
          borderRadius: '8px', color: 'var(--text-primary)',
          fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
          padding: '16px', lineHeight: 1.6, resize: 'vertical',
        }}
      />
      <div style={{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
        <select
          value={editType}
          onChange={(e) => setEditType(e.target.value)}
          style={{
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-secondary)',
            fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
            padding: '8px 10px', cursor: 'pointer',
          }}
        >
          {SHARE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <button
          onClick={handleSave}
          disabled={!saveEnabled}
          style={{
            padding: '8px 20px', background: 'var(--accent)', border: 'none',
            borderRadius: '6px', color: 'var(--bg-deep)',
            fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
            cursor: !saveEnabled ? 'not-allowed' : 'pointer',
            opacity: !saveEnabled ? 0.6 : 1,
          }}
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button
          onClick={closeEdit}
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
  );

  const renderItem = (share) => {
    if (editingId === share.id) {
      return <div key={share.id}>{renderEditForm()}</div>;
    }
    const dateLabel = share.status === 'published'
      ? `published ${formatDate(share.published_at)}`
      : share.status === 'revoked'
        ? `revoked ${formatDate(share.revoked_at)}`
        : formatDate(share.created_at);
    return (
      <div
        key={share.id}
        style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)',
          borderRadius: '12px', padding: '24px 28px', marginBottom: '16px',
          opacity: share.status === 'revoked' ? 0.7 : 1,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
          <TypeBadge type={share.share_type} />
          <span style={{
            fontFamily: 'var(--sans)', fontSize: '0.7rem', fontWeight: 300,
            color: 'var(--text-muted)', opacity: 0.7,
          }}>
            {dateLabel}
          </span>
        </div>
        <div style={{
          fontFamily: 'var(--sans)', fontSize: '0.92rem', fontWeight: 300,
          color: 'var(--text-secondary)', lineHeight: 1.7,
        }}>
          <MarkdownBody>{share.content}</MarkdownBody>
        </div>
        <div style={{ marginTop: '14px' }}>
          {confirmPublishId === share.id ? (
            <div>
              <div style={{
                fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
                color: 'var(--text-secondary)', marginBottom: '10px',
              }}>
                Where should this go? Publishing to Loore puts it in the
                Commons and on your public page at /share/u/{user?.username}.
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                <button
                  onClick={() => handlePublish(share.id)}
                  style={{
                    fontFamily: 'var(--sans)', fontSize: '0.82rem', fontWeight: 400,
                    padding: '7px 16px', borderRadius: '6px', cursor: 'pointer',
                    background: 'var(--accent)', border: 'none', color: 'var(--bg-deep)',
                  }}
                >
                  Publish to Loore
                </button>
                {['Twitter / X', 'Substack'].map((channel) => (
                  <span
                    key={channel}
                    style={{
                      fontFamily: 'var(--sans)', fontSize: '0.82rem', fontWeight: 300,
                      padding: '7px 16px', borderRadius: '6px',
                      border: '1px dashed var(--border)', color: 'var(--text-muted)',
                      opacity: 0.6, cursor: 'default',
                    }}
                  >
                    {channel} · coming soon
                  </span>
                ))}
                <button onClick={() => setConfirmPublishId(null)} style={quietAction}>
                  Cancel
                </button>
              </div>
            </div>
          ) : confirmDeleteId === share.id ? (
            <div style={{ display: 'flex', alignItems: 'baseline', gap: '14px', flexWrap: 'wrap' }}>
              <span style={{
                fontFamily: 'var(--sans)', fontSize: '0.8rem', fontWeight: 300,
                color: 'var(--text-secondary)',
              }}>
                Delete this share?
              </span>
              <button onClick={() => handleDelete(share.id)} style={{ ...quietAction, color: 'var(--error)' }}>
                Delete
              </button>
              <button onClick={() => setConfirmDeleteId(null)} style={quietAction}>
                Cancel
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: '16px' }}>
              {share.status !== 'published' && (
                <button onClick={() => openEdit(share)} style={quietAction}>Edit</button>
              )}
              {share.status !== 'published' && (
                <button
                  onClick={() => { setConfirmDeleteId(null); setConfirmPublishId(share.id); }}
                  style={quietAction}
                >
                  Publish
                </button>
              )}
              {share.status === 'published' && share.public_node_id && (
                <Link
                  to={`/node/${share.public_node_id}`}
                  style={{ ...quietAction, color: 'var(--accent)' }}
                >
                  View thread
                </Link>
              )}
              {share.status === 'published' && (
                <button onClick={() => handleRevoke(share.id)} style={quietAction}>Revoke</button>
              )}
              <button
                onClick={() => { setConfirmPublishId(null); setConfirmDeleteId(share.id); }}
                style={quietAction}
              >
                Delete
              </button>
            </div>
          )}
        </div>
      </div>
    );
  };

  const groups = [
    { title: 'Published', items: shares.filter((s) => s.status === 'published') },
    { title: 'Drafts', items: shares.filter((s) => s.status === 'draft') },
    { title: 'Revoked', items: shares.filter((s) => s.status === 'revoked') },
  ];

  return (
    <div style={{ padding: '60px 24px', maxWidth: '800px', margin: '0 auto' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: '16px',
        flexWrap: 'wrap', marginBottom: '8px',
      }}>
        <h1 style={{
          fontFamily: 'var(--serif)', fontSize: '2rem', fontWeight: 300,
          color: 'var(--text-primary)', margin: 0,
        }}>
          Share
        </h1>
        <div style={{ flex: 1 }} />
        {editingId === null && (
          <button
            onClick={openNew}
            style={{
              padding: '8px 20px', background: 'var(--accent)', border: 'none',
              borderRadius: '6px', color: 'var(--bg-deep)',
              fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
              cursor: 'pointer',
            }}
          >
            New share
          </button>
        )}
      </div>
      <p style={{
        fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
        color: 'var(--text-muted)', margin: '0 0 8px 0', opacity: 0.7,
        lineHeight: 1.6,
      }}>
        Pieces of your writing worth giving outward — nothing is visible to
        anyone until you publish it, and you can take anything back.
      </p>
      {user?.username && (
        <Link
          to={`/share/u/${user.username}`}
          style={{
            fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
            color: 'var(--text-muted)', textDecoration: 'underline',
          }}
        >
          view your public page
        </Link>
      )}
      <Link
        to="/forum"
        style={{
          fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
          color: 'var(--text-muted)', textDecoration: 'underline',
          marginLeft: '16px',
        }}
      >
        the Commons →
      </Link>

      <div style={{ height: '1px', background: 'var(--accent-dim)', opacity: 0.3, margin: '24px 0' }} />

      {editingId === 'new' && renderEditForm()}

      {!loading && shares.length === 0 && editingId === null && (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <p style={{
            color: 'var(--text-muted)', fontFamily: 'var(--sans)',
            fontSize: '0.9rem', marginBottom: '16px', lineHeight: 1.6,
          }}>
            Shares the AI proposes in conversation land here as private drafts
            — or start one yourself.
          </p>
          <button
            onClick={openNew}
            style={{
              padding: '10px 24px', background: 'var(--accent)', border: 'none',
              borderRadius: '6px', color: 'var(--bg-deep)',
              fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 400,
              cursor: 'pointer',
            }}
          >
            New share
          </button>
        </div>
      )}

      {groups.map((group) => (
        group.items.length > 0 && (
          <div key={group.title} style={{ marginBottom: '2.5rem' }}>
            <SectionHeader>{group.title}</SectionHeader>
            {group.items.map(renderItem)}
          </div>
        )
      ))}
    </div>
  );
}
