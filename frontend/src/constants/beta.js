// The day the Beta opens. The owner will change this one line when the date
// is fixed; the waiting page reads it and nothing else carries the date.
// Format YYYY-MM-DD, read as a day in the visitor's own time zone.
// The iPhone app has its own copy (BetaRelease.opensOn in WaitlistView.swift).
export const BETA_OPENS_ON = "2026-12-21";

const MONTHS = [
  "January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December",
];

// "The Beta opens on 21 December." while that day is still ahead; null from
// the start of that day on, so the page reads as it did without the sentence.
export function betaOpensSentence(now = new Date(), opensOn = BETA_OPENS_ON) {
  const [y, m, d] = opensOn.split("-").map(Number);
  if (!(now < new Date(y, m - 1, d))) return null;
  return `The Beta opens on ${d} ${MONTHS[m - 1]}.`;
}
