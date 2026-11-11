// Circles: wording shared by the picker, the entry page and the dialogs.

const NUMBER_WORDS = [
  'no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
  'nine', 'ten', 'eleven', 'twelve',
];

// "two" … "twelve", then digits.
export function countWord(n) {
  return n >= 0 && n < NUMBER_WORDS.length ? NUMBER_WORDS[n] : String(n);
}

// A person's Loore name as it is written in Circles copy: "@ben". A
// name that already carries "@" is not given a second one.
export function atName(name) {
  const bare = String(name || '').replace(/^@+/, '');
  return bare ? `@${bare}` : '';
}

// ["a", "b", "c"] → "@a, @b and @c"
export function joinNames(names) {
  const list = (names || []).map(atName).filter(Boolean);
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

// Things, not people: ["profile", "memory", "to-do list"] →
// "profile, memory and to-do list".
export function joinPlain(items) {
  const list = (items || []).filter(Boolean);
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

// What an AI reply in a shared thread reads of its requester's own
// artifacts (audience.ai_reads, from the server), as a sentence; ''
// when it reads none.
export function aiReadsSentence(aiReads) {
  const list = joinPlain(aiReads);
  return list
    ? `Your AI reads your ${list} for this reply and could share private information from them.`
    : '';
}

// The same for a reply only its requester will read ("Only me"): the
// prompt someone else shared still reads the requester's data.
export function aiReadsPrivateSentence(aiReads) {
  const list = joinPlain(aiReads);
  return list
    ? `Your AI reads your ${list} for this reply, as the prompt shared in this thread asks.`
    : '';
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
  const author = atName(audience.author);
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

// The same for the dialog before an AI reply. It has its own key, so
// ticking the box for typed replies does not skip the AI dialog.
export function aiReplyAckKey(entryId, userId) {
  return `loore_circle_ai_reply_ack_${userId}_${entryId}`;
}

// Craft mode, in a shared thread: whether the next AI reply is shared
// with the thread ("shared", the default) or only its requester reads
// it ("private"). Kept per thread, per user, on this device.
export function aiReplyPrivacyKey(entryId, userId) {
  return `loore_circle_ai_reply_privacy_${userId}_${entryId}`;
}

// Why no AI reply can be generated on `node` in a shared thread, in one
// sentence, or null when it can.
export function aiReplyOffReason(audience, node, user) {
  if (!audience) return null;
  const closed = "This entry's AI usage is None, so no AI can read it.";
  if (audience.entry_ai_usage === 'none') {
    return audience.is_author
      ? closed
      : `${atName(audience.author)} set this entry's AI usage to None, so no AI can read it.`;
  }
  if (node?.ai_usage === 'none') return closed;
  if (user?.default_ai_usage === 'none') {
    return 'Your own AI usage is set to None in Account settings.';
  }
  return null;
}

// The "Remove <name>" dialog. A person reads an entry through any of
// its circles or by being named for it, so the dialog says what removing
// them from this one circle does, and what it does not.
export function removeMemberBody(rawName, circleName) {
  const name = atName(rawName);
  return `From this moment ${name} no longer reads your entries through `
    + `${circleName}. An entry you also shared with ${name} another way `
    + `(another circle ${name} is in, or by name) stays open to ${name}. `
    + `${name} is not told.`;
}

export const NO_READERS_MESSAGE = "Nobody could read this entry yet. Choose "
  + "a circle with people in it, or name a person.";

// The check before sharing found readers the sentence on the screen did
// not name (a name typed and not yet looked up): nothing is shared, and
// the sentence is brought up to date.
export const READERS_CHANGED_MESSAGE = "Who can read this has changed. "
  + "Check the names above, then share again.";

// The same people, in any order, case ignored.
export function sameNames(a, b) {
  const norm = (list) => (list || []).map((n) => String(n).toLowerCase()).sort().join(',');
  return norm(a) === norm(b);
}

export function possessive(name) {
  return `${atName(name)}'s`;
}
