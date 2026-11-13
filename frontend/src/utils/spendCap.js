// Client-side knowledge of the per-user monthly spend cap (issue #85), so
// cost actions can be blocked BEFORE they start — most importantly, before a
// voice recording begins, since a long recording stopped only at the end
// would be lost work.
//
// The flag is set from two sources: the user object on load (UserContext calls
// markSpendBlocked when user.spend_blocked is true) and any 402 surfaced this
// session (the api.js interceptor dispatches 'loore:spend-capped'). It only
// ever flips true within a session; a new month clears it on the next load
// (user.spend_blocked comes back false).
//
// Both sources also carry the date the cap lifts (user.spend_cap_lifts_on,
// the 402's lifts_on; "YYYY-MM-DD", UTC). It is the first of next month,
// unless last month's overflow is larger than a whole month's limit and
// holds the cap for longer (#594).
//
// Payments on (#438): the trial's ceiling and an alpha tester's free
// month's lift with the first payment, not on a date and not with the
// month. The server says so (user.spend_cap_lifts_with_payment, the 402's
// lifts_with_payment, a 2xx's spend_cap_lifts_with_payment), and the texts
// here then say that instead of a reset.

import { paymentsEnabled } from './subscription';

let _capped = false;
let _liftsOn = null;
let _liftsWithPayment = false;

function rememberLiftDate(liftsOn) {
  if (typeof liftsOn === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(liftsOn)) {
    _liftsOn = liftsOn;
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('loore:spend-capped', (e) => {
    _capped = true;
    rememberLiftDate(e && e.detail && e.detail.liftsOn);
    // A 402 (api.js) says which cap it is; the banner raised here without
    // a detail keeps what is known.
    if (e && e.detail) _liftsWithPayment = !!e.detail.liftsWithPayment;
  });
}

export function markSpendBlocked(liftsOn, liftsWithPayment) {
  _capped = true;
  rememberLiftDate(liftsOn);
  _liftsWithPayment = !!liftsWithPayment;
}

export function isSpendBlocked() {
  return _capped;
}

// Re-surface the banner (and mark blocked) — call this when a capped user
// attempts a cost action that we're refusing client-side.
// A response that saved the writing but skipped the reply for the cap
// (`spend_capped`) passes its `spend_cap_lifts_on` and
// `spend_cap_lifts_with_payment`; a press refused here passes nothing and
// keeps what is known.
export function notifySpendBlocked(liftsOn, liftsWithPayment) {
  _capped = true;
  rememberLiftDate(liftsOn);
  if (liftsOn !== undefined || liftsWithPayment !== undefined) {
    _liftsWithPayment = !!liftsWithPayment;
  }
  try {
    window.dispatchEvent(new CustomEvent('loore:spend-capped', {}));
  } catch (e) {
    // Ignore — CustomEvent unavailable.
  }
}

// Tells the capped user why the press they just made did nothing (#341).
// The banner (notifySpendBlocked / the api.js 402 interceptor) states the
// cap in general; this toast answers the record or upload press itself.
// The cap resets at the start of the server's month, which is UTC, or on
// the later date the server sent (a carry over a whole month's limit).
function nextMonthStart(now) {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
}

function spendCapLiftDate(now) {
  const next = nextMonthStart(now);
  if (!_liftsOn) return next;
  const [y, m, d] = _liftsOn.split('-').map(Number);
  const lifts = new Date(Date.UTC(y, m - 1, d));
  // A date from an earlier month of a long session is already past.
  return lifts > next ? lifts : next;
}

function formatLiftDate(date) {
  return date.toLocaleDateString(undefined, {
    month: 'long', day: 'numeric', timeZone: 'UTC',
  });
}

export function spendCapResetDate(now = new Date()) {
  return formatLiftDate(spendCapLiftDate(now));
}

// The banner's text when the 402 sent none (or the banner was raised here):
// the server's wording, which names the date only when the cap lifts later
// than the start of next month. While payments are on (#438) the limit is
// not the free alpha's, and the wording says so.
export const SPEND_CAP_DEFAULT_MESSAGE =
  "You've reached your monthly usage limit for the free alpha. " +
  "It resets at the start of next month.";

function capSentence() {
  return paymentsEnabled()
    ? "You've reached your monthly usage limit."
    : "You've reached your monthly usage limit for the free alpha.";
}

// A cap only the first payment lifts (the trial's, the free month's): the
// server's 402 names which; raised here, the words fit both. They name the
// state, never where or how to pay.
export const SPEND_CAP_UNTIL_PAYMENT_MESSAGE =
  "You've reached your usage limit. It lifts with your first payment.";

export function spendCapBannerMessage(now = new Date()) {
  if (_liftsWithPayment) return SPEND_CAP_UNTIL_PAYMENT_MESSAGE;
  const lifts = spendCapLiftDate(now);
  if (lifts.getTime() === nextMonthStart(now).getTime()) {
    return paymentsEnabled()
      ? `${capSentence()} It resets at the start of next month.`
      : SPEND_CAP_DEFAULT_MESSAGE;
  }
  return `${capSentence()} It resets on ${formatLiftDate(lifts)}.`;
}

// Tests only: forget what this session learned.
export function resetSpendCapForTests() {
  _capped = false;
  _liftsOn = null;
  _liftsWithPayment = false;
}

export function spendCapToastMessage(action, now = new Date()) {
  const what = action === 'upload' ? 'upload audio'
    : action === 'glean' ? 'glean' : 'start a new recording';
  if (_liftsWithPayment) {
    return `You've reached your usage limit, so you can't ${what} `
      + 'until your first payment.';
  }
  return `You've reached your monthly usage limit, so you can't ${what} `
    + `until it resets on ${spendCapResetDate(now)}.`;
}

// True for the 402 every capped endpoint returns (see api.js).
export function isSpendCapError(err) {
  const res = err && err.response;
  return !!res && res.status === 402
    && !!res.data && res.data.error === 'monthly_spend_limit_reached';
}
