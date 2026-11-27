import React, { useEffect, useState } from 'react';
import api from '../../api';
import { pendingCohortCode } from '../../utils/cohort';

// On the waiting page (an account not approved yet, payments off): this
// browser keeps an invitation code from "Start the interview", but the
// account can't accept it (only approved accounts reach /cohort). The
// invitation approves the X account it was made for, so this is another
// account. One sentence says so, from the public invitation route.
export function waitingSentence(invitation) {
  if (!invitation || invitation.state !== 'open' || !invitation.cohort) return null;
  const { founder } = invitation;
  return `You opened an invitation to ${invitation.cohort.name} from ${founder}. `
    + 'This account is waiting for approval, so it can\'t accept it. The invitation is for '
    + `the X account ${founder} sent the link to: sign out, open the link again, and sign in with that X account.`;
}

export default function CohortWaitingNote() {
  const [sentence, setSentence] = useState(null);
  useEffect(() => {
    const code = pendingCohortCode();
    if (!code) return undefined;
    let alive = true;
    api.get(`/invitation/${encodeURIComponent(code)}`)
      .then((res) => { if (alive) setSentence(waitingSentence(res.data)); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  if (!sentence) return null;
  return (
    <p role="status" style={{
      fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.98rem',
      lineHeight: 1.7, color: 'var(--text-primary)', maxWidth: 480,
      margin: '0 auto 2rem', background: 'var(--bg-card)',
      border: '1px solid var(--border)', borderRadius: 12, padding: '1.2rem 1.5rem',
      textAlign: 'left',
    }}>
      {sentence}
    </p>
  );
}
