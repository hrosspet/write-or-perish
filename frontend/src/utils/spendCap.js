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

let _capped = false;
let _liftsOn = null;

function rememberLiftDate(liftsOn) {
  if (typeof liftsOn === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(liftsOn)) {
    _liftsOn = liftsOn;
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('loore:spend-capped', (e) => {
    _capped = true;
    rememberLiftDate(e && e.detail && e.detail.liftsOn);
  });
}

export function markSpendBlocked(liftsOn) {
  _capped = true;
  rememberLiftDate(liftsOn);
}

export function isSpendBlocked() {
  return _capped;
}

// Re-surface the banner (and mark blocked) — call this when a capped user
// attempts a cost action that we're refusing client-side.
export function notifySpendBlocked() {
  _capped = true;
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
// than the start of next month.
export const SPEND_CAP_DEFAULT_MESSAGE =
  "You've reached your monthly usage limit for the free alpha. " +
  "It resets at the start of next month.";

export function spendCapBannerMessage(now = new Date()) {
  const lifts = spendCapLiftDate(now);
  if (lifts.getTime() === nextMonthStart(now).getTime()) {
    return SPEND_CAP_DEFAULT_MESSAGE;
  }
  return "You've reached your monthly usage limit for the free alpha. " +
    `It resets on ${formatLiftDate(lifts)}.`;
}

// Tests only: forget what this session learned.
export function resetSpendCapForTests() {
  _capped = false;
  _liftsOn = null;
}

export function spendCapToastMessage(action, now = new Date()) {
  const what = action === 'upload' ? 'upload audio'
    : action === 'glean' ? 'glean' : 'start a new recording';
  return `You've reached your monthly usage limit, so you can't ${what} `
    + `until it resets on ${spendCapResetDate(now)}.`;
}

// True for the 402 every capped endpoint returns (see api.js).
export function isSpendCapError(err) {
  const res = err && err.response;
  return !!res && res.status === 402
    && !!res.data && res.data.error === 'monthly_spend_limit_reached';
}
