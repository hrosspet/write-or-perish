// The first cohort's step logic and helpers (utils/cohort.js): which step
// /cohort shows for each answer of GET /api/cohort/me, the kept code, the
// once-per-session send to /cohort, and the call's time.
import {
  STEP, cohortStep, stepIndex, claimRefusalIsFinal, shouldShowCohort,
  reportStepOpen, rememberCohortCode, pendingCohortCode, forgetCohortCode,
  COHORT_CODE_MAX_AGE_DAYS, callTimeText, fromLocalInput, toLocalInput,
  dayText, errorSentence, markInterviewOpen, forgetCohortOnSignOut,
  interviewBackLinkName, isInterviewPage,
} from './cohort';

const CODE = 'Q2xvY2tfY29kZV9mb3JfdGVzdGluZzEy';

function invitation(overrides = {}) {
  return {
    id: 7,
    state: 'claimed',
    cohort: { name: 'First Eight', founder: 'Peter' },
    letter: 'Dear Mara',
    admitted_at: null,
    call_done_at: null,
    interview: { node_id: null, open: null },
    report: { node_id: null, task_status: null, sent_at: null, skipped_at: null, shared_with_founder: false },
    intro: { node_id: null, task_status: null, shared_with_group: false },
    ...overrides,
  };
}

const me = (inv, ownerOf = []) => ({ invitation: inv, owner_of: ownerOf });

describe('cohortStep', () => {
  test('nothing loaded yet', () => {
    expect(cohortStep(null)).toBe(STEP.LOADING);
  });

  test('a kept code without an invitation is claimed first', () => {
    expect(cohortStep(me(null), CODE)).toBe(STEP.CLAIM);
    // ...also next to a declined or closed one (a new invitation).
    expect(cohortStep(me(invitation({ state: 'declined' })), CODE)).toBe(STEP.CLAIM);
  });

  test('a kept code next to a live invitation is not claimed again', () => {
    expect(cohortStep(me(invitation()), CODE)).toBe(STEP.START);
  });

  test('no invitation: nothing, or the group for a founder', () => {
    expect(cohortStep(me(null))).toBe(STEP.NONE);
    expect(cohortStep(me(null, [{ id: 1, name: 'First Eight', slug: 'first-eight' }])))
      .toBe(STEP.GROUP);
  });

  test('claimed: the letter and starting the interview', () => {
    expect(cohortStep(me(invitation({ state: 'claimed' })))).toBe(STEP.START);
    expect(cohortStep(me(invitation({ state: 'opened' })))).toBe(STEP.START);
  });

  test('interview begun: continue it or finish it', () => {
    const inv = invitation({
      state: 'interview_started',
      interview: { node_id: 123, open: { text: '/node/130', voice: '/voice?parent=130' } },
    });
    expect(cohortStep(me(inv))).toBe(STEP.INTERVIEW);
  });

  test('a draft being written, failed, empty, or done: the report', () => {
    const started = { node_id: 123, open: null };
    for (const status of ['pending', 'processing', 'failed', 'nothing_to_draft']) {
      const inv = invitation({
        state: 'interview_started', interview: started,
        report: { node_id: null, task_status: status },
      });
      expect(cohortStep(me(inv))).toBe(STEP.REPORT);
    }
    const drafted = invitation({
      state: 'report_drafted', interview: started,
      report: { node_id: 140, task_status: 'done' },
    });
    expect(cohortStep(me(drafted))).toBe(STEP.REPORT);
  });

  test('report sent, or Send nothing: the call', () => {
    const sent = invitation({
      state: 'report_sent',
      report: { node_id: 140, task_status: 'done', sent_at: '2026-11-27T10:00:00Z', shared_with_founder: true },
    });
    expect(cohortStep(me(sent))).toBe(STEP.CALL);
    const skipped = invitation({
      state: 'report_skipped',
      report: { node_id: null, task_status: null, skipped_at: '2026-11-27T10:00:00Z' },
    });
    expect(cohortStep(me(skipped))).toBe(STEP.CALL);
    const callDone = invitation({
      state: 'call_done', call_done_at: '2026-11-28T10:00:00Z',
      report: { node_id: 140, sent_at: '2026-11-27T10:00:00Z' },
    });
    expect(cohortStep(me(callDone))).toBe(STEP.CALL);
  });

  test('admitted: the group page', () => {
    const inv = invitation({
      state: 'admitted', admitted_at: '2026-11-29T10:00:00Z',
      report: { node_id: 140, sent_at: '2026-11-27T10:00:00Z' },
    });
    expect(cohortStep(me(inv))).toBe(STEP.GROUP);
  });

  test('declined and closed say so; a founder still gets the group', () => {
    expect(cohortStep(me(invitation({ state: 'declined' })))).toBe(STEP.DECLINED);
    expect(cohortStep(me(invitation({ state: 'closed', admitted_at: '2026-11-29T10:00:00Z' }))))
      .toBe(STEP.CLOSED);
    expect(cohortStep(me(invitation({ state: 'closed' }), [{ id: 1 }]))).toBe(STEP.GROUP);
  });

  test('the step list marks the interview as the first step', () => {
    expect(stepIndex(STEP.START)).toBe(0);
    expect(stepIndex(STEP.INTERVIEW)).toBe(0);
    expect(stepIndex(STEP.REPORT)).toBe(1);
    expect(stepIndex(STEP.CALL)).toBe(2);
    expect(stepIndex(STEP.GROUP)).toBe(3);
    expect(stepIndex(STEP.NONE)).toBe(-1);
  });
});

