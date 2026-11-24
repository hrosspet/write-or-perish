// Choices this browser remembers for the account that made them: the last
// privacy level and AI usage of a new entry, the "don't show again" answers,
// and the Agentic Reply / Auto-generate switches. They stay with that
// account: when a different account signs in here, they are removed, and
// its forms start from its own defaults. The same account signing in again
// (after a sign-out or an expired session) finds its choices as it left
// them. Not included: theme, craft mode, the Voice / Text choice of the home
// cards and the recording device key, which say nothing about an account.

export const CHOICE_KEYS = [
  'loore_last_privacy_level',
  'loore_last_ai_usage',
  'loore_public_reply_ack',
  'loore_agentic_reply',
  'loore_auto_generate',
  'loore:free-month-notice-dismissed',
];

// The id of the account the choices belong to.
export const OWNER_KEY = 'loore_choices_owner';

const storage = (fn) => {
  try { return fn(window.localStorage); } catch (e) { return null; /* private mode */ }
};

function clearChoices() {
  storage((s) => CHOICE_KEYS.forEach((key) => s.removeItem(key)));
}

// The app loaded with `userId` signed in. Choices of another account are
// removed. Choices stored before the owner was recorded are kept: they were
// made in this browser's current sign-in.
export function choicesSignedIn(userId) {
  if (userId === undefined || userId === null) return;
  const owner = storage((s) => s.getItem(OWNER_KEY));
  if (owner !== null && owner !== String(userId)) clearChoices();
  storage((s) => s.setItem(OWNER_KEY, String(userId)));
}

// The app loaded with nobody signed in. Choices whose owner was never
// recorded (stored before this file existed) cannot be matched to the next
// sign-in, so they are removed.
export function choicesSignedOut() {
  if (storage((s) => s.getItem(OWNER_KEY)) === null) clearChoices();
}
