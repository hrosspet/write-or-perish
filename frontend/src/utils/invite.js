// Referrals (#439): an invite link (/invite/<code>) leaves its code in this
// browser until a sign-in carries it to the server: the X sign-in as a
// query parameter, the email sign-in inside the link it mails. Nothing
// here shows or asks anything about the person who sent the link, and the
// server never says whether a code belongs to anyone.

const KEY = 'loore:invite';
// How long a code waits in the browser for a sign-in.
export const INVITE_MAX_AGE_DAYS = 30;
const CODE_RE = /^[A-Za-z0-9_-]{8,64}$/;

export function rememberInvite(code, now = Date.now()) {
  if (typeof code !== 'string' || !CODE_RE.test(code)) return false;
  try {
    window.localStorage.setItem(KEY, JSON.stringify({ code, at: now }));
    return true;
  } catch (e) {
    return false; // private mode: the invite is lost, nothing breaks
  }
}

export function pendingInvite(now = Date.now()) {
  let saved = null;
  try {
    saved = JSON.parse(window.localStorage.getItem(KEY) || 'null');
  } catch (e) {
    saved = null;
  }
  if (!saved || typeof saved.code !== 'string' || !CODE_RE.test(saved.code)) {
    return null;
  }
  if (!(now - (saved.at || 0) <= INVITE_MAX_AGE_DAYS * 86400000)) {
    forgetInvite();
    return null;
  }
  return saved.code;
}

export function forgetInvite() {
  try {
    window.localStorage.removeItem(KEY);
  } catch (e) {
    // Ignore — storage unavailable.
  }
}
