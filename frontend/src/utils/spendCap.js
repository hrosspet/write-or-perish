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

let _capped = false;

if (typeof window !== 'undefined') {
  window.addEventListener('loore:spend-capped', () => { _capped = true; });
}

export function markSpendBlocked() {
  _capped = true;
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
// The cap resets at the start of the server's month, which is UTC.
export function spendCapResetDate(now = new Date()) {
  const reset = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  return reset.toLocaleDateString(undefined, {
    month: 'long', day: 'numeric', timeZone: 'UTC',
  });
}

export function spendCapToastMessage(action, now = new Date()) {
  const what = action === 'upload' ? 'upload audio' : 'start a new recording';
  return `You've reached your monthly usage limit, so you can't ${what} `
    + `until it resets on ${spendCapResetDate(now)}.`;
}

// True for the 402 every capped endpoint returns (see api.js).
export function isSpendCapError(err) {
  const res = err && err.response;
  return !!res && res.status === 402
    && !!res.data && res.data.error === 'monthly_spend_limit_reached';
}
