// The question the homepage, Voice and Text screens ask (#391). Until the
// user has written an entry in Loore (imports and AI replies don't count;
// the server's `has_own_entries` says), it is the welcome page's question;
// after that, the everyday one.
export const WELCOME_QUESTION =
  'What brought you to Loore — and what are you hoping to find here?';
export const EVERYDAY_QUESTION = "What's on your mind?";

// Only an explicit `false` means a newcomer: a user object without the
// flag (still loading, or an older response) gets the everyday question.
export const isNewcomer = (user) => user?.has_own_entries === false;

export const entryQuestion = (user) =>
  (isNewcomer(user) ? WELCOME_QUESTION : EVERYDAY_QUESTION);
