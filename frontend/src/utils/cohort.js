// The first cohort (docs/experiments/FIRST-COHORT*.md), on the web: the
// invitation code kept through the X sign-in, which step /cohort shows,
// the once-per-session send to /cohort, and how the call's time reads.
// Everything the server says is read from GET /api/cohort/me and the
// `cohort` field of the current user (both only while COHORT_V1 is on).

import { codeStore } from './invite';
import { parseTimestamp } from './date';

// ── The invitation code, kept through the sign-in ───────────────────────

// "Start the interview" on /invitation/<code> keeps the code here, the way
// a referral invite code is kept (utils/invite.js), and /cohort claims it
// once the person is signed in. Unlike the referral code, a sign-in does
// not drop it: only the claim, or a refusal that will not change, does.
export const COHORT_CODE_MAX_AGE_DAYS = 30;
const cohortCodes = codeStore('loore:cohortInvitation', COHORT_CODE_MAX_AGE_DAYS);

export function rememberCohortCode(code, now = Date.now()) {
  return cohortCodes.remember(code, now);
}

export function pendingCohortCode(now = Date.now()) {
  return cohortCodes.pending(now);
}

export function forgetCohortCode() {
  cohortCodes.forget();
}

// Claim refusals after which the same code can never be claimed by this
// account: the code is dropped. The two X refusals keep it, so signing in
// with the right X account claims it without opening the link again.
export const FINAL_CLAIM_CODES = new Set([
  'cohort_not_open', 'cohort_taken', 'cohort_owner', 'cohort_other',
]);

export function claimRefusalIsFinal(status, code) {
  return status === 404 || FINAL_CLAIM_CODES.has(code);
}

// ── Which step /cohort shows ────────────────────────────────────────────

export const STEP = {
  LOADING: 'loading',
  CLAIM: 'claim',
  NONE: 'none',
  START: 'start',
  INTERVIEW: 'interview',
  REPORT: 'report',
  CALL: 'call',
  GROUP: 'group',
  DECLINED: 'declined',
  CLOSED: 'closed',
};

export const DRAFTING = new Set(['pending', 'processing']);

export function isLiveInvitation(inv) {
  return !!inv && inv.state !== 'declined' && inv.state !== 'closed';
}

/**
 * The one step /cohort shows for `me` (the answer of GET /api/cohort/me,
 * or null while it loads) and a code kept from the invitation page.
 *
 * - a kept code and no live invitation: claim it;
 * - no invitation: the group page for a founder, else "none";
 * - declined / closed: one sentence (a founder still gets the group);
 * - admitted: the group page;
 * - report sent or "Send nothing" pressed: the call with the founder;
 * - a report (or a draft being written, failed, or with nothing to
 *   draft): the report;
 * - an interview started: continue it, or "I'm done";
 * - else: the letter and starting the interview.
 */
export function cohortStep(me, pendingCode = null) {
  if (!me) return STEP.LOADING;
  const inv = me.invitation;
  const founder = Array.isArray(me.owner_of) && me.owner_of.length > 0;
  if (pendingCode && !isLiveInvitation(inv)) return STEP.CLAIM;
  if (!inv) return founder ? STEP.GROUP : STEP.NONE;
  if (!isLiveInvitation(inv)) {
    if (founder) return STEP.GROUP;
    return inv.state === 'declined' ? STEP.DECLINED : STEP.CLOSED;
  }
  if (inv.admitted_at) return STEP.GROUP;
  const report = inv.report || {};
  if (report.sent_at || report.skipped_at) return STEP.CALL;
  if (report.node_id || report.task_status) return STEP.REPORT;
  if (inv.interview && inv.interview.node_id) return STEP.INTERVIEW;
  return STEP.START;
}

// The steps in the order the person goes through them, for the short
// list at the top of /cohort (claiming and the end states are not shown).
export const STEP_ORDER = [
  { step: STEP.START, label: 'The interview' },
  { step: STEP.REPORT, label: 'The report' },
  { step: STEP.CALL, label: 'A call' },
  { step: STEP.GROUP, label: 'The group' },
];

export function stepIndex(step) {
  if (step === STEP.INTERVIEW) return 0;
  return STEP_ORDER.findIndex((s) => s.step === step);
}

