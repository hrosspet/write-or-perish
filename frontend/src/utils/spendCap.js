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
