import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import MarkdownBody from '../components/MarkdownBody';
import { signInPath } from '../utils/destination';
import { errorSentence, forgetCohortCode, rememberCohortCode } from '../utils/cohort';

// /invitation/<code>: the founder's letter to one invited person (the
// first cohort, docs/experiments/FIRST-COHORT.md). Public, no navigation
// bar, readable signed out; the server records the first opening as a
// time and nothing else. Two buttons: "Start the interview" keeps the code
// and goes through Sign in with X to /cohort, which claims it; "Not for
// me" declines and asks nothing more. Once the invitation was accepted,
// declined or closed, the page says so in one sentence instead of the
// letter (the server no longer sends it).

const backendUrl = process.env.REACT_APP_BACKEND_URL || '';

export const INVITATION_TEXT = {
  notFound: 'This link does not lead to an invitation. Check that you copied all of it.',
  loadFailed: 'This page could not be loaded. Reload it to try again.',
  claimed: 'This invitation has been accepted.',
  claimedSignedOut: 'If you accepted it, sign in to continue.',
  declined: 'This invitation was declined.',
  closed: 'This invitation is closed.',
  justDeclined: 'You declined the invitation. Nothing more is needed from you.',
  startNote: 'Next you sign in with your X account and choose whether to talk or type.',
};

const styles = `
  .loore-invitation {
    min-height: 100vh;
    background: var(--bg-deep);
    color: var(--text-primary);
    padding: clamp(2.5rem, 8vw, 5.5rem) clamp(1.25rem, 5vw, 2rem) 4rem;
    box-sizing: border-box;
  }
  .loore-invitation-inner {
    max-width: 36rem;
    margin: 0 auto;
  }
  .loore-invitation-mark {
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: var(--serif);
    font-weight: 300;
    font-size: 1rem;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: var(--text-muted);
    margin: 0 0 clamp(2.5rem, 7vw, 4rem);
  }
  .loore-invitation h1 {
    font-family: var(--serif);
    font-weight: 400;
    font-size: clamp(2rem, 6vw, 2.6rem);
    line-height: 1.15;
    margin: 0 0 1.75rem;
    color: var(--text-primary);
  }
  .loore-invitation h2 {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 1.6rem;
    line-height: 1.2;
    margin: 0 0 0.75rem;
    color: var(--text-primary);
  }
  .loore-invitation-letter {
    font-family: var(--reading-serif);
    font-size: 1.14rem;
    line-height: 1.7;
    color: var(--text-primary);
  }
  .loore-invitation-about {
    border-top: 1px solid var(--border);
    margin-top: 2.75rem;
    padding-top: 2rem;
    font-family: var(--sans);
    font-weight: 300;
    font-size: 0.98rem;
    line-height: 1.65;
    color: var(--text-secondary);
  }
  .loore-invitation-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 2.75rem;
  }
  .loore-invitation-btn {
    min-height: 48px;
    padding: 0 24px;
    border-radius: 6px;
    font-family: var(--sans);
    font-size: 0.98rem;
    cursor: pointer;
    transition: background 0.2s ease, border-color 0.2s ease, color 0.2s ease;
  }
  .loore-invitation-btn:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .loore-invitation-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
  .loore-invitation-start {
    background: var(--accent);
    border: 1px solid var(--accent);
    color: var(--bg-deep);
    font-weight: 400;
  }
  .loore-invitation-start:hover:not(:disabled) {
    background: var(--accent-hover);
    border-color: var(--accent-hover);
  }
  .loore-invitation-decline {
    background: transparent;
    border: 1px solid var(--border-hover);
    color: var(--text-secondary);
    font-weight: 300;
  }
  .loore-invitation-decline:hover:not(:disabled) {
    color: var(--text-primary);
  }
  .loore-invitation-note {
    font-family: var(--sans);
    font-weight: 300;
    font-size: 0.85rem;
    line-height: 1.55;
    color: var(--text-muted);
    margin: 0.9rem 0 0;
  }
  .loore-invitation-sentence {
    font-family: var(--reading-serif);
    font-size: 1.2rem;
    line-height: 1.6;
    color: var(--text-primary);
    margin: 0 0 1rem;
  }
  .loore-invitation-link {
    font-family: var(--sans);
    font-size: 0.95rem;
    color: var(--accent);
    text-decoration: none;
  }
  .loore-invitation-link:hover { text-decoration: underline; }
  .loore-invitation-error {
    font-family: var(--sans);
    font-weight: 300;
    font-size: 0.9rem;
    color: var(--error);
    margin: 1rem 0 0;
  }
`;

// <meta name="robots" content="noindex, nofollow"> while the page is open:
// the letter is about a person (the server also sends X-Robots-Tag).
function useNoIndex() {
  useEffect(() => {
    const meta = document.createElement('meta');
    meta.setAttribute('name', 'robots');
    meta.setAttribute('content', 'noindex, nofollow');
    document.head.appendChild(meta);
    return () => { meta.remove(); };
  }, []);
}