describe('the kept code', () => {
  beforeEach(() => window.localStorage.clear());

  test('is kept, read, and forgotten; separately from a referral invite', () => {
    expect(rememberCohortCode(CODE)).toBe(true);
    expect(pendingCohortCode()).toBe(CODE);
    expect(window.localStorage.getItem('loore:invite')).toBeNull();
    forgetCohortCode();
    expect(pendingCohortCode()).toBeNull();
  });

  test('a malformed code is refused, an old one dropped', () => {
    expect(rememberCohortCode('no spaces allowed')).toBe(false);
    rememberCohortCode(CODE, 0);
    expect(pendingCohortCode(COHORT_CODE_MAX_AGE_DAYS * 86400000 + 1)).toBeNull();
  });

  test('every refusal drops it (it can\'t succeed for this account); a server or network error keeps it', () => {
    expect(claimRefusalIsFinal(404)).toBe(true);
    expect(claimRefusalIsFinal(409)).toBe(true);
    expect(claimRefusalIsFinal(403)).toBe(true);
    expect(claimRefusalIsFinal(500)).toBe(false);
    expect(claimRefusalIsFinal(undefined)).toBe(false);
  });

  test('signing out forgets the code and the way back to the interview', () => {
    rememberCohortCode(CODE);
    markInterviewOpen('First Eight');
    forgetCohortOnSignOut();
    expect(pendingCohortCode()).toBeNull();
    expect(interviewBackLinkName('/node/1', { cohort: { name: 'First Eight' } })).toBeNull();
  });
});

describe('sent to /cohort once per session', () => {
  const invited = (done = false) => ({
    approved: true, cohort: { name: 'First Eight', role: 'invited', report_step_done: done },
  });

  test('an invitee with an open report step who lands on Home', () => {
    expect(shouldShowCohort(invited(), '/', { shown: false })).toBe(true);
    expect(shouldShowCohort(invited(), '/dashboard', { shown: false })).toBe(true);
  });

  test('not twice, not elsewhere, not when the report step is done', () => {
    expect(shouldShowCohort(invited(), '/', { shown: true })).toBe(false);
    expect(shouldShowCohort(invited(), '/log', { shown: false })).toBe(false);
    expect(shouldShowCohort(invited(true), '/', { shown: false })).toBe(false);
  });

  test('a founder is never sent; a kept code is, while the flag is on', () => {
    const founder = { approved: true, cohort: { name: 'X', role: 'founder', report_step_done: true } };
    expect(shouldShowCohort(founder, '/', { shown: false })).toBe(false);
    expect(shouldShowCohort({ approved: true, cohort: null }, '/', { shown: false, pendingCode: CODE }))
      .toBe(true);
    // COHORT_V1 off: the user has no `cohort` field at all.
    expect(shouldShowCohort({ approved: true }, '/', { shown: false, pendingCode: CODE }))
      .toBe(false);
  });

  test('reportStepOpen reads the user payload', () => {
    expect(reportStepOpen(null)).toBe(false);
    expect(reportStepOpen({ cohort: { role: 'member', report_step_done: false } })).toBe(true);
    expect(reportStepOpen({ cohort: { role: 'member', report_step_done: true } })).toBe(false);
  });
});