// ── Sent to /cohort once per session ────────────────────────────────────

const SENT_KEY = 'loore:cohortShown';

// Where a sign-in lands when it names no page: Home, and /dashboard (the
// server's default after a sign-in, which goes on to /profile).
export const SIGN_IN_LANDINGS = new Set(['/', '/dashboard']);

// An invited person or a member whose report step is not finished.
export function reportStepOpen(user) {
  const c = user && user.cohort;
  return !!c && (c.role === 'invited' || c.role === 'member') && !c.report_step_done;
}

export function cohortShownThisSession() {
  try {
    return window.sessionStorage.getItem(SENT_KEY) === '1';
  } catch (e) {
    return true; // no storage: never send anyone anywhere
  }
}

export function markCohortShown() {
  try {
    window.sessionStorage.setItem(SENT_KEY, '1');
  } catch (e) {
    // Ignore — storage unavailable.
  }
}

/**
 * Whether the page this load arrived on (`firstPath`) should give way to
 * /cohort: a landing of a sign-in, a person with an unfinished report step
 * (or a kept invitation code), and not done yet in this session.
 */
export function shouldShowCohort(user, firstPath, { shown, pendingCode } = {}) {
  if (!user || !user.approved || shown) return false;
  if (!SIGN_IN_LANDINGS.has(firstPath)) return false;
  return reportStepOpen(user) || (!!pendingCode && 'cohort' in user);
}

// ── Times ───────────────────────────────────────────────────────────────

function viewerTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch (e) {
    return null;
  }
}

/**
 * The call's time in the viewer's own time zone:
 * "Thursday 3 December at 19:00 (your time, Europe/Prague)". The year is
 * added when it is not this year's.
 */
export function callTimeText(iso, { now = new Date(), timeZone } = {}) {
  if (!iso) return '';
  const d = parseTimestamp(iso);
  if (isNaN(d.getTime())) return '';
  const zone = timeZone || viewerTimeZone() || undefined;
  const opts = zone ? { timeZone: zone } : {};
  // Each part on its own: browsers join them differently once a year is in.
  const part = (date, o) => date.toLocaleDateString('en-GB', { ...opts, ...o });
  const year = part(d, { year: 'numeric' });
  const date = [
    part(d, { weekday: 'long' }), part(d, { day: 'numeric' }), part(d, { month: 'long' }),
    ...(year !== part(now, { year: 'numeric' }) ? [year] : []),
  ].join(' ');
  const time = d.toLocaleTimeString('en-GB', {
    ...opts, hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const where = zone ? `your time, ${zone.replace(/_/g, ' ')}` : 'your time';
  return `${date} at ${time} (${where})`;
}

// "today", "yesterday" or "on 28 November" (with the year when it is not
// this year's), for "You started the interview …" and "Set … on …".
export function onDayText(iso, now = new Date()) {
  if (!iso) return '';
  const d = parseTimestamp(iso);
  if (isNaN(d.getTime())) return '';
  if (d.toDateString() === now.toDateString()) return 'today';
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return 'yesterday';
  return `on ${d.toLocaleDateString('en-GB', {
    day: 'numeric', month: 'long',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  })}`;
}

// "28 November 2026" for a day ("YYYY-MM-DD"), read as that calendar day.
export function dayText(day) {
  if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return '';
  const [y, m, d] = day.split('-').map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.toLocaleDateString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
  });
}

// A <input type="datetime-local"> value ("2026-12-03T19:00") for an ISO
// time, in the viewer's time zone; and back to ISO (UTC, with a Z).
export function toLocalInput(iso) {
  if (!iso) return '';
  const d = parseTimestamp(iso);
  if (isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fromLocalInput(value) {
  if (!value) return null;
  const d = new Date(value);
  return isNaN(d.getTime()) ? null : d.toISOString();
}

// The error sentence of a refused request, as the server wrote it.
export function errorSentence(err, fallback = 'That did not work. Try again.') {
  const data = err && err.response && err.response.data;
  if (data && typeof data.error === 'string' && data.error
      && !['no_subscription', 'subscription_required',
        'monthly_spend_limit_reached'].includes(data.error)) {
    return data.error;
  }
  if (data && typeof data.message === 'string' && data.message) return data.message;
  return fallback;
}
