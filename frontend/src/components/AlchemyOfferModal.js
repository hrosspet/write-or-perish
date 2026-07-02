import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';

/**
 * Opt-in modal for Alchemical Mode (gated depth work).
 *
 * Shown from the Home "Alchemy" card while alchemy_status === 'offered'.
 * The user picks a source, explicitly accepts the risks, and opts in via
 * POST /api/alchemy/opt-in. On success the user context flips to 'active'
 * and we navigate straight to /alchemy.
 */
function AlchemyOfferModal({ onClose }) {
  const { setUser } = useUser();
  const navigate = useNavigate();

  const [sources, setSources] = useState([]);
  const [loadingSources, setLoadingSources] = useState(true);
  const [selectedSlug, setSelectedSlug] = useState(null);
  const [consented, setConsented] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api.get('/alchemy/sources')
      .then((res) => {
        setSources(res.data?.sources || []);
        setLoadingSources(false);
      })
      .catch(() => {
        setError("Couldn't load the source list. Please try again later.");
        setLoadingSources(false);
      });
  }, []);

  const selected = sources.find((s) => s.slug === selectedSlug);
  const canBegin = consented && !!selected && selected.available && !submitting;

  const handleBegin = () => {
    if (!canBegin) return;
    setSubmitting(true);
    setError('');
    api.post('/alchemy/opt-in', { source_slug: selectedSlug, accept_risks: true })
      .then((res) => {
        const sourceSlug = res.data?.source_slug || selectedSlug;
        setUser((prev) =>
          prev ? { ...prev, alchemy_status: 'active', alchemy_source: sourceSlug } : prev
        );
        onClose();
        navigate('/alchemy');
      })
      .catch((err) => {
        setSubmitting(false);
        setError(
          err.response?.data?.error ||
          err.response?.data?.message ||
          'Something went wrong. Please try again.'
        );
      });
  };

  const backdropStyle = {
    position: 'fixed',
    top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: 'rgba(0, 0, 0, 0.7)',
    backdropFilter: 'blur(8px)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 10000,
    padding: '24px',
  };

  const contentStyle = {
    background: 'var(--bg-card)',
    border: '1px solid var(--border)',
    padding: '2rem',
    borderRadius: '12px',
    width: '100%',
    maxWidth: '560px',
    color: 'var(--text-primary)',
    maxHeight: '85vh',
    overflowY: 'auto',
    fontFamily: 'var(--sans)',
    fontWeight: 300,
  };

  const text = {
    color: 'var(--text-secondary)',
    lineHeight: 1.65,
    fontSize: '0.92rem',
  };

  return (
    <div style={backdropStyle} onClick={onClose}>
      <div style={contentStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{
          fontFamily: 'var(--serif)',
          fontWeight: 300,
          fontSize: '1.6rem',
          margin: '0 0 1rem 0',
        }}>
          Alchemical Mode
        </h2>

        <p style={text}>
          Based on the depth of your writing here, Loore can guide you through
          a serious body of transformative source material &mdash; translated
          into your own language, surfaced when it's relevant to where you
          are, and paced by your own sharing.
        </p>

        {/* Risk disclaimer — deliberately strong; do not soften. */}
        <div style={{
          border: '1px solid var(--warning)',
          background: 'color-mix(in srgb, var(--warning) 7%, transparent)',
          borderRadius: '8px',
          padding: '14px 16px',
          margin: '1.3rem 0',
        }}>
          <p style={{
            ...text,
            color: 'var(--warning)',
            fontWeight: 400,
            margin: '0 0 0.5rem 0',
            letterSpacing: '0.04em',
            textTransform: 'uppercase',
            fontSize: '0.75rem',
          }}>
            Please read before proceeding
          </p>
          <p style={{ ...text, margin: 0 }}>
            This mode is <strong style={{ color: 'var(--text-primary)' }}>experimental</strong>.
            It engages material that can be
            {' '}<strong style={{ color: 'var(--text-primary)' }}>destabilizing</strong> &mdash;
            it may surface difficult experiences and unsettle how things feel for a while.
            Proceed only if you are currently
            {' '}<strong style={{ color: 'var(--text-primary)' }}>stable and resourced</strong>.
            You do so <strong style={{ color: 'var(--text-primary)' }}>at your own risk</strong>.
            You can stop at any time. Nothing here replaces professional support.
          </p>
        </div>

        {/* Source selection */}
        <p style={{ ...text, fontWeight: 400, color: 'var(--text-primary)', margin: '0 0 0.6rem 0' }}>
          Choose a source
        </p>
        {loadingSources ? (
          <p style={{ ...text, color: 'var(--text-muted)' }}>Loading sources&hellip;</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {sources.map((source) => {
              const isSelected = source.slug === selectedSlug;
              const disabled = !source.available;
              return (
                <div
                  key={source.slug}
                  role="radio"
                  aria-checked={isSelected}
                  aria-disabled={disabled}
                  onClick={() => !disabled && setSelectedSlug(source.slug)}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: '12px',
                    padding: '12px 14px',
                    borderRadius: '8px',
                    border: `1px solid ${isSelected ? 'var(--accent)' : 'var(--border)'}`,
                    background: isSelected ? 'var(--accent-subtle)' : 'var(--bg-deep)',
                    cursor: disabled ? 'default' : 'pointer',
                    opacity: disabled ? 0.45 : 1,
                    transition: 'border-color 0.2s ease, background 0.2s ease',
                  }}
                >
                  {/* radio dot */}
                  <span style={{
                    flexShrink: 0,
                    marginTop: '3px',
                    width: '14px',
                    height: '14px',
                    borderRadius: '50%',
                    border: `1px solid ${isSelected ? 'var(--accent)' : 'var(--border-hover)'}`,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}>
                    {isSelected && (
                      <span style={{
                        width: '7px',
                        height: '7px',
                        borderRadius: '50%',
                        background: 'var(--accent)',
                      }} />
                    )}
                  </span>
                  <div>
                    <p style={{
                      margin: 0,
                      color: 'var(--text-primary)',
                      fontWeight: 400,
                      fontSize: '0.92rem',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.6rem',
                    }}>
                      {source.title}
                      {disabled && (
                        <span style={{
                          fontSize: '0.65rem',
                          fontWeight: 400,
                          color: 'var(--text-muted)',
                          letterSpacing: '0.05em',
                          textTransform: 'uppercase',
                        }}>
                          Coming soon
                        </span>
                      )}
                    </p>
                    {source.description && (
                      <p style={{ ...text, fontSize: '0.84rem', margin: '4px 0 0 0', color: 'var(--text-muted)' }}>
                        {source.description}
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
            {sources.length === 0 && !error && (
              <p style={{ ...text, color: 'var(--text-muted)' }}>No sources available yet.</p>
            )}
          </div>
        )}

        {/* Explicit consent */}
        <label style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '10px',
          margin: '1.3rem 0 0 0',
          cursor: 'pointer',
        }}>
          <input
            type="checkbox"
            checked={consented}
            onChange={(e) => setConsented(e.target.checked)}
            style={{ marginTop: '3px', accentColor: 'var(--accent)', cursor: 'pointer' }}
          />
          <span style={{ ...text, color: 'var(--text-primary)' }}>
            I understand this is experimental and I proceed at my own risk.
          </span>
        </label>

        {error && (
          <p style={{ ...text, color: 'var(--error)', marginTop: '1rem', marginBottom: 0 }}>
            {error}
          </p>
        )}

        <div style={{
          display: 'flex',
          justifyContent: 'flex-end',
          gap: '12px',
          marginTop: '1.6rem',
        }}>
          <button
            onClick={onClose}
            disabled={submitting}
            style={{
              padding: '10px 20px',
              fontSize: '0.92rem',
              color: 'var(--text-muted)',
              borderColor: 'var(--border)',
            }}
          >
            Not now
          </button>
          <button
            onClick={handleBegin}
            disabled={!canBegin}
            style={{
              padding: '10px 24px',
              fontSize: '0.92rem',
              color: 'var(--accent)',
              borderColor: 'var(--accent)',
              opacity: canBegin ? 1 : 0.45,
              cursor: canBegin ? 'pointer' : 'default',
            }}
          >
            {submitting ? 'Beginning…' : 'Begin'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default AlchemyOfferModal;
