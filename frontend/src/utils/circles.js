// Circles: wording shared by the picker, the entry page and the dialogs.

const NUMBER_WORDS = [
  'no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
  'nine', 'ten', 'eleven', 'twelve',
];

// "two" … "twelve", then digits.
export function countWord(n) {
  return n >= 0 && n < NUMBER_WORDS.length ? NUMBER_WORDS[n] : String(n);
}

// ["a", "b", "c"] → "a, b and c"
export function joinNames(names) {
  const list = (names || []).filter(Boolean);
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

// Names typed into "for this entry only": commas or spaces, @ optional.
export function splitNames(text) {
  const seen = new Set();
  const out = [];
  (text || '').replace(/,/g, ' ').split(/\s+/).forEach((raw) => {
    const name = raw.trim().replace(/^@+/, '');
    if (name && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      out.push(name);
    }
  });
  return out;
}

// The sentence under the picker, before saving.
export function readersSentence(readers, what = 'entry') {
  if (!readers || readers.length === 0) return '';
  return `${joinNames(readers)} can read this ${what} and reply to it. `
    + 'Nobody else can.';
}

// The save button: "Share with Close friends" for one circle and no
// extra names, else "Share with 5 people".
export function shareButtonLabel({ circles, circleIds, names, readers }) {
  const picked = (circles || []).filter((c) => (circleIds || []).includes(c.id));
  if (picked.length === 1 && (names || []).length === 0) {
    return `Share with ${picked[0].name}`;
  }
  const n = (readers || []).length;
  if (n === 0) return 'Share';
  return `Share with ${n} ${n === 1 ? 'person' : 'people'}`;
}

// Everyone who sees a reply in a shared thread, except the one writing:
// the author and the readers.
export function replyAudienceNames(audience, myUsername) {
  if (!audience) return [];
  return [audience.author, ...(audience.readers || [])]
    .filter((name) => name && name !== myUsername);
}

// The banner over a shared entry, for a reader.
export function sharedBannerText(audience) {
  const author = audience.author;
  const others = Math.max((audience.readers || []).length - 1, 0);
  const whom = others === 0
    ? 'you'
    : `you and ${countWord(others)} other ${others === 1 ? 'person' : 'people'}`;
  const only = others === 0
    ? `Only you and ${author} can read it`
    : 'Only they can read it';
  return `${author} shared this with ${whom}. ${only}, and nobody but `
    + `${author} can make it public.`;
}

// "Don't ask again in this thread": per thread, per user, on this device.
export function replyAckKey(entryId, userId) {
  return `loore_circle_reply_ack_${userId}_${entryId}`;
}

export function possessive(name) {
  return `${name}'s`;
}
