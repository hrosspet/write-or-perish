import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import {
  AddCircleMemberDialog, CircleConfirmDialog, accentButton, plainButton,
} from '../components/CircleDialogs';
import { removeMemberBody } from '../utils/circles';

// "Your circles": the lists of people the user shares entries with.
// Create, rename and delete a circle; add a person by their exact Loore
// name (no search, no list of users); remove a person.

const cardStyle = {
  background: 'var(--bg-surface)', border: '1px solid var(--border)',
  borderRadius: '12px', padding: '22px 24px',
  display: 'flex', flexDirection: 'column', gap: '16px',
};

const quietLink = {
  minHeight: '44px', padding: '0 6px', border: 0, background: 'transparent',
  color: 'var(--text-secondary)', fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '0.875rem', cursor: 'pointer',
};

const eyebrow = {
  fontSize: '0.7rem', letterSpacing: '0.14em', textTransform: 'uppercase',
  color: 'var(--text-secondary)', fontWeight: 400, fontFamily: 'var(--sans)',
};

const inputStyle = {
  flex: 1, minWidth: 0, minHeight: '46px', boxSizing: 'border-box',
  padding: '0 14px', borderRadius: '8px',
  border: '1px solid var(--border-hover)', background: 'var(--bg-input)',
  color: 'var(--text-primary)', fontFamily: 'var(--sans)', fontWeight: 300,
  fontSize: '1rem',
};

const h2Style = {
  margin: 0, fontFamily: 'var(--serif)', fontWeight: 500,
  fontSize: '1.6rem', color: 'var(--text-primary)',
};

const errorStyle = {
  fontSize: '0.85rem', lineHeight: 1.5, color: 'var(--error)',
  fontFamily: 'var(--sans)', fontWeight: 300,
};

const hintStyle = {
  fontSize: '0.8rem', lineHeight: 1.5, color: 'var(--text-secondary)',
  fontFamily: 'var(--sans)', fontWeight: 300,
};

function errorText(err, fallback) {
  return err?.response?.data?.error || fallback;
}

