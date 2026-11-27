// Referrals (#439): an invite link (/invite/<code>) leaves its code in this
// browser until a sign-in carries it to the server: the X sign-in as a
// query parameter, the email sign-in inside the link it mails. Nothing
// here shows or asks anything about the person who sent the link, and the
// server never says whether a code belongs to anyone.

const KEY = 'loore:invite';
// How long a code waits in the browser for a sign-in.
export const INVITE_MAX_AGE_DAYS = 30;
const CODE_RE = /^[A-Za-z0-9_-]{8,64}$/;

// A code kept in this browser under `key` until a sign-in uses it, for at
// most `maxAgeDays`. The referral invite above and the first cohort's
// invitation (utils/cohort.js) keep theirs this way, each under its own key.
export function codeStore(key, maxAgeDays) {
  const forget = () => {
    try {
      window.localStorage.removeItem(key);
    } catch (e) {
      // Ignore — storage unavailable.
    }
  };
  const remember = (code, now = Date.now()) => {
    if (typeof code !== 'string' || !CODE_RE.test(code)) return false;
    try {
      window.localStorage.setItem(key, JSON.stringify({ code, at: now }));
      return true;
    } catch (e) {
      return false; // private mode: the code is lost, nothing breaks
    }
  };
  const pending = (now = Date.now()) => {
    let saved = null;
    try {
      saved = JSON.parse(window.localStorage.getItem(key) || 'null');
    } catch (e) {
      saved = null;
    }
    if (!saved || typeof saved.code !== 'string' || !CODE_RE.test(saved.code)) {
      return null;
    }
    if (!(now - (saved.at || 0) <= maxAgeDays * 86400000)) {
      forget();
      return null;
    }
    return saved.code;
  };
  return { remember, pending, forget };
}

const invites = codeStore(KEY, INVITE_MAX_AGE_DAYS);

export function rememberInvite(code, now = Date.now()) {
  return invites.remember(code, now);
}

export function pendingInvite(now = Date.now()) {
  return invites.pending(now);
}

export function forgetInvite() {
  invites.forget();
}
