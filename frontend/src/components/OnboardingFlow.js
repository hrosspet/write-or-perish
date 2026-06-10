import React, { useState } from 'react';
import api from '../api';
import { useUser } from '../contexts/UserContext';

/**
 * First-login onboarding walkthrough (#147).
 *
 * Shown once, right after terms acceptance, for users who haven't
 * completed it (server-tracked via onboarding_completed_at). Both
 * "Begin" and "Skip" mark completion — it never reappears.
 *
 * The step COPY below is v0 placeholder text for maintainer tuning;
 * the engine (steps array, gating, persistence) is the deliverable.
 */

const STEPS = [
  {
    title: 'Welcome to Loore',
    body: (
      <>
        <p>
          Loore is where your words become your <em style={{ color: 'var(--accent)' }}>lore</em> —
          a growing body of writing that an AI reads to genuinely understand you.
        </p>
        <p>
          The more you put in — journal entries, voice notes, imported
          conversations — the better it reflects you back.
        </p>
      </>
    ),
  },
  {
    title: 'Two ways in',
    body: (
      <>
        <p>
          <strong style={{ color: 'var(--text-primary)' }}>Voice</strong> — just talk.
          Rambling is fine; that's where the good stuff hides. Your words are
          transcribed and the AI responds out loud.
        </p>
        <p>
          <strong style={{ color: 'var(--text-primary)' }}>Text</strong> — type instead.
          Same conversation, same understanding, formatted for reading.
        </p>
        <p>You can switch between them mid-conversation.</p>
      </>
    ),
  },
  {
    title: 'Private by default',
    body: (
      <>
        <p>
          Every entry is <strong style={{ color: 'var(--text-primary)' }}>private</strong> unless
          you decide otherwise, and you control whether the AI may read it at all.
        </p>
        <p>
          Entries the AI can read are sent to AI providers (OpenAI, Anthropic)
          to generate responses — never for training unless you explicitly opt in.
          You can change any entry's settings at any time.
        </p>
      </>
    ),
  },
  {
    title: 'It remembers',
    body: (
      <>
        <p>
          As you write, Loore builds artifacts about you: a{' '}
          <strong style={{ color: 'var(--text-primary)' }}>profile</strong>, a{' '}
          <strong style={{ color: 'var(--text-primary)' }}>memory</strong>, your{' '}
          <strong style={{ color: 'var(--text-primary)' }}>todos</strong> and{' '}
          <strong style={{ color: 'var(--text-primary)' }}>intentions</strong>.
        </p>
        <p>
          They persist across sessions and you can read and edit all of them —
          look under Profile, Todo, and Account → Artifacts.
        </p>
        <p>That's it. The rest you'll discover by writing.</p>
      </>
    ),
  },
];

export default function OnboardingFlow() {
  const [step, setStep] = useState(0);
  const [closing, setClosing] = useState(false);
  const { user, setUser } = useUser();

  const finish = async () => {
    setClosing(true);
    try {
      await api.post('/dashboard/onboarding/complete');
    } catch (err) {
      console.error('Failed to mark onboarding complete:', err);
    }
    setUser({ ...user, onboarding_completed: true });
  };

  const isLast = step === STEPS.length - 1;

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000,
      background: 'rgba(10, 9, 8, 0.92)', backdropFilter: 'blur(4px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      opacity: closing ? 0 : 1, transition: 'opacity 0.3s ease',
      padding: '24px',
    }}>
      <div style={{
        maxWidth: '480px', width: '100%',
        background: 'var(--bg-card)', border: '1px solid var(--border)',
        borderRadius: '12px', padding: '48px 40px',
      }}>
        {/* Progress dots */}
        <div style={{ display: 'flex', gap: '8px', marginBottom: '32px' }}>
          {STEPS.map((_, i) => (
            <span key={i} style={{
              width: '8px', height: '8px', borderRadius: '50%',
              background: i === step ? 'var(--accent)' : 'var(--border)',
              transition: 'background 0.2s',
            }} />
          ))}
        </div>

        <h2 style={{
          fontFamily: 'var(--serif)', fontWeight: 300, fontSize: '1.8rem',
          color: 'var(--text-primary)', margin: '0 0 16px 0',
        }}>
          {STEPS[step].title}
        </h2>

        <div style={{
          fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.95rem',
          color: 'var(--text-secondary)', lineHeight: 1.7,
        }}>
          {STEPS[step].body}
        </div>

        <div style={{
          display: 'flex', alignItems: 'center', gap: '12px',
          marginTop: '36px',
        }}>
          <button
            onClick={() => (isLast ? finish() : setStep(step + 1))}
            style={{
              padding: '10px 28px', background: 'var(--accent)',
              border: 'none', borderRadius: '6px', color: 'var(--bg-deep)',
              fontFamily: 'var(--sans)', fontSize: '0.9rem', fontWeight: 400,
              cursor: 'pointer',
            }}
          >
            {isLast ? 'Begin' : 'Next'}
          </button>
          {step > 0 && (
            <button
              onClick={() => setStep(step - 1)}
              style={{
                padding: '10px 20px', background: 'none',
                border: '1px solid var(--border)', borderRadius: '6px',
                color: 'var(--text-muted)', fontFamily: 'var(--sans)',
                fontSize: '0.9rem', cursor: 'pointer',
              }}
            >
              Back
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button
            onClick={finish}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text-muted)', fontFamily: 'var(--sans)',
              fontSize: '0.8rem', fontWeight: 300, opacity: 0.8,
              textDecoration: 'underline',
            }}
          >
            Skip for now
          </button>
        </div>
      </div>
    </div>
  );
}