function Frame({ children }) {
  return (
    <>
      <style>{styles}</style>
      <main className="loore-invitation">
        <div className="loore-invitation-inner">
          <div className="loore-invitation-mark">
            <img src="/loore-logo-transparent.svg" alt="" style={{ height: '20px', width: 'auto', opacity: 0.7 }} />
            Loore
          </div>
          {children}
        </div>
      </main>
    </>
  );
}

export default function InvitationPage() {
  const { code } = useParams();
  const { user, loading } = useUser();
  const navigate = useNavigate();
  // { state, cohort, founder, letter } | { missing: true } | { failed: true }
  const [invitation, setInvitation] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [declined, setDeclined] = useState(false);

  useNoIndex();

  useEffect(() => {
    let alive = true;
    setInvitation(null);
    api.get(`/invitation/${encodeURIComponent(code)}`)
      .then((res) => { if (alive) setInvitation(res.data); })
      .catch((err) => {
        if (!alive) return;
        setInvitation(err.response && err.response.status === 404
          ? { missing: true } : { failed: true });
      });
    return () => { alive = false; };
  }, [code]);

  useEffect(() => {
    document.title = invitation && invitation.cohort
      ? `${invitation.cohort.name} · Loore` : 'An invitation · Loore';
  }, [invitation]);

  const start = () => {
    setError('');
    const kept = rememberCohortCode(code);
    // Without storage (some private windows) the code rides in the address.
    const next = kept ? '/cohort' : `/cohort?code=${encodeURIComponent(code)}`;
    if (user) {
      navigate(next);
      return;
    }
    window.location.href = `${backendUrl}/auth/login?next=${encodeURIComponent(next)}`;
  };

  const decline = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post(`/invitation/${encodeURIComponent(code)}/decline`);
      // A code kept by an earlier "Start the interview" has nothing left
      // to do: no sign-in on this browser is sent to /cohort for it.
      forgetCohortCode();
      setDeclined(true);
    } catch (err) {
      setError(errorSentence(err));
    } finally {
      setBusy(false);
    }
  };

  if (!invitation) return <Frame />;

  if (invitation.missing || invitation.failed) {
    return (
      <Frame>
        <p className="loore-invitation-sentence">
          {invitation.missing ? INVITATION_TEXT.notFound : INVITATION_TEXT.loadFailed}
        </p>
      </Frame>
    );
  }

  if (declined) {
    return (
      <Frame>
        <p className="loore-invitation-sentence">{INVITATION_TEXT.justDeclined}</p>
      </Frame>
    );
  }

  if (invitation.state === 'claimed') {
    return (
      <Frame>
        <p className="loore-invitation-sentence">{INVITATION_TEXT.claimed}</p>
        {!loading && (user ? (
          <Link className="loore-invitation-link" to="/cohort">
            Open {invitation.cohort ? invitation.cohort.name : 'the group'}
          </Link>
        ) : (
          <Link className="loore-invitation-link" to={signInPath('/cohort')}>
            {INVITATION_TEXT.claimedSignedOut}
          </Link>
        ))}
      </Frame>
    );
  }

  if (invitation.state === 'declined' || invitation.state === 'closed') {
    return (
      <Frame>
        <p className="loore-invitation-sentence">
          {invitation.state === 'declined' ? INVITATION_TEXT.declined : INVITATION_TEXT.closed}
        </p>
      </Frame>
    );
  }

  const { cohort, founder, letter } = invitation;
  return (
    <Frame>
      <h1>A letter from {founder}</h1>
      <div className="loore-invitation-letter">
        <MarkdownBody paragraphMargin="0 0 1.05em">{letter || ''}</MarkdownBody>
      </div>
      {cohort && cohort.about && (
        <section className="loore-invitation-about" aria-labelledby="invitation-about">
          <h2 id="invitation-about">What this is</h2>
          <MarkdownBody paragraphMargin="0 0 0.9em">{cohort.about}</MarkdownBody>
        </section>
      )}
      <div className="loore-invitation-actions">
        {/* Not while the session loads: a signed-in person goes straight
            to /cohort, without a second sign-in. */}
        <button type="button" className="loore-invitation-btn loore-invitation-start"
          onClick={start} disabled={busy || loading}>
          Start the interview
        </button>
        <button type="button" className="loore-invitation-btn loore-invitation-decline"
          onClick={decline} disabled={busy}>
          Not for me
        </button>
      </div>
      {!user && !loading && <p className="loore-invitation-note">{INVITATION_TEXT.startNote}</p>}
      {error && <p role="alert" className="loore-invitation-error">{error}</p>}
    </Frame>
  );
}
