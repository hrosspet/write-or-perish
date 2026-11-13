// Where a sign-in returns to (#704): the page it started on.
//
// Every page that sends a signed-out person to sign in passes where they
// were (signInPath); the sign-in page hands it to the X sign-in and to the
// emailed link, and the server sends the browser back to it. safeDestination
// is the twin of backend/utils/destination.py: both are tested against
// utils/destinationCases.json, and anything that is not a page inside Loore
// is dropped without a word (the person lands on the default page).
//
// A new account meets its steps first: the terms over the page, then the
// waiting screen (the destination is dropped: it can't be reached) or, with
// payments on, the subscribe page, whose checkout leaves Loore and comes back
// on the Account page. So the page a signed-in account arrived on is kept in
// this tab (rememberArrival) and opened once the app itself shows
// (<PendingDestination />), if a later page load than the arrival's: within
// the same load the person is already there.

// Longest destination kept (heuristic, #704); the server's MAX_DESTINATION_LENGTH.
export const MAX_DESTINATION_LENGTH = 2048;

const DECODE_ROUNDS = 2;
const NOT_PAGES = ['/auth', '/api'];
const PRINTABLE_ASCII = /^[\x21-\x7e]+$/;
const BAD_ESCAPE = /%(?![0-9A-Fa-f]{2})/;
// Unicode's control, format, line and paragraph separators (Python's
// unicodedata categories Cc, Cf, Zl, Zp on the server).
const UNSEEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;

const pathPart = (value) => value.split(/[?#]/, 1)[0];

function safePath(path) {
  if (!path.startsWith('/') || path.includes('//') || path.includes('\\')) {
    return false;
  }
  if (UNSEEN.test(path)) return false;
  const lowered = path.toLowerCase();
  return !NOT_PAGES.some((p) => lowered === p || lowered.startsWith(`${p}/`));
}

function decode(path) {
  if (BAD_ESCAPE.test(path)) return null;
  try {
    return decodeURIComponent(path);
  } catch (e) {
    return null; // bytes that are not UTF-8
  }
}

// `value` when it is a page inside Loore to return to, else null.
export function safeDestination(value) {
  if (typeof value !== 'string' || !value
      || value.length > MAX_DESTINATION_LENGTH
      || !PRINTABLE_ASCII.test(value) || value.includes('\\')) {
    return null;
  }
  let path = pathPart(value);
  if (!safePath(path)) return null;
  for (let i = 0; i < DECODE_ROUNDS; i += 1) {
    const decoded = decode(path);
    if (decoded === null || !safePath(decoded)) return null;
    if (decoded === path) return value;
    path = decoded;
  }
  return decode(path) === path ? value : null;
}

// The page in front of the person: path, query and fragment (the Account
// page's #x, a reply's anchor).
export function here(location) {
  return `${location.pathname}${location.search || ''}${location.hash || ''}`;
}

// The sign-in page, on the way to `destination` (or to the default page).
export function signInPath(destination) {
  const safe = safeDestination(destination);
  return safe ? `/login?returnUrl=${encodeURIComponent(safe)}` : '/login';
}

// The pages that introduce Loore, and the sign-in and step pages: signing in
// from one of them goes to the start page, not back to it.
const START_PAGE_FROM = new Set([
  '/', '/landing', '/vision', '/why-loore', '/how-to', '/login',
  '/alpha-thank-you', '/welcome', '/dashboard',
  '/account-deleted', '/account-restore',
]);

// The nav's sign-in link: back to this page, unless it is one of the above.
export function signInPathFrom(location) {
  return signInPath(START_PAGE_FROM.has(location.pathname) ? '/' : here(location));
}

// Where the sign-in page sends a finished sign-in: its returnUrl (or `next`,
// the server's name for it), when it is a page inside Loore.
export function returnUrlFrom(searchParams, fallback = '/dashboard') {
  return safeDestination(searchParams.get('returnUrl'))
    || safeDestination(searchParams.get('next'))
    || fallback;
}

// ── The arrival, kept across a step that leaves the page ────────────────

const PENDING_KEY = 'loore:pendingDestination';
// How long an arrival waits for its account's steps (heuristic, #704): the
// terms and a checkout take minutes; an hour later the person is somewhere
// else.
export const PENDING_MAX_AGE_MS = 60 * 60 * 1000;
// This page load. An arrival kept by an earlier load is one a step took the
// person away from.
export const PAGE_LOAD = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

// Pages that never replace a kept arrival: the start and step pages above,
// and the Account page, where a checkout comes back.
const NOT_AN_ARRIVAL = new Set([...START_PAGE_FROM, '/account', '/confirm-account-deletion']);

function readPending() {
  try {
    return JSON.parse(window.sessionStorage.getItem(PENDING_KEY) || 'null');
  } catch (e) {
    return null;
  }
}

export function forgetPending() {
  try {
    window.sessionStorage.removeItem(PENDING_KEY);
  } catch (e) {
    // Storage unavailable: nothing was kept.
  }
}

// A signed-in account's page load arrived on `destination`.
export function rememberArrival(destination, { load = PAGE_LOAD, now = Date.now() } = {}) {
  const path = safeDestination(destination);
  if (!path || NOT_AN_ARRIVAL.has(pathPart(path))) return;
  try {
    window.sessionStorage.setItem(PENDING_KEY, JSON.stringify({ path, load, at: now }));
  } catch (e) {
    // Private mode or full: the arrival is not kept, nothing breaks.
  }
}

// The kept arrival, taken (forgotten) at once: { path, earlierLoad } or null.
export function takePending({ load = PAGE_LOAD, now = Date.now() } = {}) {
  const saved = readPending();
  forgetPending();
  if (!saved || typeof saved.at !== 'number' || !(now - saved.at <= PENDING_MAX_AGE_MS)) {
    return null;
  }
  const path = safeDestination(saved.path);
  return path ? { path, earlierLoad: saved.load !== load } : null;
}
