// Shared date/time formatting helpers (#128, frontend).
//
// Backend timestamps are UTC but were historically serialized without a
// timezone marker (e.g. "2026-05-29T10:00:00" instead of "...Z"). The native
// `new Date("2026-05-29T10:00:00")` parses a marker-less string as LOCAL time,
// which silently shifts displayed dates by the viewer's UTC offset. These
// helpers DEFENSIVELY append "Z" when no timezone marker is present, so the
// value is always interpreted as UTC and then rendered in the browser's local
// timezone.

const DATE_OPTS = { month: 'short', day: 'numeric', year: 'numeric' };

/**
 * Parse an ISO timestamp into a Date, defensively treating marker-less strings
 * as UTC. Returns an invalid Date for unparseable input (callers guard on the
 * empty/null case before calling).
 */
export function parseTimestamp(iso) {
  if (iso instanceof Date) return iso;
  let s = String(iso);
  // A timezone marker is a trailing "Z", or a "+HH:MM" / "-HH:MM" offset in
  // the time portion. If the string has a time component ("T...") but no such
  // marker, assume UTC and append "Z".
  const hasTime = s.includes('T') || /\d{2}:\d{2}/.test(s);
  const hasTzMarker = /[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s);
  if (hasTime && !hasTzMarker) {
    s = s + 'Z';
  }
  return new Date(s);
}

/**
 * Date-only formatter with relative "today"/"yesterday" wording for recent
 * dates, falling back to "Mon D, YYYY".
 *
 * @param {string|Date} iso
 * @param {object} [opts]
 * @param {string} [opts.fallback=''] returned when iso is null/empty.
 * @param {boolean} [opts.relative=true] use "today"/"yesterday" wording.
 */
export function formatDate(iso, opts = {}) {
  const { fallback = '', relative = true } = opts;
  if (!iso) return fallback;
  const d = parseTimestamp(iso);
  if (isNaN(d.getTime())) return fallback;

  if (relative) {
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return 'today';
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return 'yesterday';
  }
  return d.toLocaleDateString('en-US', DATE_OPTS);
}

/**
 * Full date + time formatter (locale-aware), e.g. used for node footers.
 *
 * @param {string|Date} iso
 * @param {object} [opts]
 * @param {string} [opts.fallback=''] returned when iso is null/empty.
 */
export function formatDateTime(iso, opts = {}) {
  const { fallback = '' } = opts;
  if (!iso) return fallback;
  const d = parseTimestamp(iso);
  if (isNaN(d.getTime())) return fallback;
  return d.toLocaleString();
}
