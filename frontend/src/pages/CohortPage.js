import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import SubscribePage from './SubscribePage';
import CohortGroup from '../components/cohort/CohortGroup';
import {
  StartStep, InterviewStep, ReportStep, CallStep,
} from '../components/cohort/CohortSteps';
import {
  StepList, pageStyle, h1Style, textStyle, smallStyle, errorStyle, linkStyle,
} from '../components/cohort/cohortUi';
import {
  STEP, cohortStep, claimRefusalIsFinal, errorSentence, forgetCohortCode,
  isLiveInvitation, markCohortShown, pendingCohortCode, rememberCohortCode,
} from '../utils/cohort';
import { rememberSubscription } from '../utils/subscription';

// /cohort (the first cohort, docs/experiments/FIRST-COHORT.md): one page
// that shows the person's current step and only that step's action, from
// GET /api/cohort/me (utils/cohort.cohortStep). A code kept by the
// invitation page is claimed here first. `bare`: the account is not let
// in yet (payments on, no subscription), so the page only claims the
// invitation, which can let it in; anything else is the subscribe page.

const backendUrl = process.env.REACT_APP_BACKEND_URL || '';

export const COHORT_TEXT = {
  none: 'You have no invitation to a group.',
  declined: 'You declined this invitation.',
  closed: 'This invitation is closed.',
  claiming: 'Opening your invitation…',
  claimNotFound: 'This invitation link does not work. Open the link you were sent again.',
  loadFailed: 'This page could not be loaded. Reload it to try again.',
};

// The claim refusals that a sign-in with another X account can answer.
const SIGN_IN_AGAIN = new Set(['cohort_sign_in_with_x', 'cohort_wrong_account']);

export default function CohortPage({ bare = false }) {
  const { user, setUser } = useUser();
  const location = useLocation();
  const navigate = useNavigate();
  // A code in the address: the invitation page could not keep it in this
  // browser (some private windows).
  const urlCode = new URLSearchParams(location.search).get('code');
  const [pendingCode, setPendingCode] = useState(() => {
    if (urlCode) {
      rememberCohortCode(urlCode);
      return urlCode;
    }
    return pendingCohortCode();
  });
  const [me, setMe] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [claimError, setClaimError] = useState(null);
  const claimedCode = useRef(null);

  useEffect(() => { markCohortShown(); }, []);

  const load = useCallback(() => api.get('/cohort/me')
    .then((res) => { setMe(res.data); })
    .catch(() => setLoadError(COHORT_TEXT.loadFailed)), []);

  useEffect(() => { load(); }, [load]);

  // The user again, after a claim: the server now names the cohort, turns
  // Circles on and may let the account in for the free period.
  const refreshUser = useCallback(() => api.get('/dashboard', { params: { profile: 0 } })
    .then((res) => {
      if (res.data && res.data.user) {
        rememberSubscription(res.data.user);
        setUser(res.data.user);
      }
    })
    .catch(() => {}), [setUser]);

  const step = cohortStep(me, pendingCode);

  useEffect(() => {
    // A kept code next to a live invitation has done its work (or belongs
    // to another invitation, which this account can't accept as well).
    if (me && pendingCode && isLiveInvitation(me.invitation)) {
      forgetCohortCode();
      setPendingCode(null);
    }
  }, [me, pendingCode]);

  useEffect(() => {
    if (step !== STEP.CLAIM || claimedCode.current === pendingCode) return;
    claimedCode.current = pendingCode;
    api.post('/cohort/claim', { code: pendingCode })
      .then(async (res) => {
        forgetCohortCode();
        setPendingCode(null);
        if (urlCode) navigate('/cohort', { replace: true });
        setMe((current) => ({
          owner_of: [], ...(current || {}), invitation: res.data.invitation,
        }));
        await refreshUser();
      })
      .catch((err) => {
        const status = err.response && err.response.status;
        const code = err.response && err.response.data && err.response.data.code;
        if (claimRefusalIsFinal(status, code)) forgetCohortCode();
        setPendingCode(null);
        if (urlCode) navigate('/cohort', { replace: true });
        setClaimError({
          text: status === 404 ? COHORT_TEXT.claimNotFound : errorSentence(err),
          signInAgain: SIGN_IN_AGAIN.has(code),
        });
      });
  }, [step, pendingCode, urlCode, navigate, refreshUser]);

  const inv = me && me.invitation;
  useEffect(() => {
    if (inv && inv.cohort && step !== STEP.GROUP) document.title = `${inv.cohort.name} · Loore`;
  }, [inv, step]);

  const onChanged = useCallback(async () => {
    await load();
    await refreshUser();
  }, [load, refreshUser]);

  if (bare && step !== STEP.CLAIM && step !== STEP.LOADING && !claimError && !loadError) {
    return <SubscribePage />;
  }

  const claimErrorBlock = claimError && (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      <p role="alert" style={{ ...textStyle, color: 'var(--text-primary)' }}>{claimError.text}</p>
      {claimError.signInAgain && (
        <p style={smallStyle}>
          <a href={`${backendUrl}/auth/logout`} style={linkStyle}>Sign out</a>, then open the
          invitation link again and sign in with the X account it names.
        </p>
      )}
      {bare && !claimError.signInAgain && (
        <Link to="/" style={linkStyle}>Continue to Loore</Link>
      )}
    </div>
  );

  let body;
  if (loadError) {
    body = <p role="alert" style={errorStyle}>{loadError}</p>;
  } else if (step === STEP.LOADING) {
    body = <p style={smallStyle}>Loading…</p>;
  } else if (step === STEP.CLAIM) {
    body = <p role="status" style={textStyle}>{COHORT_TEXT.claiming}</p>;
  } else if (step === STEP.GROUP) {
    body = <CohortGroup user={user} />;
  } else if (step === STEP.NONE) {
    body = claimErrorBlock || <p style={textStyle}>{COHORT_TEXT.none}</p>;
  } else if (step === STEP.DECLINED || step === STEP.CLOSED) {
    body = (
      <>
        {claimErrorBlock}
        <p style={textStyle}>{step === STEP.DECLINED ? COHORT_TEXT.declined : COHORT_TEXT.closed}</p>
      </>
    );
  } else {
    const Step = {
      [STEP.START]: StartStep,
      [STEP.INTERVIEW]: InterviewStep,
      [STEP.REPORT]: ReportStep,
      [STEP.CALL]: CallStep,
    }[step];
    body = (
      <>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <h1 style={h1Style}>{inv.cohort.name}</h1>
          <StepList step={step} />
        </div>
        <Step inv={inv} user={user} onChanged={onChanged} />
      </>
    );
  }

  return <div style={pageStyle} data-step={step}>{body}</div>;
}
