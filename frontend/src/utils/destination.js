// Where a sign-in returns to (#704): the page it started on.
//
// Every page that sends a signed-out person to sign in passes where they
// were (signInPath); the sign-in page hands it to the X sign-in and to the
// emailed link, and the server sends the browser back to it. safeDestination
// is the twin of backend/utils/destination.py: both are tested against
// utils/destinationCases.json, and anything that is not an ordinary page of
// Loore is dropped without a word (the person lands on the default page).
//
// A new account meets its steps first: the terms over the page, then the
// waiting screen (the destination is dropped: it can't be reached) or, with
// payments on, the subscribe page, whose checkout leaves Loore and comes back
// on the Account page. So the page a signed-in account arrived on is kept in
// this tab (rememberArrival) and opened once the app itself shows
// (<PendingDestination />), if a later page load than the arrival's: within
// the same load the person is already there.

// The rules, the same as the server's (backend/utils/destination.py, which
// explains them), written the same way. No Unicode database is consulted, so
// the browser and the server agree on every value.

// Longest destination kept (heuristic, #704); the server's MAX_DESTINATION_LENGTH.
export const MAX_DESTINATION_LENGTH = 2048;

const DECODE_ROUNDS = 2;

// Never a place to come back to, with everything below each: server
// routes, and the pages that act when opened or belong to a flow already
// under way (the server's EXCLUDED_PATHS, with the reason for each).
export const EXCLUDED_PATHS = [
  '/auth', '/api', '/media', '/static', '/health', '/ready',
  '/login', '/invite', '/invite-mismatch', '/account-restore', '/account-deleted',
];

// A checkout's return to the Account page completes it when opened.
export const EXCLUDED_QUERY_NAMES = ['session_id', 'subscription'];

// C0 and C1 controls, and Unicode 16's format characters and line and
// paragraph separators (U+2060-U+206F whole): the server's UNSEEN_RANGES.
const UNSEEN_RANGES = [
  [0x0000, 0x001F], [0x007F, 0x009F], [0x00AD, 0x00AD],
  [0x0600, 0x0605], [0x061C, 0x061C], [0x06DD, 0x06DD],
  [0x070F, 0x070F], [0x0890, 0x0891], [0x08E2, 0x08E2],
  [0x180E, 0x180E], [0x200B, 0x200F], [0x2028, 0x202E],
  [0x2060, 0x206F], [0xFEFF, 0xFEFF], [0xFFF9, 0xFFFB],
  [0x110BD, 0x110BD], [0x110CD, 0x110CD], [0x13430, 0x1343F],
  [0x1BCA0, 0x1BCA3], [0x1D173, 0x1D17A], [0xE0001, 0xE0001],
  [0xE0020, 0xE007F],
];

const PRINTABLE_ASCII = /^[\x21-\x7e]+$/;
const BAD_ESCAPE = /%(?![0-9A-Fa-f]{2})/;

// [path, query, fragment]; query and fragment are null when absent.
function split(value) {
  const hashAt = value.indexOf('#');
  const before = hashAt >= 0 ? value.slice(0, hashAt) : value;
  const fragment = hashAt >= 0 ? value.slice(hashAt + 1) : null;
  const queryAt = before.indexOf('?');
  return queryAt >= 0
    ? [before.slice(0, queryAt), before.slice(queryAt + 1), fragment]
    : [before, null, fragment];
}

const pathPart = (value) => split(value)[0];

// One round of percent-decoding, or null for an escape a browser would
// refuse (not two hex digits, or bytes that are not UTF-8).
function decode(text) {
  if (BAD_ESCAPE.test(text)) return null;
  try {
    return decodeURIComponent(text);
  } catch (e) {
    return null;
  }
}

// The path as written and decoded, or null: a malformed escape, or a path
// that would still change on a third round.
function pathForms(path) {
  const forms = [path];
  for (let i = 0; i < DECODE_ROUNDS; i += 1) {
    const decoded = decode(forms[forms.length - 1]);
    if (decoded === null) return null;
    if (decoded === forms[forms.length - 1]) return forms;
    forms.push(decoded);
  }
  return decode(forms[forms.length - 1]) === forms[forms.length - 1] ? forms : null;
}

// A query or fragment as written and decoded: a malformed escape as
// written drops it; one that appears only after a round is literal text.
function textForms(text) {
  const forms = [text];
  for (let i = 0; i < DECODE_ROUNDS; i += 1) {
    const decoded = decode(forms[forms.length - 1]);
    if (decoded === null) return forms.length === 1 ? null : forms;
    if (decoded === forms[forms.length - 1]) break;
    forms.push(decoded);
  }
  return forms;
}

function isPagePath(form) {
  if (!form.startsWith('/') || !PRINTABLE_ASCII.test(form)
      || form.includes('\\') || form.includes('//')) {
    return false;
  }
  if (form.split('/').some((segment) => segment.includes('.'))) return false;
  const lowered = form.toLowerCase();
  return !EXCLUDED_PATHS.some((p) => lowered === p || lowered.startsWith(`${p}/`));
}

function hasUnseen(text) {
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    if (UNSEEN_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi)) return true;
  }
  return false;
}

function namesExcludedParameter(query) {
  return query.split('&').some((pair) => {
    const name = decode(pair.split('=', 1)[0]);
    return name !== null && EXCLUDED_QUERY_NAMES.includes(name.toLowerCase());
  });
}

// `value` when it is an ordinary page of Loore to return to, else null.
export function safeDestination(value) {
  if (typeof value !== 'string' || !value
      || value.length > MAX_DESTINATION_LENGTH
      || !PRINTABLE_ASCII.test(value) || value.includes('\\')) {
    return null;
  }
  const [path, query, fragment] = split(value);
  const forms = pathForms(path);
  if (forms === null || !forms.every(isPagePath)) return null;
  if (query !== null && namesExcludedParameter(query)) return null;
  for (const text of [query, fragment]) {
    if (text !== null) {
      const decoded = textForms(text);
      if (decoded === null || decoded.some(hasUnseen)) return null;
    }
  }
  return value;
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