function CircleCard({ circle, open, onToggle, onChanged, onDeleted }) {
  const [name, setName] = useState('');
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState('');
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [newName, setNewName] = useState(circle.name);
  const [editError, setEditError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [removing, setRemoving] = useState(null);
  const inputId = `add-person-${circle.id}`;

  const startAdd = async (e) => {
    e.preventDefault();
    const typed = name.trim();
    if (!typed || adding) return;
    setAdding(true);
    setAddError('');
    try {
      const res = await api.post(`/circles/${circle.id}/members/preview`,
        { username: typed });
      if (res.data.already_member) {
        setAddError(`${res.data.username} is already in ${circle.name}.`);
      } else {
        setPreview(res.data);
      }
    } catch (err) {
      setAddError(errorText(err, 'That did not work. Try again.'));
    } finally {
      setAdding(false);
    }
  };

  const confirmAdd = async (includeExisting) => {
    setBusy(true);
    try {
      const res = await api.post(`/circles/${circle.id}/members`, {
        username: preview.username, include_existing: includeExisting,
      });
      onChanged(res.data.circle);
      setPreview(null);
      setName('');
    } catch (err) {
      setPreview(null);
      setAddError(errorText(err, 'That did not work. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (username) => {
    setBusy(true);
    try {
      const res = await api.delete(
        `/circles/${circle.id}/members/${encodeURIComponent(username)}`);
      onChanged(res.data.circle);
    } catch (err) {
      setAddError(errorText(err, 'That did not work. Try again.'));
    } finally {
      setBusy(false);
      setRemoving(null);
    }
  };

  const rename = async (e) => {
    e.preventDefault();
    setEditError('');
    try {
      const res = await api.put(`/circles/${circle.id}`, { name: newName });
      onChanged(res.data.circle);
      setEditing(false);
    } catch (err) {
      setEditError(errorText(err, 'That did not work. Try again.'));
    }
  };

  const destroy = async () => {
    setBusy(true);
    try {
      await api.delete(`/circles/${circle.id}`);
      onDeleted(circle.id);
    } catch (err) {
      setEditError(errorText(err, 'That did not work. Try again.'));
      setConfirmDelete(false);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <div style={{
        ...cardStyle, padding: '18px 24px', flexDirection: 'row',
        justifyContent: 'space-between', alignItems: 'center',
      }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
          <h2 style={{ ...h2Style, fontSize: '1.5rem' }}>{circle.name}</h2>
          <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            {circle.members.length > 0
              ? circle.members.map((m) => m.username).join(', ')
              : 'Nobody yet. Add a person to share with this circle.'}
          </div>
        </div>
        <button type="button" onClick={onToggle}
          style={{ ...quietLink, color: 'var(--accent)' }}>
          Open
        </button>
      </div>
    );
  }

  return (
    <div style={cardStyle}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'baseline', gap: '16px', flexWrap: 'wrap',
      }}>
        <h2 style={h2Style}>{circle.name}</h2>
        <button type="button" style={quietLink}
          onClick={() => { setEditing((v) => !v); setNewName(circle.name); setEditError(''); }}>
          Rename or delete
        </button>
      </div>

      {editing && (
        <form onSubmit={rename} style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <label htmlFor={`rename-${circle.id}`} style={eyebrow}>Name</label>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <input id={`rename-${circle.id}`} type="text" value={newName}
              maxLength={60} onChange={(e) => setNewName(e.target.value)}
              style={inputStyle} />
            <button type="submit" style={{ ...plainButton, borderColor: 'var(--accent)', color: 'var(--accent)' }}>
              Save name
            </button>
            <button type="button" style={plainButton}
              onClick={() => setConfirmDelete(true)}>
              Delete circle
            </button>
          </div>
          {editError && <div style={errorStyle}>{editError}</div>}
        </form>
      )}

      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {circle.members.length === 0 && (
          <div style={{ ...hintStyle, fontSize: '0.875rem' }}>
            Nobody yet. Add a person to share with this circle.
          </div>
        )}
        {circle.members.map((m, i) => (
          <div key={m.username} style={{
            display: 'flex', justifyContent: 'space-between',
            alignItems: 'center', gap: '12px', minHeight: '48px',
            borderTop: '1px solid var(--border)',
            borderBottom: i === circle.members.length - 1
              ? '1px solid var(--border)' : 'none',
          }}>
            <div style={{ display: 'flex', gap: '10px', alignItems: 'baseline', minWidth: 0 }}>
              <span style={{ fontSize: '1rem', fontWeight: 400, color: 'var(--text-primary)' }}>
                {m.username}
              </span>
              {m.only_new && (
                <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                  new entries only
                </span>
              )}
            </div>
            <button type="button" style={quietLink} disabled={busy}
              onClick={() => setRemoving(m.username)}>
              Remove
            </button>
          </div>
        ))}
      </div>

      <form onSubmit={startAdd} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <label htmlFor={inputId} style={eyebrow}>
          Add a person by their Loore name
        </label>
        <div style={{ display: 'flex', gap: '10px' }}>
          <input id={inputId} type="text" value={name} placeholder="@name"
            autoCapitalize="none" autoCorrect="off" spellCheck={false}
            onChange={(e) => { setName(e.target.value); setAddError(''); }}
            style={inputStyle} />
          <button type="submit" disabled={adding || !name.trim()}
            style={{
              minHeight: '46px', padding: '0 20px', borderRadius: '6px',
              border: '1px solid var(--accent)', background: 'transparent',
              color: 'var(--accent)', fontSize: '0.94rem', fontWeight: 400,
              fontFamily: 'var(--sans)',
              cursor: adding || !name.trim() ? 'default' : 'pointer',
              opacity: name.trim() ? 1 : 0.5,
            }}>
            Add
          </button>
        </div>
        {addError
          ? <div role="alert" style={errorStyle}>{addError}</div>
          : (
            <div style={hintStyle}>
              Loore finds a person only by their exact name; there is no
              list of users to browse.
            </div>
          )}
      </form>

      <AddCircleMemberDialog
        open={!!preview}
        name={preview?.username}
        circleName={circle.name}
        sharedCount={preview?.shared_count || 0}
        busy={busy}
        onClose={() => setPreview(null)}
        onChoice={confirmAdd}
      />
      <CircleConfirmDialog
        open={!!removing}
        title={`Remove ${removing} from ${circle.name}?`}
        body={removeMemberBody(removing, circle.name)}
        confirmLabel={`Remove ${removing}`}
        busy={busy}
        onClose={() => setRemoving(null)}
        onConfirm={() => remove(removing)}
      />
      <CircleConfirmDialog
        open={confirmDelete}
        title={`Delete ${circle.name}?`}
        body={circle.shared_count > 0
          ? `The ${circle.shared_count === 1 ? 'entry' : `${circle.shared_count} entries`} you shared with ${circle.name} ${circle.shared_count === 1 ? 'is' : 'are'} closed to its people from this moment, unless you also shared ${circle.shared_count === 1 ? 'it' : 'them'} another way. The entries stay yours.`
          : 'The list of people is deleted. Nothing was shared with this circle.'}
        confirmLabel="Delete circle"
        busy={busy}
        onClose={() => setConfirmDelete(false)}
        onConfirm={destroy}
      />
    </div>
  );
}

function CirclesPage() {
  const { user } = useUser();
  const [circles, setCircles] = useState(null);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [createError, setCreateError] = useState('');

  const load = useCallback(() => {
    api.get('/circles')
      .then((res) => {
        const list = res.data.circles || [];
        setCircles(list);
        setOpenId((current) => current ?? (list[0]?.id ?? null));
      })
      .catch(() => setError('Your circles could not be loaded. Reload the page to try again.'));
  }, []);

  useEffect(() => {
    document.title = 'Your circles · Loore';
    if (user?.circles_enabled) load();
  }, [user?.circles_enabled, load]);

  if (user && !user.circles_enabled) {
    return (
      <div style={{ padding: '120px 24px', textAlign: 'center' }}>
        <p style={{ color: 'var(--text-muted)', fontFamily: 'var(--sans)', fontSize: '0.9rem' }}>
          Not available.
        </p>
      </div>
    );
  }

  const create = async (e) => {
    e.preventDefault();
    setCreateError('');
    try {
      const res = await api.post('/circles', { name: newName });
      setCircles((list) => [...(list || []), res.data.circle]);
      setOpenId(res.data.circle.id);
      setNewName('');
      setCreating(false);
    } catch (err) {
      setCreateError(errorText(err, 'That did not work. Try again.'));
    }
  };

  const replace = (circle) => setCircles(
    (list) => list.map((c) => (c.id === circle.id ? circle : c)));

  return (
    <div style={{
      padding: '3rem 2rem 4rem', maxWidth: '760px', margin: '0 auto',
      fontFamily: 'var(--sans)', fontWeight: 300, color: 'var(--text-primary)',
      display: 'flex', flexDirection: 'column', gap: '26px',
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <h1 style={{
          margin: 0, fontFamily: 'var(--serif)', fontWeight: 500,
          fontSize: '2.25rem', lineHeight: 1.15,
        }}>
          Your circles
        </h1>
        <p style={{
          margin: 0, fontSize: '0.94rem', lineHeight: 1.55,
          color: 'var(--text-secondary)', maxWidth: '560px',
        }}>
          A circle is a list of people you choose. Only you see your
          circles and who is in them. The people in a circle see the
          entries you share with it, with your name.
        </p>
        <Link to="/shared" style={{ fontSize: '0.875rem', color: 'var(--accent)', textDecoration: 'none' }}>
          Shared with you
        </Link>
      </div>

      {error && <div role="alert" style={errorStyle}>{error}</div>}
      {circles === null && !error && (
        <div style={hintStyle}>Loading…</div>
      )}
      {circles !== null && circles.length === 0 && (
        <div style={{ ...hintStyle, fontSize: '0.94rem' }}>
          You have no circles yet. Make one, add people by their Loore
          name, and choose it under "Who can read this" when you write.
        </div>
      )}

      {(circles || []).map((c) => (
        <CircleCard
          key={c.id}
          circle={c}
          open={openId === c.id}
          onToggle={() => setOpenId(c.id)}
          onChanged={replace}
          onDeleted={(id) => {
            setCircles((list) => list.filter((x) => x.id !== id));
            setOpenId(null);
          }}
        />
      ))}

      {circles !== null && (creating ? (
        <form onSubmit={create} style={{ ...cardStyle, gap: '10px' }}>
          <label htmlFor="new-circle-name" style={eyebrow}>Name of the new circle</label>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <input id="new-circle-name" type="text" value={newName} autoFocus
              maxLength={60} placeholder="Close friends"
              onChange={(e) => setNewName(e.target.value)} style={inputStyle} />
            <button type="submit" style={accentButton} disabled={!newName.trim()}>
              Make circle
            </button>
            <button type="button" style={plainButton}
              onClick={() => { setCreating(false); setCreateError(''); }}>
              Cancel
            </button>
          </div>
          {createError && <div role="alert" style={errorStyle}>{createError}</div>}
        </form>
      ) : (
        <button type="button" onClick={() => setCreating(true)}
          style={{ ...plainButton, alignSelf: 'flex-start' }}>
          New circle
        </button>
      ))}
    </div>
  );
}

export default CirclesPage;