describe('times', () => {
  test('the call reads in the viewer\'s time zone, which is named', () => {
    const now = new Date('2026-11-27T12:00:00Z');
    expect(callTimeText('2026-12-03T18:00:00Z', { now, timeZone: 'Europe/Prague' }))
      .toBe('Thursday 3 December at 19:00 (your time, Europe/Prague)');
    expect(callTimeText('2026-12-03T18:00:00Z', { now, timeZone: 'America/New_York' }))
      .toBe('Thursday 3 December at 13:00 (your time, America/New York)');
    expect(callTimeText('2027-01-07T18:00:00Z', { now, timeZone: 'UTC' }))
      .toBe('Thursday 7 January 2027 at 18:00 (your time, UTC)');
    expect(callTimeText(null)).toBe('');
  });

  test('the founder\'s local input goes to the server as UTC with a Z, and comes back local', () => {
    const iso = fromLocalInput('2026-12-03T19:00');
    expect(iso).toMatch(/Z$/);
    // The same moment as the local wall-clock time typed.
    expect(iso).toBe(new Date(2026, 11, 3, 19, 0).toISOString());
    expect(toLocalInput(iso)).toBe('2026-12-03T19:00');
    // What the server sends back (UTC with a Z) shows as local time.
    expect(toLocalInput(new Date(2026, 11, 3, 19, 0).toISOString())).toBe('2026-12-03T19:00');
    expect(fromLocalInput('')).toBeNull();
    expect(toLocalInput(null)).toBe('');
  });

  test('a founder in Prague typing 19:00 sends 18:00 UTC (the server stores UTC)', () => {
    const before = process.env.TZ;
    process.env.TZ = 'Europe/Prague';
    try {
      expect(fromLocalInput('2026-12-03T19:00')).toBe('2026-12-03T18:00:00.000Z');
      expect(toLocalInput('2026-12-03T18:00:00Z')).toBe('2026-12-03T19:00');
      // A time the server sent without a zone marker is read as UTC.
      expect(toLocalInput('2026-12-03T18:00:00')).toBe('2026-12-03T19:00');
      // Summer time.
      expect(fromLocalInput('2027-06-03T19:00')).toBe('2027-06-03T17:00:00.000Z');
    } finally {
      process.env.TZ = before;
    }
  });

  test('a day reads as that calendar day', () => {
    expect(dayText('2027-01-08')).toBe('8 January 2027');
    expect(dayText('nope')).toBe('');
  });
});

test('errorSentence shows the server\'s sentence, never an app-wide code', () => {
  const err = (data) => ({ response: { data } });
  expect(errorSentence(err({ error: 'Start the interview first.', code: 'cohort_interview_first' })))
    .toBe('Start the interview first.');
  expect(errorSentence(err({ error: 'subscription_required', message: 'Loore needs a subscription.' })))
    .toBe('Loore needs a subscription.');
  expect(errorSentence({})).toBe('That did not work. Try again.');
});

describe('the link back from the interview', () => {
  beforeEach(() => window.sessionStorage.clear());
  const user = { cohort: { name: 'First Eight' } };

  test('only on a thread or Voice, after /cohort opened the interview, for a cohort person', () => {
    expect(isInterviewPage('/node/124')).toBe(true);
    expect(isInterviewPage('/voice')).toBe(true);
    expect(isInterviewPage('/log')).toBe(false);
    expect(isInterviewPage('/node/124/edit')).toBe(false);
    expect(interviewBackLinkName('/node/124', user)).toBeNull();
    markInterviewOpen('First Eight');
    expect(interviewBackLinkName('/node/124', user)).toBe('First Eight');
    expect(interviewBackLinkName('/voice', user)).toBe('First Eight');
    expect(interviewBackLinkName('/log', user)).toBeNull();
    expect(interviewBackLinkName('/node/124', { cohort: null })).toBeNull();
  });
});
